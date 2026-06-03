from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from api.config import REVIEW_TIMEOUT_SECONDS

log = logging.getLogger(__name__)

SKILL_MD_PATH = Path("/opt/.claude/skills/paper-batch-review/SKILL.md")
_SKILL_SCRIPTS = "/opt/.claude/skills/paper-batch-review/scripts"

MAX_RETRIES = 2
STATE_VERSION = 2
REVIEW_STATE_DIR = Path("/opt/paper-integrity-checker/data/output/review_v2/state")
EVIDENCE_TIMEOUT_SECONDS = int(os.environ.get("REVIEW_COVERAGE_TIMEOUT_SECONDS", "240"))
EVIDENCE_MEMORY_MB = int(os.environ.get("REVIEW_COVERAGE_MEMORY_MB", "2048"))

PROMPT_HEADER = """你是一位学术论文诚信检测复核专家。请对以下论文的检测结果进行严格复核。

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单,务必先读): {evidence_path}

证据包(review_evidence.json)已由确定性脚本预先生成,包含:源数据各sheet列的预分类(自变量/统计输出/标准差/测量值)、确定性发现(重复列对/跨表复用/小数精度不一致)、必须逐一查看的主图清单、以及每条HIGH发现。其中 coverage_manifest.must_address 列出本次复核**必须逐项给出结论**的条目。请在文字中留下明确核查痕迹；覆盖率校验会记录遗漏提醒，但不会把单纯格式留痕不足直接等同于论文高风险。请先读证据包,再展开复核。

若证据包包含 deterministic_findings.cross_sheet_reuse_groups, 复核覆盖单位是“分组”而不是原始 cross_sheet_reuse 的每一条成员。你必须查看每个分组的代表样例、最高风险样例和 requires_expansion=true 的分组；原始 cross_sheet_reuse 列表用于追溯, 不需要逐条写成 100+ 段。若一个分组经代表样例确认属于同一类合理复用或同一类完整性问题, 可以按分组给出结论并说明抽查/扩展依据。

若已确认强确定性证据本身足以支持“建议高风险”（例如跨图/跨文件大量整列复用、小数精度跨sheet显著不一致、重复列对、恒等依赖或真实图像重复/拼接）, 可以围绕关键证据直接收敛为高风险结论；同时用简短句子说明其他维度是否有独立支持或暂无独立问题。不要为了完成低风险排除项而拖长高风险报告。

## 复核规则

请严格按照以下复核规则执行（来自 SKILL.md）：

"""

READER_FACING_TEXT_RULES = """

## 面向审核人员的写法要求（必须遵守）

复核报告会直接给非专业审核人员阅读。每个确认问题必须先交代位置和现象，再解释专业核验：
1. 问题位置：先写清楚哪个图、哪张表、哪个补充文件、sheet、行列或字段。
2. 发现了什么：用平实中文说明可见问题，例如“两块数据逐格完全相同”“论文图中两行结果完全重合”。
3. 为什么重要：说明为什么这会支持高风险，或为什么只是初筛假阳性。
4. 核查依据：最后再写数值例子、公式、统计自洽性或脚本核验结果。

不要用公式或术语开头。不要写成“F=(Beta/SE)^2 成立，所以……”。应写成“问题位置：Table S2 反向分析中 Ovarian cyst 第202-207行与 POF 第214-219行。发现：两块使用完全相同的6个SNP及统计量。核查依据：F=(Beta/SE)^2 自洽，说明这些数值像是某一真实GWAS结果被放到了错误表型下，而不是随机数字。”

综合评估 reason 字段中，不同类型的问题必须换行分段：数据维度、图像维度、参考文献、方法学/统计核查、其余假阳性、最终判定分别写成独立段落，避免一整段堆在一起。

正文面向审核人员展示时，尽量使用中文规则名，不要直接暴露内部检测字段名。例如写“跨表整行重复告警”“小数位一致告警”“首位数字分布告警”“数值重复使用告警”，不要直接写 cross_sheet_row_duplicate、decimal_uniformity、Benford、value_recycling。
"""

PROMPT_FOOTER = READER_FACING_TEXT_RULES + """

## 输出要求

完成复核后，你必须输出且仅输出一个JSON对象（不要输出任何其他文字、表格、标题或解释）。

JSON格式如下：
```json
{
  "doi": "论文DOI",
  "result": "高风险 或 低风险",
  "trigger": "触发规则描述",
  "image_review": "图像复核详情（中文，需符合上述Review Text Rubric要求）",
  "data_review": "数据复核详情（中文，需符合上述Review Text Rubric要求）",
  "ref_review": "参考文献复核详情（中文）",
  "methodology_review": "方法学与统计核查详情（中文，可选；无相关问题时留空字符串）",
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合理由（中文）"
}
```

重要：result 字段必须是 "高风险" 或 "低风险" 二选一。verdict 字段必须是 "建议高风险" 或 "建议低风险" 二选一（不存在"需人工复查"/"中风险"）。不要在JSON外输出任何内容。
"""

LOWRISK_VERIFIER_PROMPT_TEMPLATE = """你是一位学术论文诚信检测低风险结论核验专家。已有一位复核专家给出低风险结论；你的任务不是从零完整重写复核，而是高强度查漏，判断这个低风险是否可以放行。

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单,务必先读): {evidence_path}

## 首轮复核结论
```json
{prior_result_json}
```

## 核验任务

请优先完成以下核验：
1. 读取复核证据包 review_evidence.json，逐项查看 coverage_manifest.must_address。
2. 检查首轮复核是否真正覆盖每条 HIGH finding、确定性数据发现、主图图文一致性核查。若证据包提供 cross_sheet_reuse_groups, 按分组核验代表样例和 requires_expansion=true 的分组，不要求逐条重写原始 cross_sheet_reuse 成员。
3. 对首轮声称为假阳性或可解释的关键项进行抽查验证，尤其是图像重复、图像拼接、跨表数据复用、小数精度异常、恒等依赖和 CV=0。
4. 如果发现遗漏、证据不足、解释过度乐观、源数据/标注图无法支持低风险，必须判为高风险。
5. 只有在首轮结论覆盖完整、关键解释可验证、且你未发现新的完整性问题时，才判为低风险。

## 复核规则摘要
{skill_rules}

{reader_facing_text_rules}

## 输出要求

输出且仅输出一个JSON对象：
```json
{{
  "doi": "论文DOI",
  "result": "高风险 或 低风险",
  "trigger": "低风险二次核验结论",
  "image_review": "图像核验详情（中文，说明你核验了哪些关键项）",
  "data_review": "数据核验详情（中文，说明你核验了哪些关键项）",
  "ref_review": "参考文献核验详情（中文）",
  "methodology_review": "方法学与统计核验详情（中文，可选；无相关问题时留空字符串）",
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合理由（中文）"
}}
```

重要：result 字段必须是 "高风险" 或 "低风险" 二选一。verdict 字段必须是 "建议高风险" 或 "建议低风险" 二选一。不要在JSON外输出任何内容。
"""

JUDGE_PROMPT_TEMPLATE = """你是学术论文诚信检测的终审专家。两位独立复核专家已对以下论文完成了复核，请你基于他们的分析做出最终判定。

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单): data/output/review_v2/ 下 <DOI下划线化>_evidence.json (含 coverage_manifest.must_address 必查项)

## 专家 A 的复核结论

结果: {r1_result}
图像复核: {r1_image}
数据复核: {r1_data}
参考文献: {r1_ref}
方法学与统计核查: {r1_methodology}
判定: {r1_verdict}
理由: {r1_reason}

## 专家 B 的复核结论

结果: {r2_result}
图像复核: {r2_image}
数据复核: {r2_data}
参考文献: {r2_ref}
方法学与统计核查: {r2_methodology}
判定: {r2_verdict}
理由: {r2_reason}

## 你的任务

1. 审查两位专家分析的逻辑和证据是否充分
2. 对关键发现进行独立验证（读取标注图、源数据文件）
3. 特别关注：两位专家是否都遗漏了某些问题，或是否过于轻易地将发现标记为假阳性
4. 如果两位专家对某个问题的解释不同，你必须独立验证后判断哪个正确
5. **独立完成图文一致性核查（见下方复核规则 Step 5G）**：即使两位专家都未做，你也必须亲自渲染主图、查看图像、与源数据逐一比对，主动以"逐图 vs 其源数据"的姿态复查，而非只验证自动发现。
6. 若证据包提供 cross_sheet_reuse_groups, 按分组复核代表样例和 requires_expansion=true 的分组；原始 cross_sheet_reuse 列表只用于追溯，不要求逐条写出每个成员。
7. 若关键强证据已被确认足以支持建议高风险，可以围绕关键证据直接收敛，并简要交代其他维度是否有独立支持或暂无独立问题。
8. 做出你自己的最终判断

## 完整复核规则（与一线复核专家所用规则一致；据此校验其覆盖率与图文核查是否到位，并补做遗漏项）

{skill_rules}

## 判定标准（二选一；不存在"中风险"或"需人工复查"）

建议高风险（任一满足）：
- PHash=0 跨页匹配（排除空白页和装饰性元素）
- template_score >= 0.9 且确认为相同内容图像（非PDF提取伪影或合理复用）
- 不同细胞系/实验条件但数据完全一致
- 跨Sheet精度不匹配（2位 vs 9位小数）
- CV=0 在非统计软件输出列
- 恒等依赖 R²>0.99999

建议低风险（全部满足）：
- 所有图像匹配已通过查看标注图确认为假阳性
- 所有HIGH级别数据异常已结合论文学科和实验设计确认为已知模式
- 源数据审计未发现问题

## 语言规范
- 禁止使用：造假、篡改、伪造、编造、不端、欺诈、操纵
- 使用：异常、完整性问题、规范问题、非自然生成、重复或异常

{reader_facing_text_rules}

## 输出要求

输出且仅输出一个JSON对象：
```json
{{
  "doi": "论文DOI",
  "result": "高风险 或 低风险",
  "trigger": "触发规则描述",
  "image_review": "图像终审意见（中文，包含你的验证发现）",
  "data_review": "数据终审意见（中文，包含你的验证发现）",
  "ref_review": "参考文献终审意见（中文）",
  "methodology_review": "方法学与统计核查终审意见（中文，可选；无相关问题时留空字符串）",
  "verdict": "建议高风险 或 建议低风险",
  "reason": "终审理由（中文）"
}}
```

重要：result 必须是 "高风险" 或 "低风险" 二选一（不存在中风险）。verdict 必须是 "建议高风险" 或 "建议低风险" 二选一。不要因为复核文本留痕格式不足而单独判高风险；只有证据本身支持高风险或关键事实无法验证时才判高风险。不要在JSON外输出任何内容。不要提及"专家A"、"专家B"、"投票"、"终审"等内部流程用语。
"""


def _load_skill_rules() -> str:
    if not SKILL_MD_PATH.exists():
        log.warning("SKILL.md not found at %s, using fallback", SKILL_MD_PATH)
        return _fallback_rules()

    content = SKILL_MD_PATH.read_text(encoding="utf-8")

    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            content = content[end + 3:].lstrip()

    sections = []
    in_section = False
    for line in content.splitlines():
        if re.match(r"^### [3-9]\b", line) or (re.match(r"^#### ", line) and in_section):
            in_section = True
        elif re.match(r"^### 10\b", line) or re.match(r"^## Important", line):
            in_section = False
        if in_section:
            sections.append(line)

    if sections:
        return "\n".join(sections)

    return content


def _fallback_rules() -> str:
    return """### 图像复核规则
- PHash距离=0的跨页匹配（排除空白页）→ 建议高风险
- 模板验证得分 >= 0.9 → 建议高风险
- 不同类型图像匹配 → 假阳性

### 数据复核规则
- 已知假阳性：聚类标签、p值截断、2x2列联表、KEGG背景、ANOVA等标准误、归一化对照
- 可疑模式：跨组>80%重叠、恒等依赖(R²>0.99999)、CV=0、等差/等比数列

### 源数据审计
- 检查每个xlsx的每个Sheet，核对样本量(n)与论文声明值
- 跨图数据复用检测：不同Sheet间完全一致的数据列
- 跨Sheet小数精度比较：同类测量2位vs9位 → 强异常指标

### 判定规则
建议高风险（任一）：样本量不匹配、跨图数据复用、PHash=0跨页、模板分≥0.9、跨Sheet精度不匹配、恒等依赖
建议低风险（全部）：所有图像匹配为假阳性、所有数据异常可解释、源数据已检查无异常

### 语言要求
- 禁止使用：造假、篡改、伪造、编造、不端、欺诈、操纵
- 使用：异常、完整性问题、规范问题、非自然生成、重复或异常"""


def _build_prompt(doi: str, report_json_path: str, input_dir: str, evidence_path: str = "") -> str:
    header = PROMPT_HEADER.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=evidence_path or "(未生成,请直接审阅源数据)",
    )
    rules = _load_skill_rules()
    return header + rules + PROMPT_FOOTER


def _build_lowrisk_verifier_prompt(
    doi: str,
    report_json_path: str,
    input_dir: str,
    evidence_path: str,
    prior_result: dict,
) -> str:
    # Keep the second pass focused: verify the low-risk release gate, do not
    # ask for a full independent narrative unless the first pass looks unsafe.
    return LOWRISK_VERIFIER_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=evidence_path or "(未生成,请直接审阅源数据)",
        prior_result_json=json.dumps(prior_result, ensure_ascii=False, indent=2),
        skill_rules=_load_skill_rules(),
        reader_facing_text_rules=READER_FACING_TEXT_RULES,
    )


def _build_judge_prompt(
    doi: str, report_json_path: str, input_dir: str,
    result1: dict, result2: dict,
) -> str:
    # NOTE: skill_rules (SKILL.md §3-9) contains literal { } braces (JSON examples),
    # so it must NOT pass through str.format(). Substitute it AFTER formatting.
    body = JUDGE_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        skill_rules="@@SKILL_RULES@@",
        reader_facing_text_rules=READER_FACING_TEXT_RULES,
        r1_result=result1.get("result", ""),
        r1_image=result1.get("image_review", ""),
        r1_data=result1.get("data_review", ""),
        r1_ref=result1.get("ref_review", ""),
        r1_methodology=result1.get("methodology_review", ""),
        r1_verdict=result1.get("verdict", ""),
        r1_reason=result1.get("reason", ""),
        r2_result=result2.get("result", ""),
        r2_image=result2.get("image_review", ""),
        r2_data=result2.get("data_review", ""),
        r2_ref=result2.get("ref_review", ""),
        r2_methodology=result2.get("methodology_review", ""),
        r2_verdict=result2.get("verdict", ""),
        r2_reason=result2.get("reason", ""),
    )
    return body.replace("@@SKILL_RULES@@", _load_skill_rules())


def _ensure_evidence_bundle(doi, report_json_path, input_dir):
    """Generate the deterministic evidence bundle in a bounded child process.

    The evidence builder reads source Excel files and can hit pathological
    workbooks. Keep that failure isolated so one DOI cannot kill the API worker.
    """
    out_dir = "/opt/paper-integrity-checker/data/output/review_v2"
    os.makedirs(out_dir, exist_ok=True)
    identity = _review_state_identity(doi, report_json_path, input_dir)
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    out = os.path.join(out_dir, f"{doi.replace('/', '_')}_{digest}_evidence.json")
    if os.path.exists(out):
        return out

    def limit_child_memory():
        if EVIDENCE_MEMORY_MB <= 0:
            return
        try:
            import resource
            limit = EVIDENCE_MEMORY_MB * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except Exception:
            pass

    try:
        script = os.path.join(_SKILL_SCRIPTS, "review_evidence.py")
        cmd = [
            sys.executable,
            script,
            "--input-dir", input_dir,
            "--doi", doi,
            "--out", out,
        ]
        if report_json_path:
            cmd.extend(["--report-json", report_json_path])
        completed = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=EVIDENCE_TIMEOUT_SECONDS,
            preexec_fn=limit_child_memory if os.name == "posix" else None,
        )
        if completed.returncode != 0:
            log.error(
                "DOI=%s evidence bundle generation failed: returncode=%s stderr=%s",
                doi, completed.returncode, completed.stderr[-1000:],
            )
            return ""
        return out
    except subprocess.TimeoutExpired:
        log.error("DOI=%s evidence bundle generation timed out after %ss", doi, EVIDENCE_TIMEOUT_SECONDS)
        return ""
    except Exception as e:
        log.error("DOI=%s evidence bundle generation failed: %s", doi, e)
        return ""


def _safe_doi_slug(doi: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", doi or "unknown").strip("_")
    return slug[:120] or "unknown"


def _path_fingerprint(path: str, is_dir: bool = False) -> dict:
    p = Path(path) if path else Path()
    try:
        resolved = str(p.resolve())
    except Exception:
        resolved = str(p)

    meta = {"path": resolved, "mtime_ns": None}
    if not path:
        return meta
    try:
        stat = p.stat()
        meta["mtime_ns"] = stat.st_mtime_ns
        if is_dir:
            meta["mtime_ns"] = max(
                [stat.st_mtime_ns]
                + [child.stat().st_mtime_ns for child in p.glob("*") if child.exists()]
            )
    except Exception:
        pass
    return meta


def _review_state_identity(doi: str, report_json_path: str, input_dir: str) -> dict:
    return {
        "version": STATE_VERSION,
        "doi": doi,
        "report": _path_fingerprint(report_json_path),
        "input": _path_fingerprint(input_dir, is_dir=True),
    }


def _review_state_path(doi: str, report_json_path: str, input_dir: str) -> Path:
    identity = _review_state_identity(doi, report_json_path, input_dir)
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return REVIEW_STATE_DIR / f"{_safe_doi_slug(doi)}_{digest}.json"


def _new_review_state(doi: str, report_json_path: str, input_dir: str) -> dict:
    return {
        "identity": _review_state_identity(doi, report_json_path, input_dir),
        "stages": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_review_state(path: Path, doi: str, report_json_path: str, input_dir: str) -> dict:
    expected = _review_state_identity(doi, report_json_path, input_dir)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("identity") == expected and isinstance(state.get("stages"), dict):
            return state
        log.info("Ignoring stale review state: %s", path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not load review state %s: %s", path, e)
    return _new_review_state(doi, report_json_path, input_dir)


def _save_review_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _get_stage(state: dict, key: str):
    return (state.get("stages") or {}).get(key)


def _store_stage(path: Path, state: dict, key: str, value) -> None:
    state.setdefault("stages", {})[key] = value
    _save_review_state(path, state)


def _valid_review_result(result) -> bool:
    return isinstance(result, dict) and result.get("result") in ("高风险", "低风险")


def _load_json_file(path: str) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not load JSON file %s: %s", path, e)
        return {}


def _evidence_counts(evidence: dict) -> dict:
    counts = ((evidence or {}).get("coverage_manifest") or {}).get("counts") or {}
    return {
        "high_findings": int(counts.get("high_findings") or 0),
        "main_figures": int(counts.get("main_figures") or 0),
        "duplicate_column_pairs": int(counts.get("duplicate_column_pairs") or 0),
        "cross_sheet_reuse": int(counts.get("cross_sheet_reuse") or 0),
        "cross_sheet_reuse_groups": int(counts.get("cross_sheet_reuse_groups") or 0),
        "cross_sheet_reuse_representatives": int(counts.get("cross_sheet_reuse_representatives") or 0),
        "decimal_precision_mismatch": int(counts.get("decimal_precision_mismatch") or 0),
        "total_must_address": int(counts.get("total_must_address") or 0),
    }


def _has_strong_deterministic_evidence(evidence: dict) -> bool:
    """Whether the evidence bundle contains paper-level signals that justify
    first-pass high-risk convergence after the reviewer confirms them."""
    det = (evidence or {}).get("deterministic_findings") or {}
    if det.get("decimal_precision_mismatch"):
        return True
    if det.get("duplicate_column_pairs"):
        return True

    reuse_groups = det.get("cross_sheet_reuse_groups") or []
    for group in reuse_groups:
        count = int(group.get("count") or 0)
        reps = group.get("representatives") or []
        max_n = max([int(r.get("n_values") or 0) for r in reps] or [0])
        if group.get("requires_expansion") or count >= 3 or max_n >= 30:
            return True

    raw_reuse_count = len(det.get("cross_sheet_reuse") or [])
    if raw_reuse_count >= 10:
        return True

    for finding in (evidence or {}).get("high_findings", []) or []:
        dim = finding.get("dim")
        issue = finding.get("test") or finding.get("match_type") or finding.get("issue_type") or ""
        if dim in {"image", "image_splicing"}:
            return True
        if issue in {
            "cross_group_duplicate",
            "linear_dependency",
            "coefficient_of_variation",
            "sd_regularity",
            "value_recycling",
        }:
            return True
    return False


def _validate_review_result(
    doi: str,
    report_json_path: str,
    input_dir: str,
    result: dict,
    evidence: dict,
) -> dict:
    if not evidence:
        return _apply_coverage_validation(doi, report_json_path, input_dir, result)
    try:
        if _SKILL_SCRIPTS not in sys.path:
            sys.path.insert(0, _SKILL_SCRIPTS)
        import coverage_validator
        validated, gaps = coverage_validator.validate(result, evidence)
        if gaps:
            log.warning("DOI=%s coverage validator warning; gaps=%s", doi, gaps)
        return validated
    except Exception as e:
        log.error("DOI=%s coverage validation errored: %s", doi, e)
        out = dict(result)
        out["_coverage_status"] = "unavailable"
        out["_coverage_error"] = str(e)
        return out


def _accept_single_review(result: dict, validated: dict, evidence: dict) -> tuple[bool, str]:
    if result.get("trigger") == "review_error":
        return False, "首个复核失败"
    if not _valid_review_result(validated):
        return False, "首个复核结果无效"
    if result.get("result") == "高风险" and validated.get("result") == "高风险":
        if _has_strong_deterministic_evidence(evidence):
            return True, "首个复核已确认强确定性证据，直接收敛为高风险结论"
        return True, "首个复核已给出高风险结论"
    if validated.get("_coverage_gaps"):
        return False, "首个低风险结论存在覆盖率留痕提醒，需要二次核验"
    return False, "首个复核为低风险，需要二次核验"


def _accept_lowrisk_consensus(result1: dict, validated1: dict, result2: dict, validated2: dict) -> bool:
    return (
        result1.get("trigger") != "review_error"
        and result2.get("trigger") != "review_error"
        and validated1.get("result") == "低风险"
        and validated2.get("result") == "低风险"
        and not validated1.get("_coverage_error")
        and not validated2.get("_coverage_error")
    )


def _is_review_error(result: dict | None) -> bool:
    return isinstance(result, dict) and result.get("trigger") == "review_error"


def _usable_cached_review_result(result: dict | None) -> bool:
    return _valid_review_result(result) and not _is_review_error(result)


def _with_review_warning(result: dict, doi: str, warning: str) -> dict:
    out = dict(result)
    out["doi"] = doi
    out["_review_warning"] = warning
    if out.get("result") == "低风险":
        out["verdict"] = "建议低风险"
    elif out.get("result") == "高风险":
        out["verdict"] = "建议高风险"
    return _clean_internal_references(out)


def _single_substantive_after_process_failure(
    doi: str,
    *results: dict | None,
) -> dict | None:
    """Recover from infrastructure-only failures.

    A timeout/parse failure is not evidence of paper-level risk. If the only
    substantive review is low-risk and the other stages are review_error
    fallbacks, keep that substantive result with an audit warning instead of
    converting the paper to high-risk.
    """
    substantive = [
        r for r in results
        if _valid_review_result(r) and not _is_review_error(r)
    ]
    if len(substantive) != 1:
        return None
    return _with_review_warning(
        substantive[0],
        doi,
        "二次核验或终审因流程错误未完成；本结论未因流程错误自动上调风险。",
    )


def _is_lowrisk_result(result: dict) -> bool:
    v = (result.get("verdict") or "") + (result.get("result") or "")
    return ("低风险" in v) and ("高风险" not in (result.get("result") or ""))


def _apply_coverage_validation(doi: str, report_json_path: str, input_dir: str, result: dict) -> dict:
    """Build the deterministic evidence bundle and validate the judge's result against
    it. Coverage gaps are annotations; they do not change the review's risk
    result by themselves."""
    try:
        if _SKILL_SCRIPTS not in sys.path:
            sys.path.insert(0, _SKILL_SCRIPTS)
        import coverage_validator
        evidence_path = _ensure_evidence_bundle(doi, report_json_path, input_dir)
        if not evidence_path:
            raise RuntimeError("evidence bundle unavailable")
        bundle = _load_json_file(evidence_path)
        if not bundle:
            raise RuntimeError("evidence bundle unreadable")
    except Exception as e:
        log.error("DOI=%s evidence bundle build failed: %s", doi, e)
        out = dict(result)
        out["_coverage_status"] = "unavailable"
        out["_coverage_error"] = str(e)
        return out
    try:
        validated, gaps = coverage_validator.validate(result, bundle)
        if gaps:
            log.warning("DOI=%s coverage validator warning; gaps=%s", doi, gaps)
        return validated
    except Exception as e:
        log.error("DOI=%s coverage validation errored: %s", doi, e)
        out = dict(result)
        out["_coverage_status"] = "unavailable"
        out["_coverage_error"] = str(e)
        return out


async def run_review_single(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
) -> dict:
    """Adaptive review with per-paper resume.

    Start with one reviewer. A first-pass high-risk result is accepted
    immediately. A first-pass low-risk result always requires a second verifier;
    two low-risk results with coverage validation pass without a judge.
    """
    state_path = _review_state_path(doi, report_json_path, input_dir)
    state = _load_review_state(state_path, doi, report_json_path, input_dir)

    cached_final = _get_stage(state, "final")
    if _valid_review_result(cached_final):
        repaired = None
        if _is_review_error(cached_final):
            repaired = _single_substantive_after_process_failure(
                doi,
                _get_stage(state, "reviewer1_validated") or _get_stage(state, "reviewer1"),
                _get_stage(state, "reviewer2_validated") or _get_stage(state, "reviewer2"),
            )
        if repaired:
            _store_stage(state_path, state, "decision", {
                "flow": "cached_error_repaired",
                "reason": repaired.get("_review_warning"),
            })
            _store_stage(state_path, state, "final", repaired)
            log.info("DOI=%s review resume: repaired cached review_error final=%s", doi, repaired.get("result"))
            return _clean_internal_references(dict(repaired))
        if _is_review_error(cached_final):
            log.warning("DOI=%s review resume: ignoring cached process-failure final", doi)
        else:
            log.info("DOI=%s review resume: returning cached final=%s", doi, cached_final.get("result"))
            return _clean_internal_references(dict(cached_final))

    evidence_path = _get_stage(state, "evidence_path")
    if not evidence_path or not Path(evidence_path).exists():
        evidence_path = _ensure_evidence_bundle(doi, report_json_path, input_dir)
        _store_stage(state_path, state, "evidence_path", evidence_path)

    evidence = _load_json_file(evidence_path)
    if evidence:
        _store_stage(state_path, state, "evidence_counts", _evidence_counts(evidence))

    result1 = _get_stage(state, "reviewer1")
    if not _usable_cached_review_result(result1):
        result1 = await _run_with_retry(
            doi, report_json_path, input_dir, output_dir, agent_id=1, evidence_path=evidence_path
        )
        _store_stage(state_path, state, "reviewer1", result1)
    else:
        log.info("DOI=%s review resume: using cached reviewer1=%s", doi, result1.get("result"))

    validated1 = _get_stage(state, "reviewer1_validated")
    if not _usable_cached_review_result(validated1):
        validated1 = _validate_review_result(doi, report_json_path, input_dir, result1, evidence)
        _store_stage(state_path, state, "reviewer1_validated", validated1)

    accept, reason = _accept_single_review(result1, validated1, evidence)
    if accept:
        final = _clean_internal_references(dict(validated1))
        _store_stage(state_path, state, "decision", {"flow": "single", "reason": reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via single reviewer (%s)", doi, final.get("result"), reason)
        return final

    log.info("DOI=%s escalating review after reviewer1: %s", doi, reason)

    result2 = _get_stage(state, "reviewer2")
    if not _usable_cached_review_result(result2):
        if result1.get("result") == "低风险" and result1.get("trigger") != "review_error":
            result2 = await _run_lowrisk_verifier_with_retry(
                doi, report_json_path, input_dir, output_dir, evidence_path, validated1
            )
        else:
            result2 = await _run_with_retry(
                doi, report_json_path, input_dir, output_dir, agent_id=2, evidence_path=evidence_path
            )
        _store_stage(state_path, state, "reviewer2", result2)
    else:
        log.info("DOI=%s review resume: using cached reviewer2=%s", doi, result2.get("result"))

    validated2 = _get_stage(state, "reviewer2_validated")
    if not _usable_cached_review_result(validated2):
        validated2 = _validate_review_result(doi, report_json_path, input_dir, result2, evidence)
        _store_stage(state_path, state, "reviewer2_validated", validated2)

    if _accept_lowrisk_consensus(result1, validated1, result2, validated2):
        final = _consensus_fallback(doi, validated1, validated2)
        _store_stage(state_path, state, "decision", {"flow": "two_reviewer_low_consensus", "reason": reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via low-risk verifier consensus", doi, final.get("result"))
        return final

    if (
        result1.get("trigger") != "review_error"
        and result2.get("trigger") != "review_error"
        and validated1.get("result") == "高风险"
        and validated2.get("result") == "高风险"
    ):
        final = _consensus_fallback(doi, validated1, validated2)
        _store_stage(state_path, state, "decision", {"flow": "two_reviewer_high_consensus", "reason": reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via high-risk consensus", doi, final.get("result"))
        return final

    judge_result = _get_stage(state, "judge")
    if not _usable_cached_review_result(judge_result):
        judge_result = await _run_judge(
            doi, report_json_path, input_dir, output_dir, validated1, validated2
        )
        _store_stage(state_path, state, "judge", judge_result)
    else:
        log.info("DOI=%s review resume: using cached judge=%s", doi, judge_result.get("result"))

    if _is_review_error(judge_result):
        repaired = _single_substantive_after_process_failure(doi, validated1, validated2, judge_result)
        if repaired:
            _store_stage(state_path, state, "decision", {
                "flow": "judge_error_repaired",
                "reason": repaired.get("_review_warning"),
            })
            _store_stage(state_path, state, "final", repaired)
            log.info("DOI=%s final verdict=%s via process-failure repair", doi, repaired.get("result"))
            return repaired

    final = _validate_review_result(doi, report_json_path, input_dir, judge_result, evidence)
    final = _clean_internal_references(dict(final))
    _store_stage(state_path, state, "decision", {"flow": "judge", "reason": reason})
    _store_stage(state_path, state, "final", final)

    log.info("DOI=%s final verdict: %s", doi, final.get("result"))
    return final


async def _run_with_retry(
    doi: str, report_json_path: str, input_dir: str, output_dir: str, agent_id: int,
    evidence_path: str = "",
) -> dict:
    """Run a single review with retry on timeout/error."""
    for attempt in range(1, MAX_RETRIES + 1):
        result = await _run_single_claude(doi, report_json_path, input_dir, output_dir, agent_id, evidence_path)
        if result.get("trigger") != "review_error":
            return result
        log.warning("Reviewer %d attempt %d failed for DOI=%s, retrying...", agent_id, attempt, doi)

    log.error("Reviewer %d exhausted retries for DOI=%s", agent_id, doi)
    return _fallback_result(doi, f"Reviewer {agent_id} exhausted retries")


async def _run_lowrisk_verifier_with_retry(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    evidence_path: str,
    prior_result: dict,
) -> dict:
    """Run the second-pass low-risk verifier with retry on timeout/error."""
    for attempt in range(1, MAX_RETRIES + 1):
        result = await _run_lowrisk_verifier_claude(
            doi, report_json_path, input_dir, output_dir, evidence_path, prior_result
        )
        if result.get("trigger") != "review_error":
            return result
        log.warning("Low-risk verifier attempt %d failed for DOI=%s, retrying...", attempt, doi)

    log.error("Low-risk verifier exhausted retries for DOI=%s", doi)
    return _fallback_result(doi, "Low-risk verifier exhausted retries")


async def _run_judge(
    doi: str, report_json_path: str, input_dir: str, output_dir: str,
    result1: dict, result2: dict,
) -> dict:
    """Run the judge (Agent 3) that makes the final determination."""
    prompt = _build_judge_prompt(doi, report_json_path, input_dir, result1, result2)

    claude_cmd = _find_claude_cli()
    cmd = [
        claude_cmd,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "Bash", "Read",
        "--max-turns", "30",
    ]

    log.info("Judge starting for DOI=%s", doi)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/opt/paper-integrity-checker",
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=REVIEW_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.warning("Judge cancelled for DOI=%s", doi)
        raise
    except asyncio.TimeoutError:
        proc.kill()
        log.error("Judge timed out for DOI=%s, using reviewer consensus", doi)
        return _consensus_fallback(doi, result1, result2)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[-500:]
        log.error("Judge failed for DOI=%s (rc=%d): %s", doi, proc.returncode, err)
        return _consensus_fallback(doi, result1, result2)

    try:
        raw = json.loads(stdout.decode(errors="replace"))
        if isinstance(raw, list):
            raw = raw[0]

        text = ""
        if isinstance(raw.get("result"), str):
            text = raw["result"]
        elif isinstance(raw.get("content"), list):
            text = " ".join(
                b.get("text", "") for b in raw["content"] if isinstance(b, dict)
            )
        elif isinstance(raw.get("content"), str):
            text = raw["content"]

        inner = _extract_json_from_text(text)
        if inner and inner.get("result") in ("高风险", "低风险"):
            inner.setdefault("doi", doi)
            inner = _clean_internal_references(inner)
            log.info("Judge finished DOI=%s → %s", doi, inner.get("result"))
            return inner

        log.warning("Judge for DOI=%s: no valid JSON found (text len=%d)", doi, len(text))
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error("Judge parse error for DOI=%s: %s", doi, e)

    return _consensus_fallback(doi, result1, result2)


def _consensus_fallback(doi: str, result1: dict, result2: dict) -> dict:
    """If judge fails, fall back to reviewer consensus (both agree) or high risk."""
    r1 = result1.get("result", "高风险")
    r2 = result2.get("result", "高风险")

    repaired = _single_substantive_after_process_failure(doi, result1, result2)
    if repaired:
        return repaired

    if r1 == r2:
        primary = max([result1, result2], key=lambda r: len(r.get("data_review", "") or ""))
        primary["doi"] = doi
        primary["result"] = r1
        primary["verdict"] = "建议高风险" if r1 == "高风险" else "建议低风险"
        return _clean_internal_references(primary)

    return _fallback_result(doi, "复核结果存在分歧，按规则取建议高风险")


def _clean_internal_references(result: dict) -> dict:
    """Remove any internal process references from the result text."""
    forbidden = re.compile(r"(专家\s*[AB]|Agent\s*\d+|agent\s*\d+|投票|vote|终审)")
    for key in ["image_review", "data_review", "ref_review", "methodology_review", "verdict", "reason"]:
        val = result.get(key, "")
        if val and forbidden.search(val):
            result[key] = forbidden.sub("", val).strip()
            result[key] = re.sub(r"\s{2,}", " ", result[key])
    return result


def _extract_json_from_text(text: str) -> dict | None:
    """Extract a review JSON object from Claude's text output."""
    if not text:
        return None
    code_match = re.search(r"```json\s*\n(.*?)```", text, re.DOTALL)
    if code_match:
        fenced = code_match.group(1)
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            repaired = _extract_review_fields_tolerant(fenced)
            if repaired:
                return repaired
    brace_start = text.find("{")
    if brace_start < 0:
        return None
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[brace_start:i + 1])
                except json.JSONDecodeError:
                    repaired = _extract_review_fields_tolerant(text[brace_start:i + 1])
                    if repaired:
                        return repaired
                break
    json_end = text.rfind("}") + 1
    if json_end > brace_start:
        try:
            return json.loads(text[brace_start:json_end])
        except json.JSONDecodeError:
            repaired = _extract_review_fields_tolerant(text[brace_start:json_end])
            if repaired:
                return repaired
    return None


def _decode_tolerant_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return (
            value
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\/", "/")
        )


def _extract_review_fields_tolerant(text: str) -> dict | None:
    """Recover the flat review JSON schema when long prose contains unescaped quotes.

    The review schema is a flat object of string fields. Model output sometimes
    includes literal English quotes inside a field value, which makes the object
    invalid JSON even though the field boundaries are still clear.
    """
    keys = [
        "doi",
        "result",
        "trigger",
        "image_review",
        "data_review",
        "ref_review",
        "methodology_review",
        "verdict",
        "reason",
    ]
    key_set = set(keys)
    matches = list(re.finditer(r'"([^"]+)"\s*:\s*"', text))
    matches = [m for m in matches if m.group(1) in key_set]
    if not matches:
        return None

    out: dict[str, str] = {}
    for idx, match in enumerate(matches):
        key = match.group(1)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        raw = text[start:end].rstrip()
        raw = re.sub(r"\s*}\s*$", "", raw, flags=re.DOTALL).rstrip()
        if raw.endswith(","):
            raw = raw[:-1].rstrip()
        if raw.endswith('"'):
            raw = raw[:-1]
        out[key] = _decode_tolerant_json_string(raw)

    if out.get("result") not in ("高风险", "低风险"):
        return None
    out.setdefault("doi", "")
    out.setdefault("trigger", "")
    out.setdefault("image_review", "")
    out.setdefault("data_review", "")
    out.setdefault("ref_review", "")
    out.setdefault("methodology_review", "")
    out.setdefault("verdict", "建议高风险" if out.get("result") == "高风险" else "建议低风险")
    out.setdefault("reason", "")
    return out


async def _run_single_claude(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    agent_id: int,
    evidence_path: str = "",
) -> dict:
    """Run one Claude CLI review subprocess."""
    prompt = _build_prompt(doi, report_json_path, input_dir, evidence_path)
    return await _run_claude_prompt(doi, f"Reviewer {agent_id}", prompt, max_turns=30)


async def _run_lowrisk_verifier_claude(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    evidence_path: str,
    prior_result: dict,
) -> dict:
    """Run the focused second-pass verifier for a first-pass low-risk result."""
    prompt = _build_lowrisk_verifier_prompt(
        doi, report_json_path, input_dir, evidence_path, prior_result
    )
    return await _run_claude_prompt(doi, "Low-risk verifier", prompt, max_turns=20)


async def _run_claude_prompt(
    doi: str,
    reviewer_label: str,
    prompt: str,
    max_turns: int,
) -> dict:
    """Run a Claude CLI prompt and parse the review JSON result."""
    claude_cmd = _find_claude_cli()
    cmd = [
        claude_cmd,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "Bash", "Read",
        "--max-turns", str(max_turns),
    ]

    log.info("%s starting for DOI=%s", reviewer_label, doi)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/opt/paper-integrity-checker",
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=REVIEW_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.warning("%s cancelled for DOI=%s", reviewer_label, doi)
        raise
    except asyncio.TimeoutError:
        proc.kill()
        log.error("%s timed out for DOI=%s", reviewer_label, doi)
        return _fallback_result(doi, f"{reviewer_label} timed out")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[-500:]
        log.error("%s failed for DOI=%s (rc=%d): %s", reviewer_label, doi, proc.returncode, err)
        return _fallback_result(doi, f"{reviewer_label} failed: {err}")

    try:
        raw = json.loads(stdout.decode(errors="replace"))
        if isinstance(raw, list):
            raw = raw[0]

        text = ""
        if isinstance(raw.get("result"), str):
            text = raw["result"]
        elif isinstance(raw.get("content"), list):
            text = " ".join(
                b.get("text", "") for b in raw["content"] if isinstance(b, dict)
            )
        elif isinstance(raw.get("content"), str):
            text = raw["content"]

        inner = _extract_json_from_text(text)
        if inner and inner.get("result") in ("高风险", "低风险"):
            inner.setdefault("doi", doi)
            log.info("%s finished DOI=%s → %s", reviewer_label, doi, inner.get("result"))
            return inner

        log.warning("%s for DOI=%s: no valid JSON found (text len=%d)",
                    reviewer_label, doi, len(text))
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error("%s parse error for DOI=%s: %s", reviewer_label, doi, e)

    return _fallback_result(doi, f"{reviewer_label}: could not parse output")


def _fallback_result(doi: str, error_msg: str) -> dict:
    return {
        "doi": doi,
        "result": "高风险",
        "trigger": "review_error",
        "image_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "data_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "ref_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "verdict": "建议高风险",
        "reason": error_msg,
    }


def _find_claude_cli() -> str:
    import shutil
    for candidate in ["/usr/local/bin/claude", "/usr/bin/claude"]:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("claude")
    if found:
        return found
    raise FileNotFoundError("claude CLI not found in PATH")
