from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from api.config import BASE_DIR, REVIEW_DIR, REVIEW_SKILL_SCRIPTS, REVIEW_TIMEOUT_SECONDS
from modules.doi_utils import normalize_doi
from modules.review_public_wording import PUBLIC_WRITING_RULES
from api.services.review_runtime import (limited_evidence, numeric_subprocess_env,
                                          paper_deadline, subprocess_budget)

log = logging.getLogger(__name__)

_SKILL_SCRIPTS = str(REVIEW_SKILL_SCRIPTS)

# A failed CLI session already leaves a durable checkpoint. Re-running the
# same full investigation immediately only doubles latency and competes with
# the next paper for the same model/process slot. Recovery is resumed from the
# checkpoint by the task retry path instead.
MAX_RETRIES = int(os.environ.get("REVIEW_MAX_RETRIES", "1"))
STATE_VERSION = 4
REVIEW_STATE_DIR = Path(os.environ.get(
    "REVIEW_STATE_DIR",
    str(REVIEW_DIR / "state"),
))
EVIDENCE_TIMEOUT_SECONDS = int(os.environ.get("REVIEW_COVERAGE_TIMEOUT_SECONDS", "6000"))
EVIDENCE_MEMORY_MB = int(os.environ.get("REVIEW_COVERAGE_MEMORY_MB", "2048"))
SOURCE_DATA_AUDIT_TIMEOUT_SECONDS = int(os.environ.get("SOURCE_DATA_AUDIT_TIMEOUT_SECONDS", "2700"))
LLM_EVIDENCE_MAX_BYTES = int(os.environ.get("REVIEW_LLM_EVIDENCE_MAX_BYTES", "60000"))
MAX_PROMPT_FIELD_CHARS = int(os.environ.get("REVIEW_MAX_PROMPT_FIELD_CHARS", "4000"))
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "MiniMax-M3.1-Flash").strip()
REVIEW_FALLBACK_MODEL = os.environ.get("REVIEW_FALLBACK_MODEL", "MiniMax-M3").strip()
REVIEW_CLAUDE_SETTINGS_FILE = os.environ.get(
    "REVIEW_CLAUDE_SETTINGS_FILE",
    "/home/library/.claude/settings-mm.json",
).strip()
# A full review still has to inspect every evidence dimension, but the evidence
# bundle has already done the expensive inventory work.  Keeping the initial
# session bounded prevents a single paper from spending most of the batch
# window repeatedly re-reading the same workbook; unresolved coverage is
# repaired from the checkpoint.
REVIEW_MAX_TURNS = int(os.environ.get("REVIEW_MAX_TURNS", "36"))
# The verifier and synthesis prompts receive deterministic evidence plus the
# first-pass draft, so they need fewer turns than the initial evidence review.
LOWRISK_VERIFIER_MAX_TURNS = int(os.environ.get("REVIEW_LOWRISK_MAX_TURNS", "16"))
JUDGE_MAX_TURNS = int(os.environ.get("REVIEW_JUDGE_MAX_TURNS", "12"))
COVERAGE_REPAIR_MAX_TURNS = int(os.environ.get("REVIEW_REPAIR_MAX_TURNS", "8"))
# A clean evidence bundle does not need two independent model narratives.  The
# first pass has already checked every required dimension and the deterministic
# gate has no actionable signal to challenge; running a second full CLI session
# in that case only adds latency without adding evidence.  Keep an opt-out for
# deployments that require the historical two-pass policy.
FAST_CLEAN_LOWRISK = os.environ.get("REVIEW_FAST_CLEAN_LOWRISK", "1") != "0"
DETERMINISTIC_FASTPATH_ENABLED = os.environ.get("REVIEW_DETERMINISTIC_FASTPATH", "1") != "0"
DETERMINISTIC_FASTPATH_MIN_POINTS = int(os.environ.get("REVIEW_DETERMINISTIC_FASTPATH_MIN_POINTS", "1000"))
DETERMINISTIC_FASTPATH_SAMPLE_LIMIT = int(os.environ.get("REVIEW_DETERMINISTIC_SAMPLE_LIMIT", "10"))
DETERMINISTIC_FASTPATH_VERSION = "15"
REVIEW_RESUME_RESULTS = os.environ.get("REVIEW_RESUME_RESULTS", "0") == "1"
_SUMMARY_CODE_VERSION = "3"
_REVIEW_CLI_CONTEXT = contextvars.ContextVar("review_cli_context", default=None)


def _review_subprocess_timeout_seconds() -> int | None:
    value = os.environ.get("REVIEW_SUBPROCESS_TIMEOUT_SECONDS", "").strip().lower()
    if value in {"0", "-1", "none", "no", "false", "unlimited"}:
        return None
    if value:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except ValueError:
            log.warning("Invalid REVIEW_SUBPROCESS_TIMEOUT_SECONDS=%r; using default", value)
    return REVIEW_TIMEOUT_SECONDS


def _claude_subprocess_env() -> dict[str, str]:
    """Keep batch reviews independent of CLI maintenance network traffic.

    Review subprocesses only use the built-in Bash/Read tools.  Marketplace
    installation and updater traffic can block on GitHub without contributing
    to a review, so disable those background activities for worker processes.
    Explicit administrator-provided values still take precedence.
    """
    env = numeric_subprocess_env()
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    env.setdefault("CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL", "1")
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.setdefault("PAPER_REVIEW_GUARD_READONLY_CELL", "1")
    guard_path = str(BASE_DIR / "api" / "reviewer_runtime")
    current_pythonpath = env.get("PYTHONPATH", "")
    path_entries = [entry for entry in current_pythonpath.split(os.pathsep) if entry]
    if guard_path not in path_entries:
        env["PYTHONPATH"] = os.pathsep.join([guard_path, *path_entries])
    return env


class EvidencePaths(NamedTuple):
    llm_summary: str
    full: str

PROMPT_HEADER = """你是一位学术论文诚信检测复核专家。请对以下论文的检测结果进行严格复核。

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单,务必先读): {evidence_path}

证据包(review_evidence.json)已由确定性脚本预先生成,包含:源数据各sheet列的预分类(自变量/统计输出/标准差/测量值)、确定性发现(重复列对/跨表复用/小数精度不一致)、CIF晶体结构文件及其异常指标、必须逐一查看的主图清单、以及每条HIGH发现。其中 coverage_manifest.must_address 列出本次复核**必须逐项给出结论**的条目。请在文字中留下明确核查痕迹；覆盖率校验会记录遗漏提醒，但不会把单纯格式留痕不足直接等同于论文高风险。请先读证据包,再展开复核。

若证据包包含 deterministic_findings.cif_review，必须打开对应 CIF 核验告警字段，并与论文正文或补充材料中的化学式、空间群、晶胞参数、Z值、密度、R1/wR2和CCDC编号逐项对照。高R因子或缺失字段本身只代表结构质量或规范性线索；只有文件不可解析、结构参数自相矛盾、占位率/坐标明显不可能，或与论文报告实质不一致时，才可作为高风险依据。

{soft_explain_guardrails}

若证据包包含 deterministic_findings.cross_sheet_reuse_groups, 复核覆盖单位是“分组”而不是原始 cross_sheet_reuse 的每一条成员。你必须查看每个分组的代表样例、最高风险样例和 requires_expansion=true 的分组；原始 cross_sheet_reuse 列表用于追溯, 不需要逐条写成 100+ 段。若一个分组经代表样例确认属于同一类合理复用或同一类完整性问题, 可以按分组给出结论并说明抽查/扩展依据。

若已确认强确定性证据本身足以支持“建议高风险”（例如跨图/跨文件大量整列复用、小数精度跨sheet显著不一致、重复列对、恒等依赖或真实图像重复/拼接），可以缩短该证据的重复论证，但不得提前结束复核。你仍须逐一核查证据包中图像、数据、参考文献、研究设计/统计四个维度的每类 HIGH 线索，并分别给出有事实依据的维度结论；没有独立问题的维度也要说明实际核查了什么以及为何排除。

## 复核规则

请严格按照以下复核规则执行（来自运行时复核规则配置）：

"""

READER_FACING_TEXT_RULES = PUBLIC_WRITING_RULES + """

## 面向审核人员的写法要求（必须遵守）

复核报告会直接给非专业审核人员阅读。每个确认问题必须先交代位置和现象，再解释专业核验：
1. 问题位置：先写清楚哪个图、哪张表、哪个补充文件、sheet、行列或字段。
2. 发现了什么：用平实中文说明可见问题，例如“两块数据逐格完全相同”“论文图中两行结果完全重合”。
3. 为什么重要：说明为什么这会支持高风险，或为什么只是初筛假阳性。
4. 核查依据：最后再写数值例子、公式、统计自洽性或脚本核验结果。

不要用公式或术语开头。不要写成“F=(Beta/SE)^2 成立，所以……”。应写成“问题位置：Table S2 反向分析中 Ovarian cyst 第202-207行与 POF 第214-219行。发现：两块使用完全相同的6个SNP及统计量。核查依据：F=(Beta/SE)^2 自洽，说明这些数值像是某一真实GWAS结果被放到了错误表型下，而不是随机数字。”

若最终结论为高风险，至少一个确认问题必须逐项使用以下五个中文标签，标签不可省略或改名：
“问题位置：”“问题类型：”“发现：”“为什么重要：”“核查依据：”。
这是最终报告的结构化输入契约；只有综合性叙述而缺少任一标签时，报告将拒绝生成。

综合评估 reason 字段中，不同类型的问题必须换行分段：数据维度、图像维度、参考文献、研究设计/统计核查、其余假阳性、最终判定分别写成独立段落，避免一整段堆在一起。

正文面向审核人员展示时，尽量使用中文规则名，不要直接暴露内部检测字段名。例如写”跨表整行重复告警””小数位一致告警””首位数字分布告警””数值重复使用告警”，不要直接写 cross_sheet_row_duplicate、decimal_uniformity、Benford、value_recycling。

绝对禁止在输出中出现以下内部术语或引用：
- 不要引用规则文档名或章节号（如 RUNTIME_RULES、SKILL.md、§2.1、§6 等）
- 不要输出内部变量名（如 md5=、seam_columns、crossref_score=、matched_doi=、ref_text=、inliers、partial_region 等）
- 不要输出文件校验哈希值（如 md5=53a3d36a）
- 如需引用检测依据，用平实中文描述（如”同一文件的两份副本经校验完全一致”而非”md5=53a3d36a”）

使用编号列表时，序号必须连续（1,2,3...），不得跳号。输出前检查一遍编号连续性。
"""

SOFT_EXPLAIN_GUARDRAILS = """
## 软解释反证约束（来自 2026-06-23 s41467-023-42025-8 漏判教训，强制遵守）

你必须主动检验每条异常线索，也必须核实合理解释是否成立。verdict 字段只能是"建议高风险"或"建议低风险"；核验经过写入各维度意见和 reason，不得把过程句写入 verdict。

以下清单按论文类型和实际提供的材料执行：没有实验样本量的理论/算法论文不套用生物重复数；未提供数值型源数据时，不得反复搜索不存在的 xlsx、强行将所有曲线数字化或编造源数据检验。须核查可用正文、表格、图注和图像，明确不可核查范围；材料不足本身不构成论文风险，关键矛盾仍无法判断时如实返回 review_error。

### 必须强制核查的硬性清单（漏一项即视为复核不到位）

1. **逐图 n 核对（不可跳过）**：对论文主图 Fig.1 至 Fig.7+ **每一张** 都要明确回答：
   - 论文图注/研究设计声明的 n=几？生物重复数？技术重复数？
   - 对应 Source Data（xlsx）该图实际有多少个数据点（行/列）？
   - n 是否一致？若不一致，写出具体差异（例：论文声明 n=9 但 xlsx 仅 8 行）。
   **任何一张主图未做此核对 → 复核不到位 → 不得判低风险。**

2. **逐图 vs Source Data 比对（不可跳过）**：对每个主图亲自渲染 PDF 页面 → Read 查看图像 → 读取对应 xlsx sheet → 逐图比对柱高、峰位、曲线趋势、量级、坐标轴范围。**仅查看 annotation 标注图不算完成。**

3. **跨图数据对比**：依据确定性证据包覆盖实际提供的、可比较的测量序列，核查重复候选及每个 requires_expansion 分组，必要时扩展。已有全量扫描结果应抽查原值和列语义，不要每轮重做全量扫描。不同物理量或不同图形不强行逐对拟合；没有数值源数据时，不要求将全部图片数字化后穷举相关性。高相关性只是线索，须确认独立样本/条件和排除合理复用后才能定性。

4. **跨处理组重复值核验（不可跳过）**：对论文中声称"不同处理组"或"独立实验"的数据列，剥除"基线/对照"后，**直接 r/slope 测量非基线列对的等同性**。Rep1=Rep3 逐位全等+Rep2 正常+多组同时 = 合成痕迹，不接"高存活对照"软解释。

5. **末位数字 / SD 规律性（不可跳过）**：必须运行 forensic_checks.py scan-digits。同一列 ≥60% 值末 4 位完全相同 = 合成数据"末位填充"。SD/SEM=0 但 raw 离散+Avrg 反算正确 = 选择性遗漏。Benford χ² 偏离需在测量值列（非聚类 ID / p 值 / 整数社区编号）上才有意义。

6. **研究设计/图注与源数据的矛盾核查（不可跳过）**：论文 Methods/图注声明的实验设计（独立实验数、处理条件、剂量、时间点）必须与 xlsx sheet 标题/列名/数据组织完全一致。若声明"3 次独立重复"但 xlsx 中 9 个重复全是同一组测量 = 高风险。

### 大表读取性能约束（必须遵守）

- 使用 `openpyxl` 的 `read_only=True` 读取工作簿时，禁止在循环中反复调用 `ws.cell(row, column)`；该方式会从头扫描 XML，在大表上形成近似二次复杂度。
- 全列或多行核查必须使用一次性的 `ws.iter_rows(..., values_only=True)` 流式遍历，并在同一遍中计算所有需要的计数、相关性和样例。
- 同一工作表不得并行启动内容重叠的全表扫描；已有确定性证据已覆盖的统计不得再次穷举重算，只需按证据摘要抽样复核。

### 软解释黑名单（出现以下解释必须配硬约束数字反证，否则视为辩护不是核验）

下列软解释**单独不能维持低风险**，必须配具体数字（n、r、slope、p 值、列名、列单位、跨文件 cross-check）才能作为反证成立：

- "标准格式" / "MaxQuant 导出格式" / "质谱学定义特征" — 配 215 行中多少行满足？定义引用出处？**Razor+unique 与 Unique 在所有蛋白都相等的真实场景：仅当蛋白只有一个 unique peptide 时，**比例多少？**与"78% 蛋白"明显矛盾 → 软解释不成立。**
- "实验惯例" / "归一化为 100% 是惯例" — 该图是否声明 n=9 但实际归一化列只有 8 个不同值？**n 永远等于数据点总数，n 不符 = 高风险。**
- "测量分辨率有限" — 同类测量在 xlsx 其他列是否精度更高？同类数据中只有 1-2 个值重复 N 次是分辨率，**所有值都是同一小数末位是合成。**
- "同一患者分入多组临床分类" — 患者 ID 维度去重后是否真有那么多独立测量？**n 仍等于独立患者数。**
- "1/3 数学必然" / "标准存储格式" — 该列的列名/单位是否支持"被 3 整除"的解释？3 个生物重复×2/3 末位 = 0.6667 是计算结果，**3 个重复完全相同的末 4 位是合成。**
- "Benford 偏离正常" — 该列是否是测量值列？非衍生量列偏离 Benford 是否仍可接受？**网络中心性/MD 轨迹/Benford 适用问题，测量值列偏离不接。**
- "Intensity/iBAQ 大整数" — 其他同类测量列是否也是大整数？还是混合了浮点？**部分列大整数+部分列浮点是异质，部分列全是 10^10 大整数是格式特征。**

### 反证失败 = 维持高风险

任何软解释如未配硬约束反证 → 视为辩护不成立 → 该 HIGH finding 维持 → 整体 verdict 倾向高风险。

### 真实案例 (s41467-023-42025-8, 2026-06-23)

复核误判低风险，GPT 复核翻盘为高风险。漏查的硬证据：
- **Fig. 2a n 不符**：论文声明 n=9 但 xlsx 实际只有 8 行数据
- **Fig. 7b 跨处理组重复值**：不同处理组非基线列完全相同（r=1.0）
漏判原因：用了 6 个软解释（"惯例"、"标准格式"、"分辨率"、"1/3 数学"、"大整数存储"、"Benford 偏离正常"），**未逐图核对 n，未做跨处理组非基线列对的 r/slope 测量**。
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
  "image_review": "图像复核详情（中文，需符合上述面向审核人员的写法要求）",
  "data_review": "数据复核详情（中文，需符合上述面向审核人员的写法要求）",
  "ref_review": "参考文献复核详情（中文）",
  "methodology_review": "研究设计与统计核查详情（中文，必须填写；无独立问题时说明核查了哪些设计/统计事实以及为何排除异常）",
  "evidence_images": [{"dimension": "image|data|ref|methodology", "issue_index": 1, "path": "仅填写确认问题对应的本地证据图路径", "caption": "证据截图说明（中文）"}],
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估（中文）——每个确认的高风险问题必须带具体位置：图像写图号/页码，数据写文件名/sheet/列/数值举例，参考文献写第几条引用。不可只写'存在数据复用'等笼统描述。",
  "dimension_verdicts": {
    "image": "建议高风险 或 建议低风险",
    "data": "建议高风险 或 建议低风险",
    "ref": "建议高风险 或 建议低风险",
    "methodology": "建议高风险 或 建议低风险"
  }
}
```

重要：result 字段必须是 "高风险" 或 "低风险" 二选一。verdict 字段必须是 "建议高风险" 或 "建议低风险" 二选一（不存在"需人工复查"/"中风险"）。不要在JSON外输出任何内容。
"""

COMPACT_REVIEW_RULES = """### 复核核心规则
目标：基于可核验事实判断“建议高风险”或“建议低风险”。初检只是线索，不能把初检高风险本身当证据。

### 风险分级边界（必须遵守）
论文中的明确笔误必须保留并给出更正建议。普通拼写、引用编号、作者/页码等书目字段、排版格式，以及结合正文上下文可以解释的四舍五入、工程近似或单位呈现差异，不得单独升级为“建议高风险”。但如果笔误直接改变了数值、对象、实验条件、图表对应关系、计算公式或核心结论，且不能由上下文解释，则属于严重笔误，可以支持“建议高风险”。

若没有这类严重笔误或其他数据、图表、计算、样本量、实验条件或核心结论层面的实质性问题，整体必须为“建议低风险”；报告仍应保留普通笔误、引用编号和格式勘误。不要为了维持高风险而把可直接更正的普通文字问题改写成数据异常，也不要把可由上下文解释的近似扩大为结果失效。

必须先读复核证据摘要 JSON。摘要中的 duplicate_column_pair_groups、cross_sheet_reuse_groups、decimal_precision_mismatch、high_findings 是本次复核的覆盖单位；完整 evidence 文件只用于必要时追溯。

数据复核优先级：
1. 先判断重复列/跨表复用中的相同列是自变量/轴变量，还是测量值/因变量。
2. 共享时间、浓度、电位、波数、R/k 轴等自变量通常是正常；不同样本/条件的测量值逐值相同才支持高风险。
3. 对分组告警按代表样例和最高风险样例核查；同一类原因覆盖整组即可，不要逐条写 50 段。
4. 小数精度、CV=0、恒等依赖、末位数字异常只有在列名和上下文不支持正常解释时才支持高风险。
5. 图文一致性核查（必须执行，不可跳过）：用 fitz.open() 渲染论文 PDF 中至少 2-3 张主图页面为 PNG（不足则全部），用 Read 查看渲染图像；有数值源数据时读取对应数据，核对关键数值、趋势、量级和点数；未提供时核对正文/表格/图注，并明确源数据比对未能执行，不得循环搜索不存在的 xlsx。已有渲染图可直接使用，不能只看 annotation 标注图。后续核验独立抽查关键判断并补齐遗漏项，综合判定只补查分歧和未覆盖项，不要求每个阶段重复全量渲染与扫描。
6. 若存在 CIF：打开文件核验晶胞、空间群、化学式、占位率、位移参数、密度和精修指标，并与论文正文或补充材料逐项对照；高R因子等质量指标不能脱离结构矛盾单独判高风险。

图像复核：真实跨页图像重复、相同实验图像复用、明确拼接不连续支持高风险；坐标轴模板、示意图、PDF提取伪影、同图不同嵌入版本通常是假阳性，但需说明依据。
若核查后仅剩少量图像重复线索，且未发现数据、图文一致性、参考文献或方法学独立问题，必须从严视觉确认其是否为真实实验图像复用；不能仅凭自动匹配或少量可解释的版式/模板相似轻易升级为高风险。

参考文献复核：CrossRef 超时/低置信度/解析碎片不构成引用异常；只有完整可读条目的 DOI、题名、作者或期刊实质不一致才支持高风险。

判定边界补充：文献编号错指、页码/作者名单等书目勘误、排版格式错误和普通笔误，应写入复核意见但不得单独触发高风险；可由论文前后文、明确的取整说明或工程近似解释的数值差异，也不得单独触发高风险。若笔误改变数值、对象、条件、图表对应关系、公式或核心结论，且上下文无法解释，则按严重笔误处理。若没有严重笔误或其他数据、图表、计算、样本量、实验条件或核心结论层面的独立实质问题，整体结论必须为建议低风险。

判定：
- 建议高风险：确认存在真实图像重复/拼接、不同实验测量值逐值复用、主图样本量不符、图文/标签矛盾、同类测量强精度异常且无法解释、CIF结构参数自相矛盾或与论文实质不一致、完整引用实质不一致等任一问题。
- 建议低风险：所有高价值告警均被代表样例或分组核查覆盖，且有具体、可验证的正常解释。

写法：每个确认问题写清位置、现象、为什么重要、核查依据。低风险也要说明核查了哪些分组和代表样例。禁止使用”造假、篡改、伪造、编造、不端、欺诈、操纵”。

跨工作表/跨图引用同一测量数据：正文主图与补充材料引用完全相同的实验数据列属于标准学术做法，不构成高风险。但不同实验（不同细胞系、不同处理条件、不同样本）声称独立测量却共享因变量/测量值列，则支持高风险。"""

DETERMINISTIC_AGENT_PROMPT_TEMPLATE = """你是一位学术论文诚信检测复核专家。下面的证据已经由确定性脚本从原始 Excel 中抽样核验，覆盖最多 10 个相关工作表/位置。你的任务是结合原始 Excel、论文 PDF 语义和这些已核验样本，判断是否足以支持最终复核结论。

重要约束：
1. 可以使用 Bash/Read 工具，但必须控制范围：先读取下方抽样包中列出的工作表/列和论文 PDF 中相关图注/上下文，不要一开始全表全图穷举。
2. 初检只是线索，不能把初检高风险本身当证据；必须亲自用原始 Excel 核对抽样包列出的至少一个首段/中段/末段样例。
3. 必须结合论文语义判断：这些重复列是否对应同一实验/同一数据在主图与补充图中的合理重复呈现，还是不同分组、不同工作表、不同图/条件声称独立但测量值逐值相同。
4. 如果第一批抽样证据已经确认不同分组或不同工作表的测量值列存在上千到数万个数据点逐值一致，并且论文语义不能解释为共享横轴、同源主补图呈现或同一数据的合理复用，可以直接收敛为建议高风险。
5. 如果第一批抽样证据不足以支持高风险，继续从完整证据/原始 Excel 中读取下一批 8-10 个相关工作表或位置；仍不足时再判低风险或说明关键证据无法支持高风险。
6. 如果这些列是时间、浓度、波数、电位等横轴/自变量，则不能作为高风险依据；如果是 y、Intensity、signal 等测量值/因变量，且语义上代表不同条件/图表结果，则可以作为关键证据。
7. 数据强证据只决定数据维度，不得据此把图像、参考文献或研究设计维度默认写成低风险。必须打开完整证据包，逐一复核所有其他维度的 HIGH 线索；没有 HIGH 线索时也要写明实际核查范围。
8. 笔误必须保留在报告中并给出更正建议。普通拼写、引用编号、书目字段、排版格式和可由上下文解释的近似只作勘误，不得单独升级；只有改变数值、对象、实验条件、图表对应、公式或核心结论且无法解释的严重笔误，才可支持有风险结论。

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}

## 已抽样核验证据
```json
{verified_evidence_json}
```

建议工具用法：
- 用 Python/openpyxl 读取已抽样核验证据中的工作表和列，确认首段/中段/末段是否确实逐值相同。
- 用 Python/fitz 或 pdftotext 读取 manuscript.pdf 中相关图号、补充图号、sheet 名附近的图注和方法上下文，判断这些 sheet 是否可能只是同一数据的主图/补充图重复呈现。
- 不要把工具输出原样粘贴进报告；只写核查结论和关键数值样例。

## 输出要求
输出且仅输出一个 JSON 对象：
```json
{{
  "doi": "论文DOI",
  "result": "高风险 或 低风险",
  "trigger": "触发规则描述",
  "image_review": "图像复核详情。若图像不是主要依据，写明未作为主要判定依据。",
  "data_review": "数据复核详情。必须写明抽样核查了哪些工作表/列/数值段，为什么支持或不支持高风险。",
  "ref_review": "参考文献复核详情。若不是主要依据，写明不作为主要判定依据。",
  "methodology_review": "研究设计与统计核查详情（必须填写；无独立问题时说明核查范围及排除依据）。",
  "evidence_images": [{{"dimension": "image|data|ref|methodology", "issue_index": 1, "path": "仅填写确认问题对应的本地证据图路径", "caption": "证据截图说明（中文）"}}],
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估。分段写：数据维度、图像维度、参考文献、最终判定。",
  "dimension_verdicts": {{
    "image": "建议高风险 或 建议低风险",
    "data": "建议高风险 或 建议低风险",
    "ref": "建议高风险 或 建议低风险",
    "methodology": "建议高风险 或 建议低风险"
  }}
}}
```

语言要求：禁止使用“造假、篡改、伪造、编造、不端、欺诈、操纵”。不要输出内部流程名称、角色称谓或流程协调信息。
"""

LOWRISK_VERIFIER_PROMPT_TEMPLATE = """你是一位学术论文诚信检测低风险结论核验专家。已有一份复核给出了低风险结论；你的任务是采用反证式核查方法，主动寻找它遗漏或过度乐观的地方。

你不需要从零重做全量复核，但必须带着怀疑立场审视首轮结论的每一个关键判断。最终结论只依据可核验的事实；不能因为低风险文本写得不充分，就在没有实质证据时直接判高风险。

{original_review_instruction}

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单,务必先读): {evidence_path}

{soft_explain_guardrails}

## 待核验低风险结论
```json
{prior_result_json}
```

## 核验任务

工作假设：这份低风险结论可能有误。按以下步骤尝试推翻低风险结论；若推翻失败且关键解释均经你独立验证成立，才维持低风险。

### A. 覆盖率审查 — 首轮是否做全了？
1. 读取 review_evidence.json，逐项核对 coverage_manifest.must_address。
2. 首轮是否覆盖了每条 HIGH finding、每个确定性数据发现（duplicate columns / cross-sheet reuse groups / decimal precision mismatch）、每个主图的图文一致性核查？
3. 若证据包提供 cross_sheet_reuse_groups，按分组检查代表样例和 requires_expansion=true 的分组。
4. **任何 must_address 项未被首轮覆盖 → 你必须亲自核查该项。**

### B. 假阳性压力测试 — 首轮的排除理由站得住吗？
对首轮标记为假阳性的每个关键项，主动尝试反驳：
1. **图像重复/拼接**：亲自用 Read 工具查看标注图。首轮说"模板匹配"或"不同类型图像"，但图像内容真的不同吗？轴标签、数据点、曲线形状是否实质相同？
2. **跨表数据复用**：首轮说"共享对照""共享横轴"或"同一实验不同展示"——**先打开 xlsx 查看共享列的列头名称、数值范围和单位**，确认它是自变量/横轴（时间点、浓度、剂量）还是因变量/测量值（吸光度、细胞数、表达量）。若共享列实际是测量值，"共享横轴"解释不成立。同时检查论文原文是否声称这些是独立实验，以及处理组数据是否也相同。
3. **小数精度异常**：首轮说"不同仪器输出"或"汇总均值"，但同类测量出现 2 位 vs 9 位精度真的能用这个解释吗？
4. **恒等依赖 / CV=0**：首轮说"分类变量"或"标准统计输出"，但该列的列名和上下文真的支持这个解释吗？
5. **样本量**：首轮说"n 一致"，但是否覆盖了 Fig.1-Fig.6 全部主图？还是只查了部分就下了结论？

### C. 独立发现 — 检查前序复核可能遗漏的问题
1. **图文一致性（必须执行）**：独立查看至少 2-3 张关键主图（不足则全部），已有 PDF 渲染图可复用；优先核查首轮遗漏、可疑或影响结论的图。有数值源数据则核对相应数值；无源数据则核对正文、表格与图注并明确限制。不得将无 xlsx 当成流程无法结束的理由，也不得声称完成未执行的源数据检验。仅查看 annotation 标注图不算完成此步骤。
2. **跨图标签矛盾**：同一数据列在不同图中是否使用了矛盾的样品/条件标签？
3. **末位数字 / SD 规律性**：首轮是否运行了 forensic_checks.py scan-digits？若未运行，你来运行并检查结果。
4. **CIF结构一致性**：若证据包列出 CIF，打开文件复算关键参数，并核对论文中的化学式、空间群、晶胞、Z、密度、R1/wR2及CCDC编号；不得只复述初审告警。

### D. 判定
- **改判高风险**（任一成立）：
  - 首轮有 must_address 项未覆盖，且你核查后发现该项确实存在问题
  - 首轮的假阳性排除理由经你验证不成立（读了标注图/源数据后发现内容确实相同或异常）
  - 你独立发现了首轮未提及的数据完整性问题（图文不一致、跨图复用、样本量不符等）
  - 首轮的排除理由缺乏具体证据，且你亲自核查后无法验证其合理性，或关键文件/证据无法读取
- **维持低风险**（全部成立）：
  - 你尝试反驳了每个关键假阳性判断，但首轮的解释经你独立验证确实成立
  - 你亲自做了图文一致性核查，未发现不一致
  - 你未发现任何首轮遗漏的新问题
  - 你能具体说明你验证了哪些项、用了什么方法、看到了什么

## 复核规则
{skill_rules}

{reader_facing_text_rules}

## 输出要求

输出且仅输出一个JSON对象：
```json
{{
  "doi": "论文DOI",
  "result": "高风险 或 低风险",
  "trigger": "低风险核验结论",
  "image_review": "图像核验详情（中文，说明你验证了哪些假阳性判断、亲自核查了什么）",
  "data_review": "数据核验详情（中文，说明你验证了哪些假阳性判断、独立核查了什么）",
  "ref_review": "参考文献核验详情（中文）",
  "methodology_review": "研究设计与统计核查详情（中文，必须填写；无独立问题时说明核查了哪些设计/统计事实以及为何排除异常）",
  "evidence_images": [{{"dimension": "image|data|ref|methodology", "issue_index": 1, "path": "仅填写确认问题对应的本地证据图路径", "caption": "证据截图说明（中文）"}}],
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估（中文）——每个确认的高风险问题必须带具体位置（图号/文件/sheet/列/数值）",
  "dimension_verdicts": {{
    "image": "建议高风险 或 建议低风险",
    "data": "建议高风险 或 建议低风险",
    "ref": "建议高风险 或 建议低风险",
    "methodology": "建议高风险 或 建议低风险"
  }}
}}
```

重要：result 字段必须是 "高风险" 或 "低风险" 二选一。verdict 字段必须是 "建议高风险" 或 "建议低风险" 二选一。不要在JSON外输出任何内容。不要出现内部流程名称、角色称谓或流程协调信息。
"""

JUDGE_PROMPT_TEMPLATE = """你是学术论文诚信检测的综合复核专家。已有两份相互独立的复核结论，请你基于证据做出最终判定。

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单): {evidence_path}

{soft_explain_guardrails}
{original_review_instruction}
{coverage_repair_instruction}

## 复核结论一

结果: {r1_result}
图像复核: {r1_image}
数据复核: {r1_data}
参考文献: {r1_ref}
研究设计与统计核查: {r1_methodology}
判定: {r1_verdict}
理由: {r1_reason}

## 复核结论二

结果: {r2_result}
图像复核: {r2_image}
数据复核: {r2_data}
参考文献: {r2_ref}
研究设计与统计核查: {r2_methodology}
判定: {r2_verdict}
理由: {r2_reason}

## 你的任务

1. 审查两份复核分析的逻辑和证据是否充分
2. 对关键发现进行独立验证（读取标注图、源数据文件）
3. 特别关注：两份复核是否都遗漏了某些问题，或是否过于轻易地将发现标记为假阳性
4. 如果两份复核对某个问题的解释不同，你必须独立验证后判断哪个正确
5. **图文一致性仲裁**：对存在分歧、没有具体核验依据或尚未覆盖的主图，亲自查看图像并对照可用源数据/正文/图注；两份意见均有明确依据且无矛盾的已核查项目，保留其事实与定位，不再全量渲染、扫描和重算。所有 HIGH 线索仍须逐项有结论，未核查项不得写成已完成。
6. 若证据包提供 cross_sheet_reuse_groups, 按分组复核代表样例和 requires_expansion=true 的分组；原始 cross_sheet_reuse 列表只用于追溯，不要求逐条写出每个成员。
7. 若关键强证据已被确认足以支持建议高风险，可以缩短该证据的重复论证，但必须继续逐类核查其他维度的 HIGH 线索。不得仅因数据维度已经足够判高风险，就把图像、参考文献或研究设计维度默认写成低风险。
8. 做出你自己的最终判断
{original_review_task}

## 完整复核规则（与前序复核所用规则一致；据此校验其覆盖率与图文核查是否到位，并补做遗漏项）

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
  "image_review": "图像复核意见（中文，包含你的验证发现）",
  "data_review": "数据复核意见（中文，包含你的验证发现）",
  "ref_review": "参考文献复核意见（中文）",
  "methodology_review": "研究设计与统计核查意见（中文，必须填写；无独立问题时说明核查范围及排除依据）",
  "evidence_images": [{{"dimension": "image|data|ref|methodology", "issue_index": 1, "path": "仅填写确认问题对应的本地证据图路径", "caption": "证据截图说明（中文）"}}],
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估（中文）——每个确认的高风险问题必须带具体位置（图号/文件/sheet/列/数值）",
  "dimension_verdicts": {{
    "image": "建议高风险 或 建议低风险",
    "data": "建议高风险 或 建议低风险",
    "ref": "建议高风险 或 建议低风险",
    "methodology": "建议高风险 或 建议低风险"
  }}
}}
```

重要：result 必须是 "高风险" 或 "低风险" 二选一（不存在中风险）。verdict 必须是 "建议高风险" 或 "建议低风险" 二选一。不要因为复核文本留痕格式不足而单独判高风险；只有证据本身支持高风险或关键事实无法验证时才判高风险。不要在JSON外输出任何内容。不要出现内部流程名称、角色称谓或流程协调信息。
"""


def _publication_preflight_instruction(doi: str, ep: EvidencePaths) -> str:
    """Let reviewers see publication failures while they can still fix them."""
    import shlex
    import uuid
    evidence = ep.full or ep.llm_summary
    if not evidence:
        return ""
    draft = Path(evidence).parent / f'review_draft_{uuid.uuid4().hex}.json'
    command = ' '.join(shlex.quote(str(value)) for value in (
        BASE_DIR / 'venv/bin/python', BASE_DIR / 'scripts/validate_review_result.py',
        '--evidence', evidence, '--doi', doi, '--result', draft,
    ))
    return (
        "\n## 提交前必须执行本地发布校验\n"
        f"把完整的最终 JSON 草稿保存到本次专用路径：{draft}\n"
        "不要使用 /tmp 下固定名称的共享草稿，以免与并行任务相互覆盖。"
        "用 Bash 原样执行下面命令：\n"
        f"{command}\n"
        "这是只读校验，和最终报告使用同一发布规则，不会写数据库或生成报告。"
        "尽早保存含已核实事实的草稿，预留至少最后 6 轮用于校验与输出。"
        "若返回 ok=false，先区分字段表达缺口和事实缺口，只修复对应项；"
        "最多校验修复两次，同一 gaps 重复且无新增证据时停止循环，保留草稿及具体未解决项。"
        "通过后原样输出该 JSON。不要修改校验程序、证据包或检查规则以使校验通过。"
        "不得编造检查事实，不得把流程错误作为论文高风险证据。"
        "不能解决的事实缺口必须如实保留，不得声称通过。\n"
        "对同类重复列可以合并说明，但须完整列出所覆盖的工作表、组/通道、"
        "核查方法、代表原始数值及结论；不能只写‘等表’或‘其余均正常’。"
        "单个重复与 pooled 汇总的关系不能用于排除独立通道之间的重复。\n"
    )


def _build_prompt(
    doi: str,
    report_json_path: str,
    input_dir: str,
    ev: EvidencePaths | str | None = None,
    full_evidence_path: str = "",
    review_plan: dict | None = None,
) -> str:
    if isinstance(ev, str):
        ep = EvidencePaths(ev, full_evidence_path)
    else:
        ep = ev or EvidencePaths("", "")
    header = PROMPT_HEADER.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=ep.llm_summary or "(未生成,请直接审阅源数据)",
        soft_explain_guardrails=SOFT_EXPLAIN_GUARDRAILS,
    )
    if ep.full and ep.full != ep.llm_summary:
        header += f"\n完整证据包路径(仅必要时追溯, 不要求逐条展开): {ep.full}\n"
    rules = COMPACT_REVIEW_RULES
    plan_text = ""
    if review_plan and review_plan.get("instruction"):
        plan_text = (
            "\n## 本篇自适应核查计划\n"
            f"模式：{review_plan.get('name', 'targeted_evidence_review')}。"
            + str(review_plan["instruction"])
            + "不要把计划名称或内部流程写入最终 JSON。\n"
        )
    return header + rules + plan_text + PROMPT_FOOTER + _publication_preflight_instruction(doi, ep)


def _build_lowrisk_verifier_prompt(
    doi: str,
    report_json_path: str,
    input_dir: str,
    prior_result: dict,
    ev: EvidencePaths | None = None,
) -> str:
    # Keep the second pass focused: verify the low-risk release gate, do not
    # ask for a full independent narrative unless the first pass looks unsafe.
    ep = ev or EvidencePaths("", "")
    evidence_display = ep.llm_summary or "(未生成,请直接审阅源数据)"
    if ep.full and ep.full != ep.llm_summary:
        evidence_display += f"\n- 完整证据包路径(仅必要时追溯): {ep.full}"
    original_review = _prior_review_result_for_doi(doi)
    original_review_instruction = _original_review_instruction(original_review)
    return LOWRISK_VERIFIER_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=evidence_display,
        original_review_instruction=original_review_instruction,
        prior_result_json=json.dumps(_compact_review_result_for_prompt(prior_result), ensure_ascii=False, indent=2),
        skill_rules=COMPACT_REVIEW_RULES,
        reader_facing_text_rules=READER_FACING_TEXT_RULES,
        soft_explain_guardrails=SOFT_EXPLAIN_GUARDRAILS,
    ) + _publication_preflight_instruction(doi, ep)


def _build_judge_prompt(
    doi: str, report_json_path: str, input_dir: str,
    result1: dict, result2: dict,
    ev: EvidencePaths | None = None,
    coverage_gaps: list[str] | None = None,
) -> str:
    # NOTE: skill_rules may contain literal { } braces (JSON examples),
    # so it must NOT pass through str.format(). Substitute it AFTER formatting.
    # Repair must retain the latest checked facts; truncating them here can
    # turn a repair into another full synthesis and reintroduce resolved gaps.
    r1 = result1 if coverage_gaps else _compact_review_result_for_prompt(result1)
    r2 = result2 if coverage_gaps else _compact_review_result_for_prompt(result2)
    ep = ev or EvidencePaths("", "")
    original_review = _prior_review_result_for_doi(doi)
    original_review_instruction = ""
    original_review_task = ""
    if original_review:
        original_review_instruction = f"- 本轮复核前既有复核结论: {original_review}"
        if original_review == "高风险":
            original_review_task = (
                "9. 既有复核结论为高风险时，如果前序复核试图降为低风险，你必须进行反降级核验："
                "逐项检查原高风险线索是否被具体证据排除；若无法具体排除，或图文一致性、"
                "跨表/跨sheet/跨面板测量值复用、样本量与源数据一致性仍有疑点，应维持高风险。"
            )
    evidence_display = ep.llm_summary or "(未生成,请直接审阅源数据)"
    if ep.full and ep.full != ep.llm_summary:
        evidence_display += f"\n- 完整证据包路径(仅必要时追溯): {ep.full}"
    coverage_repair_instruction = ""
    if coverage_gaps:
        coverage_repair_instruction = (
            "\n## 本次必须补齐的覆盖缺口\n"
            "上一版综合结论未通过覆盖校验。请在对应复核字段中逐项加入明确的核查事实和风险结论，"
            "不要只改最终判定。重复列可按同一工作簿/工作表中的同一原因分组说明，"
            "但须列出所覆盖工作表、涉及的组/通道、代表原始数值和结论；"
            "pooled汇总列的解释不能代替独立通道重复的核查。"
            "对HIGH类别请同时写出类别标识和实际核查结论，不得仅写已纳入复核。"
            "高风险发现需完整给出问题位置、问题类型、具体情况、需关注原因、核查依据，"
            "且result、verdict、reason的最终结论必须一致：\n- "
            + "\n- ".join(str(gap) for gap in coverage_gaps)
        )
        # Repair one saved draft; do not invent a second independent review
        # or demand another complete investigation for a schema-only gap.
        return (
            f"你负责修复同一篇论文已保存的复核草稿。DOI: {doi}\n"
            f"检测报告: {report_json_path}\n原始材料: {input_dir}\n证据包: {evidence_display}\n"
            "下方是一份待校验草稿，不代表两个独立专家的一致意见。保留已核实事实与证据定位；"
            "先区分表达/字段缺口和事实缺口。表达缺口只修正表达，事实缺口必须核查对应材料；"
            "不要重做已经有具体依据且没有矛盾的全部核查。未解决的事实缺口不得声称已完成。\n"
            + coverage_repair_instruction + "\n完整原稿：\n"
            + json.dumps(result1, ensure_ascii=False)
            + "\n完整复核规则：\n" + COMPACT_REVIEW_RULES + PROMPT_FOOTER
            + _publication_preflight_instruction(doi, ep)
        )
    body = JUDGE_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=evidence_display,
        original_review_instruction=original_review_instruction,
        original_review_task=original_review_task,
        coverage_repair_instruction=coverage_repair_instruction,
        skill_rules="@@SKILL_RULES@@",
        reader_facing_text_rules=READER_FACING_TEXT_RULES,
        soft_explain_guardrails=SOFT_EXPLAIN_GUARDRAILS,
        r1_result=r1.get("result", ""),
        r1_image=r1.get("image_review", ""),
        r1_data=r1.get("data_review", ""),
        r1_ref=r1.get("ref_review", ""),
        r1_methodology=r1.get("methodology_review", ""),
        r1_verdict=r1.get("verdict", ""),
        r1_reason=r1.get("reason", ""),
        r2_result=r2.get("result", ""),
        r2_image=r2.get("image_review", ""),
        r2_data=r2.get("data_review", ""),
        r2_ref=r2.get("ref_review", ""),
        r2_methodology=r2.get("methodology_review", ""),
        r2_verdict=r2.get("verdict", ""),
        r2_reason=r2.get("reason", ""),
    )
    candidates = [
        {key: result.get(key, []) for key in ('evidence_images', 'excel_evidence_regions')}
        for result in (r1, r2)
        if result.get('evidence_images') or result.get('excel_evidence_regions')
    ]
    if candidates:
        body += (
            '\n## 可核对的截图候选\n'
            '以下仅为定位候选，不是已确认结论。必须查看源材料及截图后，仅将最终确认问题对应的核心截图'
            '写入evidence_images或excel_evidence_regions；修改与正文不一致的说明，排除错误标注。\n'
            + json.dumps(candidates, ensure_ascii=False, default=str)
        )
    return body.replace("@@SKILL_RULES@@", COMPACT_REVIEW_RULES) + _publication_preflight_instruction(doi, ep)


def _review_artifacts_dir(output_dir: str) -> Path | None:
    if not output_dir:
        return None
    path = Path(output_dir) / "review_artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _evidence_bundle_path(doi: str, report_json_path: str, input_dir: str, artifacts_dir: Path | None = None) -> Path:
    identity = _review_state_identity(doi, report_json_path, input_dir)
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    # New API/review calls pass artifacts_dir=output_dir/review_artifacts.
    # The legacy review_v2 root is kept only for old scripts/tests that do not
    # know a per-paper output_dir.
    base = artifacts_dir or REVIEW_DIR
    return base / f"{doi.replace('/', '_')}_{digest}_evidence.json"


def _ensure_evidence_bundle(doi, report_json_path, input_dir, artifacts_dir: Path | str | None = None):
    """Generate the deterministic evidence bundle in a bounded child process.

    The evidence builder reads source Excel files and can hit pathological
    workbooks. Keep that failure isolated so one DOI cannot kill the API worker.
    """
    artifacts = Path(artifacts_dir) if artifacts_dir else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    legacy_dir = REVIEW_DIR
    legacy_dir.mkdir(parents=True, exist_ok=True)
    out_path = _evidence_bundle_path(doi, report_json_path, input_dir, artifacts)
    def reusable(path):
        bundle = _load_json_file(str(path))
        return bool(bundle) and not bundle.get('_fallback_reason')

    if out_path.exists() and reusable(out_path):
        _augment_evidence_bundle(out_path, doi, report_json_path, input_dir)
        return str(out_path)

    legacy_path = _evidence_bundle_path(doi, report_json_path, input_dir, None)
    relocated = legacy_dir / "_unsorted" / legacy_path.name
    if artifacts:
        for candidate in (legacy_path, relocated):
            if candidate.exists() and reusable(candidate):
                shutil.copy2(candidate, out_path)
                _augment_evidence_bundle(out_path, doi, report_json_path, input_dir)
                return str(out_path)
    elif legacy_path.exists() and reusable(legacy_path):
        _augment_evidence_bundle(legacy_path, doi, report_json_path, input_dir)
        return str(legacy_path)
    elif relocated.exists() and reusable(relocated):
        _augment_evidence_bundle(relocated, doi, report_json_path, input_dir)
        return str(relocated)

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
            "--out", str(out_path),
        ]
        if report_json_path:
            cmd.extend(["--report-json", report_json_path])
        env = numeric_subprocess_env()
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(BASE_DIR), _SKILL_SCRIPTS, env.get("PYTHONPATH", ""))
            if p
        )
        completed = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=subprocess_budget(EVIDENCE_TIMEOUT_SECONDS),
            cwd=str(BASE_DIR),
            env=env,
            preexec_fn=limit_child_memory if os.name == "posix" else None,
        )
        if completed.returncode != 0:
            log.error(
                "DOI=%s evidence bundle generation failed: returncode=%s stderr=%s",
                doi, completed.returncode, completed.stderr[-1000:],
            )
            return _source_data_audit_fallback_evidence(
                out_path, doi, report_json_path, input_dir, "review_evidence failed"
            )
        _augment_evidence_bundle(out_path, doi, report_json_path, input_dir)
        return str(out_path)
    except subprocess.TimeoutExpired:
        log.error("DOI=%s evidence bundle generation timed out after %ss", doi, EVIDENCE_TIMEOUT_SECONDS)
        return _source_data_audit_fallback_evidence(
            out_path, doi, report_json_path, input_dir, "review_evidence timed out"
        )
    except Exception as e:
        log.error("DOI=%s evidence bundle generation failed: %s", doi, e)
        return _source_data_audit_fallback_evidence(
            out_path, doi, report_json_path, input_dir, f"review_evidence exception: {e}"
        )


def _source_data_audit_fallback_evidence(
    out_path: Path,
    doi: str,
    report_json_path: str,
    input_dir: str,
    reason: str,
) -> str:
    try:
        audit = _run_source_data_audit_bounded(input_dir, doi)
        if audit is None:
            raise TimeoutError("source-data audit did not finish within its bounded subprocess")
    except Exception as e:
        log.error("DOI=%s source-data audit fallback failed after evidence error: %s", doi, e)
        return ""

    high = [
        {
            "dim": "data",
            "test": item.get("test"),
            "location": item.get("location", ""),
            "source": "source_data_audit",
            "severity": "high",
        }
        for item in audit.get("high_findings") or []
    ]
    must = [
        f"source_data_audit::{item.get('test')}::{idx}"
        for idx, item in enumerate(audit.get("high_findings") or [])
    ]
    bundle = {
        "doi": doi,
        "input_dir": input_dir,
        "report_json": report_json_path,
        "paper": {},
        "summary": {},
        "data_files": [],
        "inventory": [],
        "main_figures": [],
        "high_findings": high,
        "deterministic_findings": {
            "duplicate_column_pairs": [],
            "cross_sheet_reuse": [],
            "cross_sheet_reuse_groups": [],
            "decimal_precision_mismatch": [],
            "source_data_audit": audit,
        },
        "review_guidance": {
            "fallback": (
                f"Full review_evidence bundle unavailable: {reason}. "
                "Source data audit still ran and must be used for risk judgement."
            )
        },
        "coverage_manifest": {
            "must_address": must,
            "counts": {
                "high_findings": len(high),
                "main_figures": 0,
                "duplicate_column_pairs": 0,
                "cross_sheet_reuse": 0,
                "cross_sheet_reuse_groups": 0,
                "cross_sheet_reuse_representatives": 0,
                "decimal_precision_mismatch": 0,
                "source_data_audit_high": len(audit.get("high_findings") or []),
                "source_data_matrix_reuse": len(audit.get("rectangular_matrix_reuse") or []),
                "source_data_digit_edit_reuse": len(audit.get("digit_edit_matrix_reuse") or []),
                "source_data_n_mismatch": len(audit.get("figure_n_mismatch") or []),
                "source_data_exact_series_reuse": len(audit.get("same_sheet_exact_series_reuse") or []),
                "source_data_fixed_ratio_series": len(audit.get("fixed_ratio_series") or []),
                "total_must_address": len(must),
            },
        },
        "_fallback_reason": reason,
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        _augment_evidence_with_cif_findings(out_path, report_json_path, input_dir)
        log.warning("DOI=%s wrote source-data-audit fallback evidence: %s high=%d", doi, out_path, len(high))
        return str(out_path)
    except Exception as e:
        log.error("DOI=%s could not write source-data-audit fallback evidence: %s", doi, e)
        return ""


def _run_source_data_audit_bounded(input_dir: str, doi: str) -> dict | None:
    """Run the supplemental audit outside the API process with a hard deadline."""
    script = BASE_DIR / "api" / "services" / "source_data_audit.py"
    command = [sys.executable, str(script), "--input-dir", input_dir, "--doi", doi]
    env = numeric_subprocess_env()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(BASE_DIR), env.get("PYTHONPATH", "")) if part
    )
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=subprocess_budget(SOURCE_DATA_AUDIT_TIMEOUT_SECONDS),
            cwd=str(BASE_DIR),
            env=env,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "DOI=%s source-data audit timed out after %ss",
            doi,
            SOURCE_DATA_AUDIT_TIMEOUT_SECONDS,
        )
        return None
    except OSError as exc:
        log.error("DOI=%s source-data audit could not start: %s", doi, exc)
        return None
    if completed.returncode != 0:
        log.error(
            "DOI=%s source-data audit failed rc=%s: %s",
            doi,
            completed.returncode,
            completed.stderr[-1000:],
        )
        return None
    try:
        result = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        log.error("DOI=%s source-data audit returned invalid JSON: %s", doi, exc)
        return None
    return result if isinstance(result, dict) else None


def _augment_evidence_with_source_data_audit(evidence_path: Path, doi: str, input_dir: str) -> None:
    """Add local source-data audit findings to the deterministic evidence bundle.

    review_evidence.py is shared legacy code; keeping the new audit as an
    augmentation here lets API/batch review use it without changing unrelated
    detector behavior.
    """
    try:
        bundle = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("DOI=%s could not read evidence before source-data audit: %s", doi, e)
        return
    try:
        from api.services.source_data_audit import AUDIT_VERSION

        existing = ((bundle.get("deterministic_findings") or {}).get("source_data_audit") or {})
        if existing.get("version") == AUDIT_VERSION:
            return

        audit = _run_source_data_audit_bounded(input_dir, doi)
        if audit is None:
            return
    except Exception as e:
        log.warning("DOI=%s source-data audit failed: %s", doi, e)
        return

    det = bundle.setdefault("deterministic_findings", {})
    det["source_data_audit"] = audit

    bundle["high_findings"] = [
        item for item in bundle.get("high_findings", [])
        if item.get("source") != "source_data_audit"
    ]
    project_channel_pairs = {
        (
            str(item.get("file") or "").rsplit("/", 1)[-1],
            str(item.get("sheet") or ""),
            min(int(item.get("col_a_idx") or -1), int(item.get("col_b_idx") or -1)),
            max(int(item.get("col_a_idx") or -1), int(item.get("col_b_idx") or -1)),
        )
        for item in (bundle.get("_project_channel_duplicate_findings") or [])
    }
    for item in audit.get("high_findings") or []:
        if item.get("test") == "same_sheet_channel_duplicate":
            details = item.get("details") or {}
            pair = (
                str(item.get("location") or "").split(" / ", 1)[0].rsplit("/", 1)[-1],
                str(item.get("location") or "").split(" / ")[1] if " / " in str(item.get("location") or "") else "",
                min(int(details.get("col_a_idx") or -1), int(details.get("col_b_idx") or -1)),
                max(int(details.get("col_a_idx") or -1), int(details.get("col_b_idx") or -1)),
            )
            if pair in project_channel_pairs:
                continue
        bundle.setdefault("high_findings", []).append({
            "dim": "data",
            "test": item.get("test"),
            "location": item.get("location", ""),
            "source": "source_data_audit",
            "severity": "high",
        })

    manifest = bundle.setdefault("coverage_manifest", {})
    must = manifest.setdefault("must_address", [])
    must[:] = [item for item in must if not str(item).startswith("source_data_audit::")]
    for idx, item in enumerate(audit.get("high_findings") or []):
        must.append(f"source_data_audit::{item.get('test')}::{idx}")
    counts = manifest.setdefault("counts", {})
    counts["source_data_audit_high"] = len(audit.get("high_findings") or [])
    counts["source_data_matrix_reuse"] = len(audit.get("rectangular_matrix_reuse") or [])
    counts["source_data_digit_edit_reuse"] = len(audit.get("digit_edit_matrix_reuse") or [])
    counts["source_data_n_mismatch"] = len(audit.get("figure_n_mismatch") or [])
    counts["source_data_exact_series_reuse"] = len(audit.get("same_sheet_exact_series_reuse") or [])
    counts["source_data_fixed_ratio_series"] = len(audit.get("fixed_ratio_series") or [])
    counts["high_findings"] = len(bundle.get("high_findings") or [])
    counts["total_must_address"] = len(must)

    try:
        Path(evidence_path).write_text(json.dumps(bundle, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        log.info(
            "DOI=%s source-data audit added to evidence: high=%d matrix=%d n_mismatch=%d exact_series=%d fixed_ratio=%d",
            doi,
            counts["source_data_audit_high"],
            counts["source_data_matrix_reuse"],
            counts["source_data_n_mismatch"],
            counts["source_data_exact_series_reuse"],
            counts["source_data_fixed_ratio_series"],
        )
    except Exception as e:
        log.warning("DOI=%s could not write augmented evidence: %s", doi, e)


def _augment_evidence_bundle(
    evidence_path: Path,
    doi: str,
    report_json_path: str,
    input_dir: str,
) -> None:
    _augment_suspicious_same_group_channel_duplicates(evidence_path, input_dir)
    _augment_evidence_with_source_data_audit(evidence_path, doi, input_dir)
    _augment_evidence_with_cif_findings(evidence_path, report_json_path, input_dir)


def _augment_suspicious_same_group_channel_duplicates(
    evidence_path: Path, input_dir: str,
) -> None:
    """Promote exact duplicates between distinct measurement channels.

    ``scan_columns.py`` historically marked only cross-group pairs as HIGH.
    A workbook can, however, place two different channels in the same repeat
    block (for example TSPAN4-GFP and mCherry in Fig. 3E).  If one channel's
    full numeric column is copied into the other, ``group_a == group_b`` and
    the finding used to remain only a coverage warning.  Resolve the merged
    header labels here and add a paper-level finding so review cannot silently
    downgrade it.
    """
    try:
        bundle = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        det = (bundle.get("deterministic_findings") or {})
        pairs = det.get("duplicate_column_pairs") or []
        if not pairs:
            return
        import openpyxl

        root = Path(input_dir)
        promoted = bundle.setdefault("_project_channel_duplicate_findings", [])
        existing_keys = {
            (str(item.get("file") or ""), str(item.get("sheet") or ""),
             int(item.get("col_a_idx") or 0), int(item.get("col_b_idx") or 0))
            for item in promoted
        }
        additions = []
        workbook_cache = {}

        def find_workbook(name: str) -> Path | None:
            direct = root / name
            if direct.is_file():
                return direct
            matches = list(root.rglob(Path(name).name))
            return matches[0] if matches else None

        def merged_label(ws, col_idx: int, max_header_row: int = 8) -> str:
            """Return the merged/header label governing a zero-based column."""
            col = col_idx + 1
            candidates = []
            for merged in ws.merged_cells.ranges:
                if merged.min_col <= col <= merged.max_col and merged.min_row <= max_header_row:
                    value = ws.cell(merged.min_row, merged.min_col).value
                    if isinstance(value, str) and value.strip():
                        candidates.append((merged.max_col - merged.min_col, len(value), value.strip()))
            if candidates:
                # Prefer the narrowest governing merge; it is usually the
                # channel label rather than the whole-repeat title.
                candidates.sort(key=lambda item: (item[0], -item[1]))
                return candidates[0][2]
            # Non-merged headers: use the closest non-empty string to the left
            # on the first few rows, which also handles exported spreadsheets
            # whose merge metadata was stripped.
            for row in range(1, max_header_row + 1):
                value = ws.cell(row, col).value
                if isinstance(value, str) and value.strip():
                    return value.strip()
                for left in range(col - 1, max(0, col - 32), -1):
                    value = ws.cell(row, left).value
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return ""

        for pair in pairs:
            if pair.get("cross_group"):
                continue
            n_points = int(pair.get("n_points") or 0)
            if n_points < 15:
                continue
            a_idx = int(pair.get("col_a_idx") or 0)
            b_idx = int(pair.get("col_b_idx") or 0)
            if abs(a_idx - b_idx) < 5:
                continue
            key = (str(pair.get("file") or ""), str(pair.get("sheet") or ""), a_idx, b_idx)
            if key in existing_keys:
                continue
            workbook_name = str(pair.get("file") or "")
            workbook_path = find_workbook(workbook_name)
            if not workbook_path:
                continue
            try:
                wb = workbook_cache.get(str(workbook_path))
                if wb is None:
                    wb = openpyxl.load_workbook(workbook_path, read_only=False, data_only=True)
                    workbook_cache[str(workbook_path)] = wb
                if pair.get("sheet") not in wb.sheetnames:
                    continue
                ws = wb[pair.get("sheet")]
                label_a = merged_label(ws, a_idx)
                label_b = merged_label(ws, b_idx)
                if not label_a or not label_b or label_a == label_b:
                    continue
                values_a = []
                values_b = []
                start = max(1, int(pair.get("data_start_row") or 3) + 1)
                for row in range(start, (ws.max_row or start) + 1):
                    va = ws.cell(row, a_idx + 1).value
                    vb = ws.cell(row, b_idx + 1).value
                    if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                        values_a.append(va)
                        values_b.append(vb)
                n = min(len(values_a), len(values_b))
                if n < 15:
                    continue
                equal = sum(1 for va, vb in zip(values_a[:n], values_b[:n]) if va == vb)
                # Filter out copied categorical/identifier columns. A genuine
                # channel-level measurement duplicate normally carries
                # fractional precision in most rows.
                fractional = sum(
                    1 for value in values_a[:n] + values_b[:n]
                    if abs(float(value) - round(float(value))) > 1e-9
                ) / max(1, 2 * n)
                if equal != n or fractional < 0.5:
                    continue
                item = {
                    "test": "same_sheet_channel_duplicate",
                    "severity": "high",
                    "location": (
                        f"{workbook_name} / {pair.get('sheet')} / "
                        f"{pair.get('group_a') or '同一重复组'} / "
                        f"{label_a} vs {label_b}"
                    ),
                    "description": (
                        f"同一重复组中两个不同通道（{label_a}；{label_b}）的两列"
                        f"逐值完全相同，共 {n} 个测量值；示例为 "
                        f"{', '.join(str(v) for v in values_a[:5])}。"
                    ),
                    "file": workbook_name,
                    "sheet": pair.get("sheet"),
                    "group": pair.get("group_a"),
                    "col_a_idx": a_idx,
                    "col_b_idx": b_idx,
                    "label_a": label_a,
                    "label_b": label_b,
                    "n_values": n,
                    "sample": values_a[:5],
                }
                additions.append(item)
                promoted.append({
                    "file": workbook_name,
                    "sheet": pair.get("sheet"),
                    "col_a_idx": a_idx,
                    "col_b_idx": b_idx,
                    "label_a": label_a,
                    "label_b": label_b,
                    "n_values": n,
                })
                existing_keys.add(key)
            except Exception as exc:
                log.debug("channel duplicate inspection failed for %s: %s", workbook_name, exc)
        for wb in workbook_cache.values():
            with contextlib.suppress(Exception):
                wb.close()
        if not additions:
            return
        high = bundle.setdefault("high_findings", [])
        must = bundle.setdefault("coverage_manifest", {}).setdefault("must_address", [])
        existing_high = {
            (str(item.get("test") or ""), str(item.get("location") or ""))
            for item in high
        }
        for item in additions:
            key = (item["test"], item["location"])
            if key in existing_high:
                continue
            index = len(high)
            high.append({
                "dim": "data", "test": item["test"],
                "location": item["location"], "source": "project_channel_duplicate",
                "severity": "high",
            })
            must.append(f"high::data::{index}")
            existing_high.add(key)
        counts = bundle.setdefault("coverage_manifest", {}).setdefault("counts", {})
        counts["high_findings"] = len(high)
        counts["same_sheet_channel_duplicate"] = len(promoted)
        counts["total_must_address"] = len(must)
        Path(evidence_path).write_text(json.dumps(bundle, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        log.warning("Promoted %d same-group channel duplicate(s) to HIGH evidence", len(additions))
    except Exception as exc:
        log.warning("Could not augment same-group channel duplicates: %s", exc)


def _augment_evidence_with_cif_findings(
    evidence_path: Path,
    report_json_path: str,
    input_dir: str,
) -> None:
    """Attach full CIF findings and file inventory to the deterministic review bundle."""
    try:
        bundle = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read evidence before CIF augmentation: %s", e)
        return

    report = {}
    if report_json_path and Path(report_json_path).exists():
        try:
            report = json.loads(Path(report_json_path).read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not read report JSON before CIF augmentation: %s", e)

    cif_findings = [
        item for item in (report.get("data_anomalies") or [])
        if str(item.get("test") or "").startswith("cif_")
    ]
    root = Path(input_dir)
    cif_files = []
    if root.exists():
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() == ".cif":
                try:
                    cif_files.append(path.relative_to(root).as_posix())
                except ValueError:
                    cif_files.append(str(path))
    if not cif_files and not cif_findings:
        return

    deterministic = bundle.setdefault("deterministic_findings", {})
    deterministic["cif_review"] = {
        "files": cif_files,
        "findings": cif_findings,
    }

    high_findings = bundle.setdefault("high_findings", [])
    manifest = bundle.setdefault("coverage_manifest", {})
    must_address = manifest.setdefault("must_address", [])
    existing = {
        (str(item.get("test") or ""), str(item.get("location") or ""))
        for item in high_findings
    }
    for finding in cif_findings:
        if finding.get("severity") != "high":
            continue
        key = (str(finding.get("test") or ""), str(finding.get("location") or ""))
        if key in existing:
            continue
        high_index = len(high_findings)
        high_findings.append({
            "dim": "data",
            "test": finding.get("test"),
            "location": finding.get("location", ""),
            "source": "cif_checker",
            "severity": "high",
        })
        must_address.append(f"high::data::{high_index}")
        existing.add(key)

    counts = manifest.setdefault("counts", {})
    counts["cif_files"] = len(cif_files)
    counts["cif_findings"] = len(cif_findings)
    counts["cif_high"] = sum(1 for item in cif_findings if item.get("severity") == "high")
    counts["high_findings"] = len(high_findings)
    counts["total_must_address"] = len(must_address)
    bundle.setdefault("review_guidance", {})["cif"] = (
        "Open each listed CIF and verify detector fields against the manuscript: formula, space group, "
        "unit cell, Z, density, R1/wR2, and CCDC identifier. Treat poor refinement alone as a quality "
        "signal; require a concrete structural contradiction or manuscript mismatch for high risk."
    )

    try:
        Path(evidence_path).write_text(
            json.dumps(bundle, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        log.info(
            "CIF review evidence added: files=%d findings=%d high=%d",
            len(cif_files),
            len(cif_findings),
            counts["cif_high"],
        )
    except Exception as e:
        log.warning("Could not write CIF-augmented evidence: %s", e)


def _short_text(value, limit: int = 500):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    for sep in ("。", "；", ". ", "; ", "\n"):
        pos = truncated.rfind(sep)
        if pos > limit * 0.5:
            return truncated[: pos + len(sep)].rstrip() + "..."
    return truncated.rstrip() + "..."


def _axis_like_header(header: str) -> bool:
    h = str(header or "").lower()
    axis_terms = (
        "time", "potential", "voltage", "wavelength",
        "wavenumber", "frequency", "temperature", "dose", "concentration",
        "r+α", "r+alpha", "r (", "k ", "q ", "2θ", "theta", "binding energy",
        "axis", "scan", "cycle",
        "energy", "raman", "angle", "pressure", "depth",
        "position", "distance", "diameter", "bin ",
    )
    return any(term in h for term in axis_terms)


def _representative_items(items: list[dict], max_items: int = 3) -> list[dict]:
    def rank(item):
        n = int(item.get("n_points") or item.get("n_values") or 0)
        return (
            bool(item.get("requires_expansion")),
            n,
            len(item.get("locations") or []),
            len(json.dumps(item, ensure_ascii=False, default=str)),
        )

    return sorted(items, key=rank, reverse=True)[:max_items]


def _compact_duplicate_column_pairs(pairs: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str, str, str, str], list[dict]] = {}
    for pair in pairs or []:
        ha = str(pair.get("col_a_header") or "").strip().lower()
        hb = str(pair.get("col_b_header") or "").strip().lower()
        sorted_headers = tuple(sorted([ha, hb]))
        key = (
            str(pair.get("file") or ""),
            str(pair.get("sheet") or ""),
            sorted_headers[0],
            sorted_headers[1],
            str(pair.get("overlap") or ""),
        )
        buckets.setdefault(key, []).append(pair)

    groups = []
    for idx, ((_file, _sheet, ha, hb, _overlap), members) in enumerate(
        sorted(buckets.items(), key=lambda kv: (kv[0], -len(kv[1]))),
        1,
    ):
        reps = _representative_items(members, max_items=2)
        max_points = max([int(m.get("n_points") or 0) for m in members] or [0])
        groups.append({
            "group_id": f"dupcol_group_{idx}",
            "count": len(members),
            "file": _file,
            "sheet": _sheet,
            "headers": [ha, hb],
            "max_points": max_points,
            "likely_axis_or_shared_variable": _axis_like_header(ha) or _axis_like_header(hb),
            "representatives": [
                {
                    "col_a_idx": r.get("col_a_idx"),
                    "col_a_header": r.get("col_a_header"),
                    "col_b_idx": r.get("col_b_idx"),
                    "col_b_header": r.get("col_b_header"),
                    "header_row": r.get("header_row"),
                    "data_start_row": r.get("data_start_row"),
                    "overlap": r.get("overlap"),
                    "n_points": r.get("n_points"),
                    "sample_a": r.get("sample_a", [])[:5],
                    "sample_b": r.get("sample_b", [])[:5],
                }
                for r in reps
            ],
        })
    return groups


def _compact_high_findings(findings: list[dict], max_items: int = 20) -> list[dict]:
    out = []
    for idx, item in enumerate(findings or []):
        compact = {
            "id": f"high::{item.get('dim', 'unknown')}::{idx}",
            "dim": item.get("dim"),
            "test": item.get("test") or item.get("match_type") or item.get("issue_type"),
            "location": _short_text(item.get("location") or item.get("pages") or item.get("ref_number") or "", 300),
        }
        out.append(compact)
    return out[:max_items]


def _compact_cross_sheet_groups(groups: list[dict]) -> list[dict]:
    out = []
    for group in groups or []:
        out.append({
            "group_id": group.get("group_id"),
            "scope": group.get("scope"),
            "count": group.get("count"),
            "requires_expansion": group.get("requires_expansion"),
            "header_signature": group.get("header_signature"),
            "n_values_bucket": group.get("n_values_bucket"),
            "figure_labels": group.get("figure_labels", [])[:12],
            "headers_sample": group.get("headers_sample", [])[:8],
            "locations_sample": group.get("locations_sample", [])[:8],
            "representatives": [
                {
                    "reuse_index": r.get("reuse_index"),
                    "n_values": r.get("n_values"),
                    "locations": r.get("locations", [])[:10],
                    "sample": r.get("sample", [])[:5],
                }
                for r in (group.get("representatives") or [])[:2]
            ],
        })
    return out


def _build_llm_evidence_summary(evidence: dict, full_evidence_path: str) -> dict:
    det = (evidence or {}).get("deterministic_findings") or {}
    counts = _evidence_counts(evidence)
    dup_groups = _compact_duplicate_column_pairs(det.get("duplicate_column_pairs") or [])
    reuse_groups = _compact_cross_sheet_groups(det.get("cross_sheet_reuse_groups") or [])
    decimal = det.get("decimal_precision_mismatch") or []
    source_audit = det.get("source_data_audit") or {}
    cif_review = det.get("cif_review") or {}

    summary = {
        "doi": (evidence or {}).get("doi"),
        "input_dir": (evidence or {}).get("input_dir"),
        "report_json": (evidence or {}).get("report_json"),
        "full_evidence_path": full_evidence_path,
        "paper": {
            "title": ((evidence or {}).get("paper") or {}).get("title"),
            "doi": ((evidence or {}).get("paper") or {}).get("doi"),
            "filename": ((evidence or {}).get("paper") or {}).get("filename"),
        },
        "summary_counts": counts,
        "review_scope": {
            "coverage_unit": "Use grouped evidence units below, not every raw must_address item.",
            "duplicate_column_pair_groups": len(dup_groups),
            "cross_sheet_reuse_groups": len(reuse_groups),
            "raw_must_address_count": counts.get("total_must_address"),
            "instruction": (
                "Review each group representative and any requires_expansion group. "
                "If a group is explained by the same axis/shared-variable pattern, one group-level verdict covers all members."
            ),
        },
        "high_findings": _compact_high_findings((evidence or {}).get("high_findings") or []),
        "deterministic_findings": {
            "duplicate_column_pair_groups": dup_groups,
            "cross_sheet_reuse_groups": reuse_groups,
            "decimal_precision_mismatch": decimal[:10],
            "source_data_audit": _compact_source_data_audit(source_audit),
            "cif_review": cif_review,
        },
        "main_figures": ((evidence or {}).get("main_figures") or [])[:20],
        "data_files": ((evidence or {}).get("data_files") or [])[:20],
        "review_guidance": (evidence or {}).get("review_guidance") or {},
    }
    return summary


def _compact_source_data_audit(audit: dict) -> dict:
    if not audit:
        return {}
    return {
        "version": audit.get("version"),
        "data_file_count": audit.get("data_file_count"),
        "high_findings": (audit.get("high_findings") or [])[:10],
        "rectangular_matrix_reuse": (audit.get("rectangular_matrix_reuse") or [])[:10],
        "near_modified_matrix_reuse": (audit.get("near_modified_matrix_reuse") or [])[:10],
        "digit_edit_matrix_reuse": (audit.get("digit_edit_matrix_reuse") or [])[:10],
        "figure_n_mismatch": (audit.get("figure_n_mismatch") or [])[:10],
        "same_sheet_exact_series_reuse": (audit.get("same_sheet_exact_series_reuse") or [])[:10],
        "fixed_ratio_series": (audit.get("fixed_ratio_series") or [])[:10],
    }


def _ensure_llm_evidence_bundle(doi: str, full_evidence_path: str) -> str:
    if not full_evidence_path or not Path(full_evidence_path).exists():
        return full_evidence_path or ""
    try:
        full_path = Path(full_evidence_path)
        out = full_path.with_name(full_path.stem + "_llm_summary.json")
        if out.exists() and out.stat().st_mtime_ns >= full_path.stat().st_mtime_ns:
            try:
                cached = json.loads(out.read_text(encoding="utf-8"))
                if cached.get("_summary_code_version") == _SUMMARY_CODE_VERSION:
                    return str(out)
            except Exception:
                pass
        evidence = _load_json_file(str(full_path))
        if not evidence:
            return full_evidence_path
        summary = _build_llm_evidence_summary(evidence, full_evidence_path)
        summary["_summary_code_version"] = _SUMMARY_CODE_VERSION
        payload = json.dumps(summary, ensure_ascii=False, indent=1)
        if len(payload.encode("utf-8")) > LLM_EVIDENCE_MAX_BYTES:
            # Last-resort cap: keep counts and highest-priority groups, preserving
            # the full evidence path for traceability.
            det = summary.get("deterministic_findings") or {}
            det["duplicate_column_pair_groups"] = det.get("duplicate_column_pair_groups", [])[:12]
            det["cross_sheet_reuse_groups"] = det.get("cross_sheet_reuse_groups", [])[:12]
            summary["high_findings"] = summary.get("high_findings", [])[:12]
            payload = json.dumps(summary, ensure_ascii=False, indent=1)
        out.write_text(payload, encoding="utf-8")
        log.info(
            "DOI=%s LLM evidence summary generated: %s (%d bytes from %d bytes)",
            doi,
            out,
            len(payload.encode("utf-8")),
            full_path.stat().st_size,
        )
        return str(out)
    except Exception as e:
        log.warning("DOI=%s could not build LLM evidence summary: %s", doi, e)
        return full_evidence_path


def _safe_doi_slug(doi: str) -> str:
    doi = normalize_doi(doi) or doi
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


def _review_state_path(
    doi: str,
    report_json_path: str,
    input_dir: str,
    artifacts_dir: Path | str | None = None,
) -> Path:
    identity = _review_state_identity(doi, report_json_path, input_dir)
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    if artifacts_dir:
        return Path(artifacts_dir) / f"{_safe_doi_slug(doi)}_{digest}_state.json"
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


def _localize_artifact(path_value: str, artifacts_dir: Path | None) -> str:
    if not path_value or not artifacts_dir:
        return path_value or ""
    src = Path(path_value)
    if not src.exists():
        return path_value
    try:
        src.resolve().relative_to(artifacts_dir.resolve())
        return str(src)
    except Exception:
        pass
    dst = artifacts_dir / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return str(dst)


def _find_any_completed_state_for_doi(doi: str) -> dict | None:
    """Scan REVIEW_STATE_DIR for any completed state file matching this DOI.

    Unlike _review_state_path (which computes an identity-specific hash), this
    function globs by DOI slug prefix so it can discover states created with
    different report/input paths.  This is the reconciliation bridge: an API
    review and a batch review may use different paths but still need to agree
    on the final verdict for the same paper.

    Returns the latest completed state dict (by updated_at), or None.
    """
    try:
        slug = _safe_doi_slug(doi)
    except Exception:
        return None
    pattern = f"{slug}_*.json"
    try:
        candidates: list[Path] = list(REVIEW_STATE_DIR.glob(pattern))
        # Also scan review_artifacts/ subdirectories (API pipeline output)
        review_v2 = REVIEW_STATE_DIR.parent
        for artifacts_dir in review_v2.glob("*/review_artifacts"):
            candidates.extend(artifacts_dir.glob(f"{slug}_*_state.json"))
    except Exception:
        return None

    # Exclude .tmp files (partial atomic writes)
    candidates = [p for p in candidates if not p.name.endswith(".tmp")]
    if not candidates:
        return None

    best_state: dict | None = None
    best_updated = ""
    for path in candidates:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        identity = state.get("identity") or {}
        if identity.get("version") != STATE_VERSION:
            continue
        final = (state.get("stages") or {}).get("final")
        if not isinstance(final, dict) or final.get("result") not in ("高风险", "低风险"):
            continue
        updated = state.get("updated_at", "")
        if updated > best_updated:
            best_updated = updated
            best_state = state

    return best_state


def _get_stage(state: dict, key: str):
    return (state.get("stages") or {}).get(key)


def _store_stage(path: Path, state: dict, key: str, value) -> None:
    state.setdefault("stages", {})[key] = value
    if key in ("judge", "coverage_repair") and _usable_cached_review_result(value):
        # A later failed call must never erase the latest substantive draft.
        state["stages"]["synthesis_candidate"] = dict(value)
    _save_review_state(path, state)


def _valid_review_result(result) -> bool:
    return isinstance(result, dict) and result.get("result") in ("高风险", "低风险")


@lru_cache(maxsize=1)
def _prior_review_result_map() -> dict[str, str]:
    path = os.environ.get("REVIEW_PRIOR_RESULTS_JSON", "").strip()
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not load REVIEW_PRIOR_RESULTS_JSON=%s: %s", path, e)
        return {}
    out: dict[str, str] = {}
    if isinstance(data, dict):
        iterable = data.items()
    elif isinstance(data, list):
        iterable = (
            (item.get("doi"), item.get("review_result"))
            for item in data
            if isinstance(item, dict)
        )
    else:
        return {}
    for doi, result in iterable:
        norm = normalize_doi(str(doi or "")) or str(doi or "")
        if norm and result in ("高风险", "低风险"):
            out[norm] = result
    return out


def _prior_review_result_for_doi(doi: str) -> str | None:
    norm = normalize_doi(doi) or doi
    return _prior_review_result_map().get(norm)


def _original_review_instruction(original_review: str | None) -> str:
    if not original_review:
        return ""
    text = f"本论文进入本轮复核前的既有复核结论：{original_review}。"
    if original_review == "高风险":
        text += (
            "\n如果既有结论为高风险，而首轮本次复核给出低风险，你必须重点围绕原高风险线索做反降级核验："
            "默认假设原高风险可能成立，主动查找首轮低风险是否遗漏了图文一致性、跨表/跨sheet/跨面板测量值复用、"
            "样本量与源数据不一致等证据。只有原高风险线索被逐项核实为可解释，才可维持低风险。"
        )
    return text


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
        "source_data_audit_high": int(counts.get("source_data_audit_high") or 0),
        "source_data_matrix_reuse": int(counts.get("source_data_matrix_reuse") or 0),
        "source_data_digit_edit_reuse": int(counts.get("source_data_digit_edit_reuse") or 0),
        "source_data_n_mismatch": int(counts.get("source_data_n_mismatch") or 0),
        "source_data_exact_series_reuse": int(counts.get("source_data_exact_series_reuse") or 0),
        "source_data_fixed_ratio_series": int(counts.get("source_data_fixed_ratio_series") or 0),
        "total_must_address": int(counts.get("total_must_address") or 0),
    }


def _adaptive_review_plan(evidence: dict | None) -> dict:
    """Choose the smallest complete review plan from deterministic coverage.

    The evidence builder has already performed the expensive workbook and
    image inventory.  The model should spend turns adjudicating those fixed
    findings, not rediscovering them with open-ended scripts.  This plan only
    changes the normal first-pass prompt/tool scope; publication validation
    and the single whole-paper deadline remain unchanged.
    """
    evidence = evidence or {}
    counts = _evidence_counts(evidence)
    strong = _has_strong_deterministic_evidence(evidence)
    figures = counts.get("main_figures", 0)
    actionable = _evidence_has_actionable_signal(evidence)
    high = counts.get("high_findings", 0)

    if strong and not figures and high >= 4:
        return {
            "name": "deterministic_data_focus",
            "max_turns": min(REVIEW_MAX_TURNS, 24),
            "allowed_tools": ("Read",),
            "instruction": (
                "本篇的工作簿确定性扫描已经完成，并已把长序列重复、重复列和小数位线索"
                "写入证据包。只阅读证据摘要及必要的具体文件位置，逐组判断其语义并完成图像、"
                "数据、参考文献、研究设计/统计四个维度；禁止重新扫描整个 Excel、运行组合比较、"
                "生成新的全量扫描脚本或重复已完成的证据发现。没有主图清单时，按证据包和正文"
                "明确说明图像核查范围与限制。"
            ),
        }
    if strong or actionable:
        return {
            "name": "targeted_evidence_review",
            "max_turns": min(REVIEW_MAX_TURNS, 32),
            "allowed_tools": ("Bash", "Read"),
            "instruction": (
                "确定性证据包是本轮核查清单。优先核查 HIGH 发现及其代表样例，按分组给出结论；"
                "只对未覆盖或存在语义分歧的位置做定向读取，禁止重新执行全表扫描或无界图像/数值穷举。"
            ),
        }
    return {
        "name": "clean_coverage_review",
        "max_turns": min(REVIEW_MAX_TURNS, 20),
        "allowed_tools": ("Read",),
        "instruction": (
            "证据包未发现可操作的高风险线索。按证据包覆盖四个维度并抽查关键位置；"
            "不要重新扫描完整工作簿或重复生成证据。只有发现具体矛盾时才读取对应原文件。"
        ),
    }


def _has_strong_deterministic_evidence(evidence: dict) -> bool:
    """Whether the evidence bundle contains paper-level signals that justify
    first-pass high-risk convergence after the review confirms them."""
    det = (evidence or {}).get("deterministic_findings") or {}
    if det.get("decimal_precision_mismatch"):
        return True
    if det.get("duplicate_column_pairs"):
        return True
    source_audit = det.get("source_data_audit") or {}
    if source_audit.get("high_findings"):
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


def _sample_text(values, max_items: int = 5) -> str:
    out = []
    for value in (values or [])[:max_items]:
        if isinstance(value, float):
            out.append(f"{value:.6g}")
        else:
            out.append(str(value))
    return "、".join(out)


def _split_location(location: str) -> tuple[str, str, str]:
    parts = str(location or "").split("::")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return str(location or ""), "", ""


def _human_locations(locations: list[str], max_items: int = 6) -> str:
    shown = []
    for loc in (locations or [])[:max_items]:
        file_name, sheet, column = _split_location(loc)
        if sheet and column:
            shown.append(f"{file_name} 的 {sheet} 工作表的 {column} 列")
        elif sheet:
            shown.append(f"{file_name} 的 {sheet} 工作表")
        else:
            shown.append(str(loc))
    if len(locations or []) > max_items:
        shown.append(f"等 {len(locations)} 个位置")
    return "、".join(shown)


def _strong_duplicate_column_groups(evidence: dict) -> list[dict]:
    det = (evidence or {}).get("deterministic_findings") or {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for pair in det.get("duplicate_column_pairs") or []:
        if not pair.get("cross_group"):
            continue
        n_points = int(pair.get("n_points") or 0)
        ha = str(pair.get("col_a_header") or "")
        hb = str(pair.get("col_b_header") or "")
        if n_points < DETERMINISTIC_FASTPATH_MIN_POINTS:
            continue
        if _axis_like_header(ha) or _axis_like_header(hb):
            continue
        key = (str(pair.get("file") or ""), str(pair.get("sheet") or ""))
        groups.setdefault(key, []).append(pair)
    out = []
    for (file_name, sheet), members in groups.items():
        members = sorted(members, key=lambda item: int(item.get("n_points") or 0), reverse=True)
        out.append({
            "file": file_name,
            "sheet": sheet,
            "count": len(members),
            "max_points": max(int(m.get("n_points") or 0) for m in members),
            "representatives": members[:3],
        })
    return sorted(out, key=lambda item: (item["max_points"], item["count"]), reverse=True)


def _strong_cross_sheet_groups(evidence: dict) -> list[dict]:
    det = (evidence or {}).get("deterministic_findings") or {}
    out = []
    for group in det.get("cross_sheet_reuse_groups") or []:
        reps = group.get("representatives") or []
        strong_reps = [
            rep for rep in reps
            if int(rep.get("n_values") or 0) >= DETERMINISTIC_FASTPATH_MIN_POINTS
        ]
        headers = [str(h or "") for h in group.get("headers_sample") or []]
        if not strong_reps:
            continue
        if headers and all(_axis_like_header(h) for h in headers):
            continue
        out.append({
            "group_id": group.get("group_id") or "",
            "scope": group.get("scope") or "",
            "count": int(group.get("count") or 0),
            "requires_expansion": bool(group.get("requires_expansion")),
            "headers": headers,
            "figure_labels": group.get("figure_labels") or [],
            "representatives": sorted(
                strong_reps,
                key=lambda rep: int(rep.get("n_values") or 0),
                reverse=True,
            )[:3],
        })
    return sorted(
        out,
        key=lambda item: max(int(rep.get("n_values") or 0) for rep in item["representatives"]),
        reverse=True,
    )


def _find_input_file(input_dir: str, file_name: str) -> Path | None:
    base = Path(input_dir or "")
    if not base.exists():
        return None
    direct = base / file_name
    if direct.exists():
        return direct
    matches = list(base.rglob(Path(file_name).name))
    return matches[0] if matches else None


def _norm_cell(value):
    if isinstance(value, float):
        return round(value, 12)
    return value


def _values_equal(a, b) -> bool:
    return _norm_cell(a) == _norm_cell(b)


def _column_values(ws, col_1based: int, start_row: int) -> list:
    values = []
    for row in ws.iter_rows(
        min_row=start_row,
        min_col=col_1based,
        max_col=col_1based,
        values_only=True,
    ):
        value = row[0]
        if value is not None:
            values.append(value)
    return values


def _sample_segments(values: list, max_each: int = 3) -> dict:
    n = len(values)
    if n <= max_each * 3:
        return {"first": values[:max_each], "middle": values[max(0, n // 2 - 1): n // 2 + 2], "last": values[-max_each:]}
    mid = n // 2
    return {
        "first": values[:max_each],
        "middle": values[max(0, mid - 1): min(n, mid + 2)],
        "last": values[-max_each:],
    }


def _match_sample_prefix(values: list, sample: list) -> int:
    if not sample:
        return 0
    score = 0
    for got, expected in zip(values, sample):
        if _values_equal(got, expected):
            score += 1
    return score


def _find_column_by_header_and_sample(wb, sheet: str, header: str, sample: list) -> dict | None:
    if sheet not in wb.sheetnames:
        return None
    ws = wb[sheet]
    header = str(header or "").strip()
    candidates = []
    max_header_rows = min(5, ws.max_row or 5)
    for row in ws.iter_rows(min_row=1, max_row=max_header_rows):
        for cell in row:
            if str(cell.value or "").strip() != header:
                continue
            values = _column_values(ws, cell.column, cell.row + 1)
            candidates.append({
                "column_index": cell.column,
                "header_row": cell.row,
                "values": values,
                "prefix_score": _match_sample_prefix(values, sample),
            })
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item["prefix_score"], len(item["values"])), reverse=True)
    best = candidates[0]
    if sample and best["prefix_score"] == 0:
        return None
    return best


def _verify_duplicate_pair(wb, pair: dict) -> dict:
    sheet = str(pair.get("sheet") or "")
    out = {
        "kind": "same_sheet_duplicate_column",
        "file": pair.get("file"),
        "sheet": sheet,
        "group_a": pair.get("group_a"),
        "group_b": pair.get("group_b"),
        "col_a_header": pair.get("col_a_header"),
        "col_b_header": pair.get("col_b_header"),
        "expected_points": int(pair.get("n_points") or 0),
        "verified": False,
    }
    if sheet not in wb.sheetnames:
        out["error"] = "sheet not found"
        return out
    ws = wb[sheet]
    col_a = int(pair.get("col_a_idx") or 0) + 1
    col_b = int(pair.get("col_b_idx") or 0) + 1
    data_start_row = _evidence_data_start_row(pair)
    values_a = _column_values(ws, col_a, data_start_row)
    values_b = _column_values(ws, col_b, data_start_row)
    n = min(out["expected_points"] or len(values_a), len(values_a), len(values_b))
    equal_count = sum(1 for a, b in zip(values_a[:n], values_b[:n]) if _values_equal(a, b))
    out.update({
        "col_a_index": col_a,
        "col_b_index": col_b,
        "header_row": data_start_row - 1,
        "data_start_row": data_start_row,
        "compared_points": n,
        "equal_points": equal_count,
        "first_values": _sample_segments(values_a[:n], max_each=3),
        "sample_from_evidence": pair.get("sample_a", [])[:5],
        "verified": bool(n >= min(out["expected_points"], DETERMINISTIC_FASTPATH_MIN_POINTS) and equal_count == n),
    })
    return out


def _evidence_data_start_row(pair: dict, default: int = 3) -> int:
    try:
        row = int(pair.get("data_start_row") or 0)
    except (TypeError, ValueError):
        row = 0
    if row > 0:
        return row

    for key in ("header_row", "col_a_header_row", "col_b_header_row"):
        try:
            row = int(pair.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if row > 0:
            return row + 1
    return default


def _verify_reuse_rep(wb, rep: dict) -> dict:
    locations = rep.get("locations") or []
    out = {
        "kind": "cross_sheet_reuse",
        "locations": locations,
        "expected_points": int(rep.get("n_values") or 0),
        "sample_from_evidence": rep.get("sample", [])[:5],
        "verified": False,
    }
    resolved = []
    for loc in locations[:2]:
        _file, sheet, header = _split_location(loc)
        found = _find_column_by_header_and_sample(wb, sheet, header, rep.get("sample") or [])
        if not found:
            out["error"] = f"column not found for {loc}"
            return out
        resolved.append({
            "location": loc,
            "sheet": sheet,
            "header": header,
            "column_index": found["column_index"],
            "header_row": found["header_row"],
            "values": found["values"],
        })
    if len(resolved) < 2:
        out["error"] = "fewer than two locations"
        return out
    values_a = resolved[0]["values"]
    values_b = resolved[1]["values"]
    n = min(out["expected_points"] or len(values_a), len(values_a), len(values_b))
    equal_count = sum(1 for a, b in zip(values_a[:n], values_b[:n]) if _values_equal(a, b))
    out.update({
        "resolved_locations": [
            {k: v for k, v in item.items() if k != "values"}
            for item in resolved
        ],
        "compared_points": n,
        "equal_points": equal_count,
        "first_values": _sample_segments(values_a[:n], max_each=3),
        "verified": bool(n >= min(out["expected_points"], DETERMINISTIC_FASTPATH_MIN_POINTS) and equal_count == n),
    })
    return out


def _build_verified_deterministic_evidence(
    doi: str,
    input_dir: str,
    evidence: dict,
    sample_limit: int = DETERMINISTIC_FASTPATH_SAMPLE_LIMIT,
) -> dict | None:
    """Verify a bounded sample of strong deterministic data findings directly
    against the original workbook before asking an LLM to write the review.
    """
    duplicate_groups = _strong_duplicate_column_groups(evidence)
    reuse_groups = _strong_cross_sheet_groups(evidence)
    if not duplicate_groups and not reuse_groups:
        return None

    workbook_name = "Source data.xlsx"
    if duplicate_groups:
        workbook_name = duplicate_groups[0].get("file") or workbook_name
    elif reuse_groups and reuse_groups[0]["representatives"]:
        first_loc = (reuse_groups[0]["representatives"][0].get("locations") or [""])[0]
        workbook_name = _split_location(first_loc)[0] or workbook_name

    workbook_path = _find_input_file(input_dir, workbook_name)
    if not workbook_path:
        return {
            "doi": doi,
            "version": DETERMINISTIC_FASTPATH_VERSION,
            "verified": False,
            "error": f"workbook not found: {workbook_name}",
        }

    try:
        import openpyxl
        wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    except Exception as e:
        return {
            "doi": doi,
            "version": DETERMINISTIC_FASTPATH_VERSION,
            "verified": False,
            "error": f"workbook unreadable: {e}",
            "workbook": str(workbook_path),
        }

    try:
        checks = []
        checked_sheets = set()

        def _can_add_sheets(sheet_names: list[str]) -> bool:
            new_sheets = set(sheet_names) - checked_sheets
            return len(checked_sheets) + len(new_sheets) <= sample_limit

        for group in duplicate_groups:
            for pair in group.get("representatives") or []:
                sheet = str(pair.get("sheet") or "")
                if not _can_add_sheets([sheet]):
                    continue
                check = _verify_duplicate_pair(wb, pair)
                checks.append(check)
                if sheet:
                    checked_sheets.add(sheet)

        for group in reuse_groups:
            for rep in group.get("representatives") or []:
                sheets = [_split_location(loc)[1] for loc in rep.get("locations") or [] if _split_location(loc)[1]]
                if not _can_add_sheets(sheets[:2]):
                    continue
                check = _verify_reuse_rep(wb, rep)
                check["group_scope"] = group.get("scope")
                check["group_count"] = group.get("count")
                checks.append(check)
                checked_sheets.update(sheets[:2])

        verified_checks = [c for c in checks if c.get("verified")]
        return {
            "doi": doi,
            "version": DETERMINISTIC_FASTPATH_VERSION,
            "workbook": str(workbook_path),
            "sample_limit_sheets": sample_limit,
            "checked_sheets": sorted(checked_sheets),
            "checked_sheet_count": len(checked_sheets),
            "verified": bool(verified_checks),
            "verified_check_count": len(verified_checks),
            "check_count": len(checks),
            "checks": checks,
            "candidate_strong_evidence": {
                "duplicate_column_groups": duplicate_groups[:5],
                "cross_sheet_reuse_groups": reuse_groups[:5],
            },
            "interpretation_hint": (
                "Only use these checks as high-risk evidence when the compared columns are measurement/dependent-value "
                "columns, not shared x-axis or independent-variable columns."
            ),
        }
    finally:
        with contextlib.suppress(Exception):
            wb.close()


def _build_deterministic_agent_prompt(
    doi: str,
    report_json_path: str,
    input_dir: str,
    verified_evidence: dict,
) -> str:
    compact = dict(verified_evidence)
    compact["checks"] = [
        {
            k: v for k, v in check.items()
            if k not in {"error"} or v
        }
        for check in verified_evidence.get("checks", [])
    ]
    return DETERMINISTIC_AGENT_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        verified_evidence_json=json.dumps(compact, ensure_ascii=False, indent=2, default=str),
    ) + PUBLIC_WRITING_RULES


def _source_data_audit_highrisk_review_result(doi: str, evidence: dict) -> dict | None:
    audit = ((evidence or {}).get("deterministic_findings") or {}).get("source_data_audit") or {}
    high = audit.get("high_findings") or []
    if not high:
        return None

    matrix_items = [
        item for item in audit.get("rectangular_matrix_reuse") or []
        if item.get("severity") == "high"
    ]
    near_modified_items = [
        item for item in audit.get("near_modified_matrix_reuse") or []
        if item.get("severity") == "high"
    ]
    digit_edit_items = [
        item for item in audit.get("digit_edit_matrix_reuse") or []
        if item.get("severity") == "high"
    ]
    n_items = [
        item for item in audit.get("figure_n_mismatch") or []
        if item.get("severity") == "high"
    ]
    exact_series_items = [
        item for item in audit.get("same_sheet_exact_series_reuse") or []
        if item.get("severity") == "high"
    ]
    ratio_items = [
        item for item in audit.get("fixed_ratio_series") or []
        if item.get("severity") == "high"
    ]
    data_parts = []
    reason_parts = []
    excel_evidence_regions = []

    for item in matrix_items[:3]:
        sample = _sample_text(item.get("sample") or [])
        description = str(item.get("description") or "").rstrip("。")
        basis = (
            f"示例数值为 {sample}"
            if sample
            else "按两个标明区域中的数值逐格比对"
        )
        data_parts.append(
            f"问题位置：{item.get('location')}。"
            "问题类型：跨图或跨文件数值矩阵重复。"
            f"具体情况：{description}。"
            "需关注原因：不同图号或不同源数据文件中，整块测量值逐格完全相同，"
            "不能用共享横轴、坐标模板或少量公共基线解释。"
            f"核查依据：{basis}。"
        )
        regions = []
        for side_name, side_label in (("left", "区域A"), ("right", "区域B")):
            side = item.get(side_name) or {}
            if not all(side.get(key) for key in ("file", "sheet", "row_range", "col_range")):
                continue
            regions.append({
                "label": side_label,
                "file": side["file"],
                "sheet": side["sheet"],
                "row_range": side["row_range"],
                "col_range": side["col_range"],
            })
        if len(regions) == 2:
            excel_evidence_regions.append({
                "dimension": "data",
                "issue_index": len(data_parts),
                "regions": regions,
                "caption": (
                    f"Excel证据截图：{item.get('location')}。"
                    "图片直接读取原工作簿中标明的单元格区域并保留行列坐标。"
                ),
            })
    for item in near_modified_items[:3]:
        sample_pairs = item.get("sample_pairs") or []
        sample = "、".join(
            f"{pair.get('left')} → {pair.get('right')}" for pair in sample_pairs[:4]
        )
        data_parts.append(
            f"问题位置：{item.get('location')}。"
            "问题类型：不同图中的对应测量值保留相同前两位小数，但后续小数位被系统性轻微改写。"
            f"具体情况：{item.get('description')}。"
            "需关注原因：这些图对应不同实验比较；若确实沿用同一批原始测量，数值应完全一致，"
            "若为独立测量，则不应所有对应值都保留相同的前两位小数。"
            f"核查依据：逐格比较原始工作簿中的两个矩阵"
            + (f"；示例为 {sample}。" if sample else "。")
        )
        regions = []
        for side_name, side_label in (("left", "区域A"), ("right", "区域B")):
            side = item.get(side_name) or {}
            if not all(side.get(key) for key in ("file", "sheet", "row_range", "col_range")):
                continue
            regions.append({
                "label": side_label,
                "file": side["file"],
                "sheet": side["sheet"],
                "row_range": side["row_range"],
                "col_range": side["col_range"],
            })
        if len(regions) == 2:
            excel_evidence_regions.append({
                "dimension": "data",
                "issue_index": len(data_parts),
                "regions": regions,
                "caption": (
                    f"Excel证据截图：{item.get('location')}。"
                    "图片直接读取原工作簿并保留行列坐标，可逐格比较相同前缀与后续小数差异。"
                ),
            })
    if near_modified_items:
        strongest = near_modified_items[0]
        total_values = sum(int(item.get("n_values") or 0) for item in near_modified_items)
        total_matching = sum(int(item.get("matching_prefix_count") or 0) for item in near_modified_items)
        total_changed = sum(int(item.get("changed_count") or 0) for item in near_modified_items)
        aggregate = (
            f"合计 {total_matching}/{total_values} 个对应值的前两位小数一致，"
            f"{total_changed}/{total_values} 个值的后续小数位不同；"
            if len(near_modified_items) > 1
            else ""
        )
        reason_parts.append(
            f"数据维度：确认不同图的对应值存在末位小数系统性改写；{aggregate}"
            f"其中最大连续区域位于 {strongest.get('location')}，该区域 "
            f"{strongest.get('matching_prefix_count')}/{strongest.get('n_values')} 个值的前两位小数一致，"
            f"{strongest.get('changed_count')}/{strongest.get('n_values')} 个值的后续小数位不同。"
        )
    for item in digit_edit_items[:3]:
        operation_names = {"substitute": "替换", "insert": "插入", "delete": "删除"}
        position_names = {
            "integer": "整数位",
            "decimal_first": "小数首位",
            "decimal_middle": "小数中间位",
            "decimal_last": "小数末位",
        }
        sample = "、".join(
            f"{pair.get('left')} → {pair.get('right')}"
            f"（{' + '.join(operation_names.get(value, value) for value in pair.get('operations', []))}/"
            f"{' + '.join(position_names.get(value, value) for value in pair.get('positions', []))}，"
            f"{pair.get('edit_count')}次编辑）"
            for pair in (item.get("sample_pairs") or [])[:4]
        )
        max_edits = int(item.get("max_edits_per_value") or 2)
        data_parts.append(
            f"问题位置：{item.get('location')}。"
            "问题类型：不同图中的对应数值疑似复制后逐项替换、插入或删除少量数字。"
            f"具体情况：{item.get('description')}。"
            f"需关注原因：普通独立测量可以数值接近，但不应让几乎整块数据的数字串都只差不超过"
            f" {max_edits} 次编辑；"
            "这种逐格保留绝大多数数字的模式更符合复制后微调。"
            "核查依据：将数值统一为不补零的数字串并保留符号和小数点，"
            "对每个对应值计算数字编辑关系"
            + (f"；示例为 {sample}。" if sample else "。")
        )
        regions = []
        for side_name, side_label in (("left", "区域A"), ("right", "区域B")):
            side = item.get(side_name) or {}
            if not all(side.get(key) for key in ("file", "sheet", "row_range", "col_range")):
                continue
            regions.append({
                "label": side_label,
                "file": side["file"],
                "sheet": side["sheet"],
                "row_range": side["row_range"],
                "col_range": side["col_range"],
            })
        if len(regions) == 2:
            excel_evidence_regions.append({
                "dimension": "data",
                "issue_index": len(data_parts),
                "regions": regions,
                "caption": (
                    f"Excel证据截图：{item.get('location')}。"
                    "图片保留原工作簿行列坐标，可逐格核查数字替换、插入或删除。"
                ),
            })
    if digit_edit_items:
        strongest = digit_edit_items[0]
        max_edits = int(strongest.get("max_edits_per_value") or 2)
        reason_parts.append(
            f"数据维度：确认跨图对应数值存在系统性少量数字编辑；{strongest.get('location')} 中 "
            f"{strongest.get('matched_count')}/{strongest.get('n_values')} 个值完全相同或最多相差"
            f" {max_edits} 次数字编辑，"
            f"其中 {strongest.get('changed_count')}/{strongest.get('n_values')} 个值确有改动。"
        )
    if matrix_items:
        strongest = matrix_items[0]
        reason_parts.append(
            f"数据维度：确认跨图/跨文件数值矩阵复用；{strongest.get('location')} "
            f"共有 {strongest.get('n_values')} 个测量值逐格一致。"
        )

    for item in n_items[:3]:
        description = str(item.get("description") or "").rstrip("。")
        data_parts.append(
            f"问题位置：{item.get('location')}。"
            "问题类型：图注样本量与源数据重复列数不一致。"
            f"具体情况：{description}。"
            "需关注原因：样本量是统计检验和误差线解释的基础，图注声明的 n "
            "与源数据中可见重复列数不一致，会直接影响图文一致性和统计独立性判断。"
            f"核查依据：图注声明 n={item.get('declared_n')}，"
            f"源数据中可见 {item.get('source_n')} 列重复测量。"
        )
    if n_items:
        strongest = n_items[0]
        reason_parts.append(
            f"研究设计/统计核查：确认图注 n 值与源数据重复列数不一致；"
            f"{strongest.get('location')} 声明 n={strongest.get('declared_n')}，"
            f"源数据为 {strongest.get('source_n')} 列。"
        )

    for item in exact_series_items[:6]:
        sample = _sample_text(item.get("sample") or [])
        description = str(item.get("description") or "").rstrip("。")
        basis = (
            f"示例数值为 {sample}"
            if sample
            else "按两个标明序列中的连续数值逐项比对"
        )
        data_parts.append(
            f"问题位置：{item.get('location')}。"
            "问题类型：同一工作表不同图面板数值精确复用。"
            f"具体情况：{description}。"
            "需关注原因：不同图面板中的连续高精度测量值逐值完全相同，"
            "不能用共享横轴、坐标模板或少量共同对照解释。"
            f"核查依据：{basis}。"
        )
    if exact_series_items:
        strongest = exact_series_items[0]
        reason_parts.append(
            f"数据维度：确认同一工作表不同图面板存在连续测量值精确复用；"
            f"{strongest.get('location')} 共 {strongest.get('n_values')} 个值一致。"
        )

    for item in ratio_items[:4]:
        description = str(item.get("description") or "").rstrip("。")
        data_parts.append(
            f"问题位置：{item.get('location')}。"
            "问题类型：不同测量序列呈固定比例派生。"
            f"具体情况：{description}。"
            "需关注原因：不同时间点、剂量或浓度下的独立测量应包含个体随机波动；"
            "大多数或全部重复逐点共享同一缩放系数，符合数值派生模式而非普通相关。"
            f"核查依据：{item.get('matched_count')}/{item.get('total_compared')} 个对应值"
            f"共享比例 {float(item.get('ratio') or 0):.8g}。"
        )
    if ratio_items:
        strongest = ratio_items[0]
        reason_parts.append(
            f"数据维度：确认固定比例派生模式；{strongest.get('location')} 中 "
            f"{strongest.get('matched_count')}/{strongest.get('total_compared')} 个对应值"
            f"共享比例 {float(strongest.get('ratio') or 0):.8g}。"
        )
        reason_parts.append(
            "研究设计/统计核查：固定比例关系破坏了时间点、剂量或浓度组之间独立测量误差的可解释性。"
        )

    reason_parts.append(
        "图像维度：图像线索不是本次自动直达高风险的主要依据。"
    )
    reason_parts.append(
        "参考文献维度：本次自动直达高风险不依赖参考文献异常。"
    )
    reason_parts.append(
        "最终判定：源数据确定性审计已发现可复核的硬证据，建议高风险。"
    )

    trigger_types = sorted({item.get("test") for item in high if item.get("test")})
    return {
        "doi": doi,
        "result": "高风险",
        "trigger": "源数据确定性审计：" + "、".join(trigger_types),
        "image_review": (
            "图像复核：本次结论主要依据源数据确定性审计结果；"
            "图像相似性线索未作为自动高风险判定的主要依据。"
        ),
        "data_review": "\n\n".join(f"（{index}）{part}" for index, part in enumerate(data_parts, 1)),
        "ref_review": "参考文献复核：未作为本次自动高风险判定的主要依据。",
        "methodology_review": "\n".join(
            part for part in reason_parts if part.startswith("研究设计/统计核查")
        ),
        "verdict": "建议高风险",
        "reason": "\n".join(reason_parts),
        "dimension_verdicts": {
            "image": "建议低风险",
            "data": "建议高风险",
            "ref": "建议低风险",
            "methodology": "建议高风险" if (n_items or ratio_items) else "建议低风险",
        },
        # Put near-modified matrices first: they are less visually obvious than
        # exact copies and need the side-by-side cells to remain explainable.
        "excel_evidence_regions": sorted(
            excel_evidence_regions,
            key=lambda item: "后续小数差异" not in str(item.get("caption") or ""),
        ),
        "_source_data_audit_version": audit.get("version"),
        "_fastpath_version": DETERMINISTIC_FASTPATH_VERSION,
    }


def _deterministic_highrisk_review_result(doi: str, evidence: dict) -> dict | None:
    """Create a substantive high-risk review when deterministic evidence is already
    strong enough to avoid another tool-heavy LLM pass.

    This is intentionally limited to long measurement-value repetitions. Image
    and reference findings still require normal review unless data evidence alone
    supports the paper-level verdict.
    """
    if not DETERMINISTIC_FASTPATH_ENABLED or not evidence:
        return None

    duplicate_groups = _strong_duplicate_column_groups(evidence)
    reuse_groups = _strong_cross_sheet_groups(evidence)
    if not duplicate_groups and not reuse_groups:
        return None

    data_parts = []
    reason_parts = []

    if duplicate_groups:
        group = duplicate_groups[0]
        reps = group["representatives"]
        details = []
        for rep in reps:
            details.append(
                f"{rep.get('group_a')} 组的 {rep.get('col_a_header')} 列与 "
                f"{rep.get('group_b')} 组的 {rep.get('col_b_header')} 列 "
                f"{int(rep.get('n_points') or 0)} 个数据点逐值一致"
                + (f"，开头数值为 {_sample_text(rep.get('sample_a'))}" if rep.get("sample_a") else "")
            )
        data_parts.append(
            f"问题位置：{group['file']} 的 {group['sheet']} 工作表。"
            f"发现：{'; '.join(details)}。"
            "为什么重要：这些列位于不同分组下，列名为测量值列而非时间、浓度、波数等横轴变量，"
            "大段测量值逐点完全一致不能用共享坐标轴解释。"
            "核查依据：确定性证据包已按列级别比对原始工作簿，重复覆盖数千个连续数据点。"
        )
        reason_parts.append(
            f"数据维度：{group['file']} 的 {group['sheet']} 工作表存在不同分组测量值列逐值重复，"
            f"最长覆盖 {group['max_points']} 个数据点；代表样例显示 {details[0]}。"
        )

    if reuse_groups:
        reuse_texts = []
        for idx, group in enumerate(reuse_groups[:2], 1):
            rep_texts = []
            for rep in group["representatives"]:
                rep_texts.append(
                    f"{_human_locations(rep.get('locations') or [])} 之间 "
                    f"{int(rep.get('n_values') or 0)} 个数值一致"
                    + (f"，开头数值为 {_sample_text(rep.get('sample'))}" if rep.get("sample") else "")
                )
            reuse_texts.append(
                f"跨表复用第 {idx} 组：{'; '.join(rep_texts)}"
            )
        data_parts.append(
            "问题位置：Source data.xlsx 中多个工作表的测量值列。"
            f"发现：{'；'.join(reuse_texts)}。"
            "为什么重要：这些复用发生在不同工作表或不同图表分组之间，且每组覆盖上千到数万个连续测量值，"
            "不是少量公共零值或共享横轴造成的偶然重合。"
            "核查依据：证据包已把原始跨表重复聚类为分组并保留代表样例，代表样例均为长序列逐值一致。"
        )
        strongest = reuse_groups[0]["representatives"][0]
        reason_parts.append(
            "数据维度：跨工作表测量值复用分组同样支持高风险；"
            f"例如 {_human_locations(strongest.get('locations') or [])} "
            f"之间 {int(strongest.get('n_values') or 0)} 个数值一致。"
        )

    det = (evidence or {}).get("deterministic_findings") or {}
    precision = det.get("decimal_precision_mismatch") or []
    methodology = ""
    if precision:
        headers = sorted({str(item.get("header") or "") for item in precision if item.get("header")})
        spreads = [
            item.get("precision_spread") for item in precision
            if isinstance(item.get("precision_spread"), list)
        ]
        methodology = (
            f"研究设计与统计核查：同名测量值列 {', '.join(headers[:3]) or 'y'} "
            f"在多个工作表中存在小数位跨度差异"
            + (f"（例如 {spreads[0][0]} 位到 {spreads[0][1]} 位）" if spreads else "")
            + "。该项单独看可能受导出格式影响，但与上述长序列测量值重复同时出现，增加了数据完整性风险。"
        )

    image_review = (
        "图像复核：初检存在图像重复线索，但本次结论主要依据源数据中的强确定性异常。"
        "在数据证据已经足以支持总风险结论的情况下，图像线索不作为本次最终判定的主要依据。"
    )
    ref_review = (
        "参考文献复核：初检参考文献线索为数据集 DOI 未由 CrossRef 验证。"
        "这类 DOI 可由其他注册机构管理，单独不构成本次高风险判定依据。"
    )
    reason_parts.append(
        "图像维度：图像线索未作为主要判定依据；当前高风险结论不依赖图像重复是否成立。"
    )
    reason_parts.append(
        "参考文献维度：数据集 DOI 未被 CrossRef 验证不作为主要判定依据。"
    )
    reason_parts.append(
        "最终判定：源数据中存在多个不同分组或不同工作表的长序列测量值逐值一致，"
        "位置和样例均可由确定性证据追溯，已足以支持建议高风险。"
    )

    return {
        "doi": doi,
        "result": "高风险",
        "trigger": "强确定性数据证据：不同分组或不同工作表的测量值长序列逐值重复",
        "image_review": image_review,
        "data_review": "\n".join(data_parts),
        "ref_review": ref_review,
        "methodology_review": methodology,
        "verdict": "建议高风险",
        "reason": "\n".join(reason_parts),
        "dimension_verdicts": {
            "image": "建议低风险",
            "data": "建议高风险",
            "ref": "建议低风险",
            "methodology": "建议低风险",
        },
        "_fastpath_version": DETERMINISTIC_FASTPATH_VERSION,
    }


def _validate_review_result(
    doi: str,
    report_json_path: str,
    input_dir: str,
    result: dict,
    evidence: dict,
) -> dict:
    # The CLI may omit the bookkeeping labels even when all substantive
    # sections are present.  Normalize before coverage validation so a schema
    # omission does not force an unnecessary retry or a review_error fallback.
    result = _normalize_review_result(result)
    if not evidence:
        return _apply_coverage_validation(doi, report_json_path, input_dir, result)
    try:
        if _SKILL_SCRIPTS not in sys.path:
            sys.path.insert(0, _SKILL_SCRIPTS)
        from api.services.review_validation import validate
        validated, gaps = validate(result, evidence)
        if gaps:
            log.warning("DOI=%s coverage validator warning; gaps=%s", doi, gaps)
        return validated
    except Exception as e:
        log.error("DOI=%s coverage validation errored: %s", doi, e)
        return _fail_closed_on_evidence_missing(doi, result, f"coverage validation errored: {e}")


def _fail_closed_on_evidence_missing(doi: str, result: dict, reason: str) -> dict:
    """Block publication when evidence is unavailable; never invent a verdict."""
    out = dict(result)
    out["_coverage_status"] = "unavailable"
    out["_coverage_error"] = reason
    out["_review_warning"] = (
        f"复核证据包不可用（{reason}），当前判断不能作为最终结果发布；"
        "需恢复证据并通过校验。流程异常不改变论文风险结论。"
    )
    log.warning("DOI=%s publication blocked: evidence unavailable (%s)", doi, reason)
    return out


def _evidence_has_actionable_signal(evidence: dict | None) -> bool:
    """Return whether the evidence bundle contains anything needing a second pass."""
    evidence = evidence or {}
    counts = ((evidence.get("coverage_manifest") or {}).get("counts") or {})
    signal_keys = (
        "high_findings", "duplicate_column_pairs", "cross_sheet_reuse",
        "cross_sheet_reuse_groups", "decimal_precision_mismatch",
        "trailing_digit_uniformity", "passage_offset_pattern",
        "source_data_audit_high", "source_data_matrix_reuse",
        "source_data_digit_edit_reuse", "source_data_n_mismatch",
        "source_data_exact_series_reuse", "source_data_fixed_ratio_series",
    )
    if any(int(counts.get(key) or 0) > 0 for key in signal_keys):
        return True
    det = evidence.get("deterministic_findings") or {}
    return any(bool(det.get(key)) for key in (
        "high_findings", "duplicate_column_pairs", "cross_sheet_reuse",
        "cross_sheet_reuse_groups", "decimal_precision_mismatch",
        "trailing_digit_uniformity", "passage_offset_pattern",
    ))


def _accept_single_review(doi: str, result: dict, validated: dict, evidence: dict) -> tuple[bool, str]:
    if result.get("trigger") == "review_error":
        return False, "首个复核失败"
    if not _valid_review_result(validated):
        return False, "首个复核结果无效"
    if validated.get("_coverage_error"):
        return False, "首个复核的覆盖校验不可用，需要再次核验"
    if validated.get("_coverage_gaps"):
        return False, "首个复核未覆盖全部维度或高风险线索，需要再次核验"
    prior = _prior_review_result_for_doi(doi)
    if (
        os.environ.get("REVIEW_ACCEPT_FIRST_LOW", "0") == "1"
        and result.get("result") == "低风险"
        and validated.get("result") == "低风险"
        and not validated.get("_coverage_gaps")
        and not validated.get("_coverage_error")
    ):
        return True, "运行配置允许首轮低风险直接闭环"
    if prior == "高风险":
        return False, "既有复核结论为高风险，需要多轮对抗核验后才能闭环"
    if (
        FAST_CLEAN_LOWRISK
        and result.get("result") == "低风险"
        and validated.get("result") == "低风险"
        and not _evidence_has_actionable_signal(evidence)
        and not validated.get("_coverage_gaps")
        and not validated.get("_coverage_error")
    ):
        return True, "证据包无高风险线索且首轮覆盖校验通过，直接闭环"
    if result.get("result") == "高风险" and validated.get("result") == "高风险":
        if _has_strong_deterministic_evidence(evidence):
            return True, "首个复核已确认强确定性证据，直接收敛为高风险结论"
        return True, "首个复核已给出高风险结论"
    if (
        prior == "低风险"
        and result.get("result") == "低风险"
        and validated.get("result") == "低风险"
        and not validated.get("_coverage_gaps")
        and not validated.get("_coverage_error")
    ):
        return True, "既有复核结论为低风险，首轮完整复核仍为低风险，直接闭环"
    if validated.get("_coverage_gaps"):
        return False, "首个低风险结论存在覆盖率留痕提醒，需要二次核验"
    return False, "首个复核为低风险，需要二次核验"


def _accept_lowrisk_consensus(doi: str, result1: dict, validated1: dict, result2: dict, validated2: dict) -> bool:
    if _prior_review_result_for_doi(doi) == "高风险":
        return False
    return (
        result1.get("trigger") != "review_error"
        and result2.get("trigger") != "review_error"
        and validated1.get("result") == "低风险"
        and validated2.get("result") == "低风险"
        and not validated1.get("_coverage_error")
        and not validated2.get("_coverage_error")
        # A low-risk consensus is publishable only when both reviewers
        # explicitly covered every deterministic finding.  Without this
        # guard, an omitted item such as Fig. 3E's identical channel columns
        # could be carried into a final low-risk result as a mere warning.
        and not validated1.get("_coverage_gaps")
        and not validated2.get("_coverage_gaps")
    )


def _is_review_error(result: dict | None) -> bool:
    return isinstance(result, dict) and result.get("trigger") == "review_error"


def _has_unresolved_coverage(result: dict | None) -> bool:
    """Return whether a cached result still has a coverage blocker.

    Cached finals with unresolved semantic gaps must be re-entered into the
    review/repair flow. Otherwise ``REVIEW_RESUME_RESULTS=1`` could resurrect
    a stale low-risk result that omitted a deterministic finding.
    """
    return isinstance(result, dict) and bool(
        result.get("_coverage_gaps") or result.get("_coverage_error")
    )


def _normalize_review_result(result: dict | None) -> dict:
    """Normalize the container without deriving dimension verdicts from overall risk.

    Missing labels are returned to the reviewer by the shared preflight gate.
    A data-only finding must not silently become four high-risk dimensions.
    """
    out = dict(result or {})
    aliases = {'low_risk': '低风险', 'high_risk': '高风险'}
    risk = out.get('result')
    if isinstance(risk, str):
        out['result'] = aliases.get(risk.strip().lower(), risk)
    dimensions = out.get('dimension_verdicts')
    out['dimension_verdicts'] = dict(dimensions) if isinstance(dimensions, dict) else {}
    # Keep the model's explanatory sentence, while canonicalizing the label
    # used by the publication gate. This is an explicit dimension conclusion,
    # not an inference from the paper-level result.
    for dimension, value in list(out['dimension_verdicts'].items()):
        if not isinstance(value, str):
            continue
        if '高风险' in value or '有风险' in value or 'risk identified' in value.lower():
            out['dimension_verdicts'][dimension] = '建议高风险'
        elif ('低风险' in value or '未发现风险' in value
              or 'no risk' in value.lower() or 'low risk' in value.lower()):
            out['dimension_verdicts'][dimension] = '建议低风险'
    # Models sometimes place ``问题1：`` immediately after the dimension
    # heading. The report layout binds screenshots to numbered findings at
    # line starts; normalize this harmless formatting variation before the
    # coverage validator so a valid screenshot is not rejected and sent back
    # through another expensive model repair pass.
    for field in ('image_review', 'data_review', 'ref_review', 'methodology_review', 'reason'):
        value = out.get(field)
        if isinstance(value, str) and value:
            value = re.sub(r'(?<!\n)(问题\s*\d+\s*[:：])', r'\n\1', value)
            # Also accept the common heading form ``问题1`` followed by a
            # newline. Add the delimiter required by the shared report
            # layout without changing the reader-facing wording.
            value = re.sub(r'(问题\s*\d+)(?=\s*(?:\n|$))', r'\1：', value)
            out[field] = value
    return out


async def _kill_and_reap_process(proc) -> None:
    """Stop a timed-out review process and reap it before returning.

    Previously timeout paths called ``kill()`` and immediately returned.  The
    Claude parent (and, depending on the CLI version, its tool subprocesses)
    could therefore remain alive while the retry started, exhausting process
    slots and making subsequent reviews appear to fail or hang.
    """
    from api.services.report import _kill_process_group
    await _kill_process_group(proc)


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
        and not _has_unresolved_coverage(r)
    ]
    if len(substantive) != 1:
        return None
    return _with_review_warning(
        substantive[0],
        doi,
        "二次核验或综合判定因流程错误未完成；本结论未因流程错误自动上调风险。",
    )


def _is_lowrisk_result(result: dict) -> bool:
    v = (result.get("verdict") or "") + (result.get("result") or "")
    return ("低风险" in v) and ("高风险" not in (result.get("result") or ""))


def _apply_coverage_validation(doi: str, report_json_path: str, input_dir: str, result: dict) -> dict:
    """Build the deterministic evidence bundle and validate the synthesis result against
    it. Coverage gaps are annotations; they do not change the review's risk
    result by themselves.

    2026-06-23 fail-closed fix: when the evidence bundle is unavailable, a low-risk
    verdict is unsafe (5 漏判 root cause: evidence timeout → rubber-stamp 低风险).
    We escalate to 高风险 with an audit warning so operators must rerun review with
    a working bundle to down-grade. High-risk results are kept (LLM still flagged
    genuine concerns).
    """
    try:
        if _SKILL_SCRIPTS not in sys.path:
            sys.path.insert(0, _SKILL_SCRIPTS)
        from api.services.review_validation import validate
        evidence_path = _ensure_evidence_bundle(doi, report_json_path, input_dir)
        if not evidence_path:
            return _fail_closed_on_evidence_missing(doi, result, "evidence bundle unavailable")
        bundle = _load_json_file(evidence_path)
        if not bundle:
            return _fail_closed_on_evidence_missing(doi, result, "evidence bundle unreadable")
    except Exception as e:
        log.error("DOI=%s evidence bundle build failed: %s", doi, e)
        return _fail_closed_on_evidence_missing(doi, result, f"evidence bundle build failed: {e}")
    try:
        validated, gaps = validate(result, bundle)
        if gaps:
            log.warning("DOI=%s coverage validator warning; gaps=%s", doi, gaps)
        return validated
    except Exception as e:
        log.error("DOI=%s coverage validation errored: %s", doi, e)
        return _fail_closed_on_evidence_missing(doi, result, f"coverage validation errored: {e}")


def _preprocess_review_input_dir(input_dir: str) -> None:
    """Optionally inspect preprocessing opportunities without modifying sources."""
    if os.environ.get("PAPER_CLEAN_JUNK", "1") != "0":
        try:
            from utils.paper_preprocessor import clean_junk_files

            stats = clean_junk_files(input_dir, dry_run=False, log_fn=log.info)
            removed = sum(stats.get("actions", {}).values())
            if removed:
                log.info("review clean-junk removed %d noise entries in %s", removed, input_dir)
            for warning in stats.get("warnings", []):
                log.warning("review clean-junk: %s", warning)
        except Exception as e:
            log.warning("review clean-junk failed for %s: %s", input_dir, e)

    if os.environ.get("PAPER_PREPROCESS", "0") != "1":
        return
    try:
        from utils.paper_preprocessor import preprocess_paper_dir

        stats = preprocess_paper_dir(input_dir, dry_run=True, log_fn=log.info)
        if stats.get("actions", {}).get("duplicates_removed"):
            log.info(
                "review preprocess dry-run found %d duplicate files in %s",
                stats["actions"]["duplicates_removed"],
                input_dir,
            )
        for warning in stats.get("warnings", []):
            log.warning("review preprocess: %s", warning)
    except Exception as e:
        log.warning("review preprocess failed for %s: %s", input_dir, e)


async def _run_review_single_impl(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    resume: bool = False,
) -> dict:
    """Adaptive review with per-paper evidence caching.

    Start with one review pass. A first-pass high-risk result is accepted
    immediately. A first-pass low-risk result always requires a second verifier;
    two low-risk results with coverage validation pass without a synthesis pass.

    By default every invocation runs fresh LLM review stages so an explicit
    re-review cannot silently return an old final verdict. Set
    REVIEW_RESUME_RESULTS=1 only for controlled recovery jobs that intentionally
    resume interrupted reviewer/judge outputs.
    """
    doi = normalize_doi(doi) or doi
    _preprocess_review_input_dir(input_dir)
    artifacts_dir = _review_artifacts_dir(output_dir)
    state_path = _review_state_path(doi, report_json_path, input_dir, artifacts_dir)
    legacy_state_path = _review_state_path(doi, report_json_path, input_dir)
    if not state_path.exists() and legacy_state_path.exists():
        state = _load_review_state(legacy_state_path, doi, report_json_path, input_dir)
    else:
        state = _load_review_state(state_path, doi, report_json_path, input_dir)

    # A DOI alone is not an input identity: other submissions may contain
    # different source data. Never reuse another submission's review.
    resume_results = resume or REVIEW_RESUME_RESULTS
    # Keep CLI session recovery task-local while this paper runs. Fresh
    # reviews must never inherit another invocation's session checkpoint.
    if not resume_results:
        # Explicit fresh reviews get their own stage history. Preserve older
        # artifacts for audit, but do not let a future resume select old facts.
        if state.get("stages"):
            archive = state_path.with_name(state_path.stem + "_" + uuid.uuid4().hex + ".history.json")
            _save_review_state(archive, dict(state))
        state["stages"] = {k: v for k, v in state.get("stages", {}).items()
                           if k in ("evidence_path", "llm_evidence_path", "evidence_counts")}
        state["cli_run_id"] = uuid.uuid4().hex
        _save_review_state(state_path, state)
    _REVIEW_CLI_CONTEXT.set({"run_id": state.get("cli_run_id", ""),
                             "scope": str(state_path.resolve()), "resume": resume_results})

    cached_final = _get_stage(state, "final") if resume_results else None
    if _usable_cached_review_result(cached_final):
        cached_final = _validate_review_result(
            doi, report_json_path, input_dir, cached_final,
            _load_json_file(_get_stage(state, "evidence_path")),
        )
        if not _has_unresolved_coverage(cached_final):
            return _clean_internal_references(dict(cached_final))

    evidence_path = _get_stage(state, "evidence_path")
    rebuild_evidence = (not evidence_path or not Path(evidence_path).exists()
                        or _load_json_file(evidence_path).get('_fallback_reason'))
    if rebuild_evidence:
        evidence_path = await limited_evidence(
            _ensure_evidence_bundle,
            doi,
            report_json_path,
            input_dir,
            artifacts_dir,
        )
        _store_stage(state_path, state, "evidence_path", evidence_path)
    else:
        localized = _localize_artifact(evidence_path, artifacts_dir)
        if localized != evidence_path:
            evidence_path = localized
            _store_stage(state_path, state, "evidence_path", evidence_path)

    evidence = _load_json_file(evidence_path)
    if evidence and evidence.get('_fallback_reason'):
        failed = _fallback_result(doi, 'Evidence preparation incomplete; saved diagnostics retained; '
                                  'do not start model investigation from a partial inventory')
        failed['_retry_exhausted'] = True
        _store_stage(state_path, state, 'final', failed)
        return failed
    if evidence:
        _store_stage(state_path, state, "evidence_counts", _evidence_counts(evidence))

    # A deterministic source-data result is useful evidence, but it covers only
    # the data/methodology dimensions.  Keep it in the audit state and continue
    # through the full reviewer so image/reference findings cannot be skipped.
    source_audit_seed = _source_data_audit_highrisk_review_result(doi, evidence)
    if _usable_cached_review_result(source_audit_seed):
        _store_stage(state_path, state, "source_data_audit_seed", source_audit_seed)
        log.info(
            "DOI=%s source-data audit found strong evidence; continuing full multi-dimension review",
            doi,
        )

    llm_evidence_path = _get_stage(state, "llm_evidence_path")
    if evidence_path and (rebuild_evidence or not llm_evidence_path or not Path(llm_evidence_path).exists()):
        llm_evidence_path = _ensure_llm_evidence_bundle(doi, evidence_path)
        _store_stage(state_path, state, "llm_evidence_path", llm_evidence_path)
    else:
        localized = _localize_artifact(llm_evidence_path, artifacts_dir)
        if localized != llm_evidence_path:
            llm_evidence_path = localized
            _store_stage(state_path, state, "llm_evidence_path", llm_evidence_path)

    ev = EvidencePaths(llm_summary=llm_evidence_path or evidence_path, full=evidence_path)
    review_plan = _adaptive_review_plan(evidence)
    log.info(
        "DOI=%s adaptive review plan=%s max_turns=%s tools=%s",
        doi,
        review_plan.get("name"),
        review_plan.get("max_turns"),
        ",".join(review_plan.get("allowed_tools") or ()),
    )

    # A saved synthesis supersedes earlier provisional reviewer verdicts.
    # Revalidate it before considering a first-pass shortcut: a validator fix
    # must not roll a previously synthesized verdict back to reviewer1.
    #
    # A failed repair must not hide a saved substantive draft with gaps.
    saved_synthesis = False
    saved_synthesis_stage = ""
    saved_synthesis_candidate = None
    if resume_results:
        # New states retain the latest substantive draft in a stable slot.
        # Legacy states may preserve it only in coverage_repair_source.
        for stage_key in ("synthesis_candidate", "coverage_repair", "coverage_repair_validated", "coverage_repair_source", "judge"):
            candidate = _get_stage(state, stage_key)
            if not _valid_review_result(candidate) or _is_review_error(candidate):
                continue
            saved_synthesis = True
            saved_synthesis_stage = stage_key
            saved_synthesis_candidate = candidate
            break
    saved_later_review = saved_synthesis or (resume_results and _usable_cached_review_result(_get_stage(state, "reviewer2")))
    if resume_results and saved_synthesis_candidate is not None:
        candidate = _validate_review_result(
            doi, report_json_path, input_dir, saved_synthesis_candidate, evidence
        )
        if not _has_unresolved_coverage(candidate):
            final = _clean_internal_references(dict(candidate))
            _store_stage(state_path, state, "decision", {
                "flow": "resumed_synthesis",
                "source": saved_synthesis_stage,
            })
            _store_stage(state_path, state, "final", final)
            return final
        # Use the revalidated candidate below.  This matters when validation
        # adds a missing HIGH finding or a required dimension label to a draft
        # that looked complete in the raw checkpoint.
        saved_synthesis_candidate = candidate

    # Resume the latest substantive synthesis directly when its only problem
    # is publication coverage.  Do not restart the two full reviewer passes:
    # their evidence is already represented in this draft and rerunning them
    # is the main cause of long recovery loops and worker exhaustion.
    if resume_results and saved_synthesis_candidate is not None:
        candidate = _validate_review_result(
            doi, report_json_path, input_dir, saved_synthesis_candidate, evidence
        )
        gaps = list(candidate.get("_coverage_gaps") or [])
        if candidate.get("_coverage_error"):
            gaps.append(str(candidate["_coverage_error"]))
        if gaps:
            log.info(
                "DOI=%s review resume: repairing saved %s directly (%d coverage gaps)",
                doi, saved_synthesis_stage, len(gaps),
            )
            repaired = await _run_judge(
                doi, report_json_path, input_dir, output_dir,
                candidate, candidate, ev=ev, coverage_gaps=gaps,
            )
            _store_stage(state_path, state, "coverage_repair", repaired)
            repaired = _validate_review_result(
                doi, report_json_path, input_dir, repaired, evidence
            )
            _store_stage(state_path, state, "coverage_repair_validated", repaired)
            if not _has_unresolved_coverage(repaired) and not _is_review_error(repaired):
                final = _clean_internal_references(dict(repaired))
                _store_stage(state_path, state, "decision", {
                    "flow": "resumed_synthesis_repair",
                    "source": saved_synthesis_stage,
                })
                _store_stage(state_path, state, "final", final)
                return final
            # A bounded repair failure is a process failure.  Preserve the
            # substantive draft for the next recovery invocation and return an
            # explicit review_error now; restarting reviewer1/reviewer2 here
            # only repeats the expensive work and can overwrite better facts.
            _store_stage(state_path, state, "coverage_repair_source", candidate)
            error = repaired.get("_review_error") or repaired.get("reason") or (
                "; ".join(str(gap) for gap in (repaired.get("_coverage_gaps") or gaps))
            ) or "覆盖修复未完成"
            failed = _fallback_result(
                doi,
                f"已保存的综合复核覆盖修复失败：{error}",
            )
            failed["result"] = candidate.get("result", failed["result"])
            failed["verdict"] = candidate.get("verdict", failed["verdict"])
            failed["_review_candidate"] = candidate
            _store_stage(state_path, state, "decision", {
                "flow": "resumed_synthesis_repair_error",
                "source": saved_synthesis_stage,
                "error": error,
            })
            _store_stage(state_path, state, "final", failed)
            return failed

    result1 = _get_stage(state, "reviewer1") if resume_results else None
    if not _usable_cached_review_result(result1):
        result1 = await _run_with_retry(
            doi, report_json_path, input_dir, output_dir, agent_id=1, ev=ev,
            review_plan=review_plan,
        )
        _store_stage(state_path, state, "reviewer1", result1)
    else:
        log.info("DOI=%s review resume: using cached first-pass review=%s", doi, result1.get("result"))

    if _is_review_error(result1):
        _store_stage(state_path, state, "decision", {"flow": "reviewer1_failed", "reason": result1.get("_review_error")})
        _store_stage(state_path, state, "final", result1)
        return result1

    validated1 = _validate_review_result(doi, report_json_path, input_dir, result1, evidence)
    _store_stage(state_path, state, "reviewer1_validated", validated1)

    # Strong deterministic source-data evidence does not need a second full
    # paper investigation. If the first pass is substantively high-risk, use a
    # short, evidence-only coverage repair to fill the publication contract;
    # this avoids the former reviewer1 -> reviewer2 -> judge chain that could
    # consume the entire 90-minute wall clock. A repair failure remains an
    # explicit process error and is never published as a risk conclusion.
    if (
        validated1.get("result") == "高风险"
        and not validated1.get("_coverage_error")
        and _has_strong_deterministic_evidence(evidence)
    ):
        gaps = list(validated1.get("_coverage_gaps") or [])
        if gaps:
            log.info(
                "DOI=%s strong deterministic evidence: bounded coverage repair instead of second full review (%d gaps)",
                doi, len(gaps),
            )
            repaired = await _run_judge(
                doi, report_json_path, input_dir, output_dir,
                validated1, validated1, ev=ev, coverage_gaps=gaps,
            )
            _store_stage(state_path, state, "coverage_repair", repaired)
            repaired = _validate_review_result(
                doi, report_json_path, input_dir, repaired, evidence,
            )
            _store_stage(state_path, state, "coverage_repair_validated", repaired)
            if (
                not _is_review_error(repaired)
                and not repaired.get("_coverage_error")
                and not repaired.get("_coverage_gaps")
                and repaired.get("result") == "高风险"
            ):
                final = _clean_internal_references(dict(repaired))
                _store_stage(state_path, state, "decision", {
                    "flow": "strong_deterministic_coverage_repair",
                    "reason": "首轮已确认强确定性证据，仅执行定向覆盖修复。",
                })
                _store_stage(state_path, state, "final", final)
                log.info("DOI=%s final verdict=%s via bounded deterministic repair", doi, final.get("result"))
                return final
            # Keep the substantive checkpoint for the task recovery path; do
            # not start another expensive full investigation in this call.
            _store_stage(state_path, state, "coverage_repair_source", validated1)
            failed = _fallback_result(
                doi,
                "强确定性证据已保存，但定向覆盖修复未完成；请从检查点继续",
            )
            failed["_review_candidate"] = validated1
            _store_stage(state_path, state, "decision", {
                "flow": "strong_deterministic_coverage_repair_error",
                "reason": failed["_review_error"],
            })
            _store_stage(state_path, state, "final", failed)
            return failed
        final = _clean_internal_references(dict(validated1))
        _store_stage(state_path, state, "decision", {
            "flow": "strong_deterministic_single",
            "reason": "首轮已确认强确定性证据且覆盖校验通过。",
        })
        _store_stage(state_path, state, "final", final)
        return final

    accept, reason = _accept_single_review(doi, result1, validated1, evidence)
    if accept and not saved_later_review:
        final = _clean_internal_references(dict(validated1))
        _store_stage(state_path, state, "decision", {"flow": "single", "reason": reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via first-pass review (%s)", doi, final.get("result"), reason)
        return final

    log.info("DOI=%s escalating review after first pass: %s", doi, reason)

    result2 = _get_stage(state, "reviewer2") if resume_results else None
    if not _usable_cached_review_result(result2):
        if result1.get("result") == "低风险" and result1.get("trigger") != "review_error":
            result2 = await _run_lowrisk_verifier_with_retry(
                doi, report_json_path, input_dir, output_dir, validated1, ev=ev,
            )
        else:
            result2 = await _run_with_retry(
                doi, report_json_path, input_dir, output_dir, agent_id=2, ev=ev,
                review_plan=review_plan,
            )
        _store_stage(state_path, state, "reviewer2", result2)
    else:
        log.info("DOI=%s review resume: using cached second-pass review=%s", doi, result2.get("result"))

    if _is_review_error(result2):
        _store_stage(state_path, state, "decision", {"flow": "reviewer2_failed", "reason": result2.get("_review_error")})
        _store_stage(state_path, state, "final", result2)
        return result2

    validated2 = _validate_review_result(doi, report_json_path, input_dir, result2, evidence)
    _store_stage(state_path, state, "reviewer2_validated", validated2)

    if not saved_synthesis and _accept_lowrisk_consensus(doi, result1, validated1, result2, validated2):
        final = _consensus_fallback(doi, validated1, validated2)
        # _accept_lowrisk_consensus() guarantees that both passes have no
        # semantic coverage gaps. Keep this branch free of any warning-only
        # escape hatch so an omitted HIGH finding cannot be published.
        decision_reason = "两轮独立复核均为低风险，且覆盖校验未发现需升级的问题。"
        final = _clean_internal_references(dict(final))
        _store_stage(state_path, state, "decision", {"flow": "two_review_low_consensus", "reason": decision_reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via low-risk verification consensus", doi, final.get("result"))
        return final

    if (
        not saved_synthesis
        and result1.get("trigger") != "review_error"
        and result2.get("trigger") != "review_error"
        and validated1.get("result") == "高风险"
        and validated2.get("result") == "高风险"
        and not validated1.get("_coverage_error")
        and not validated2.get("_coverage_error")
        and not validated1.get("_coverage_gaps")
        and not validated2.get("_coverage_gaps")
    ):
        final = _consensus_fallback(doi, validated1, validated2)
        decision_reason = "两轮独立复核均确认高风险，采用高风险共识结论。"
        _store_stage(state_path, state, "decision", {"flow": "two_review_high_consensus", "reason": decision_reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via high-risk consensus", doi, final.get("result"))
        return final

    judge_result = None
    if resume_results:
        for saved_key in ("synthesis_candidate", "coverage_repair", "coverage_repair_validated", "coverage_repair_source", "judge"):
            saved_candidate = _get_stage(state, saved_key)
            if _usable_cached_review_result(saved_candidate):
                judge_result = saved_candidate
                break
    if not _usable_cached_review_result(judge_result):
        judge_result = await _run_judge(
            doi, report_json_path, input_dir, output_dir, validated1, validated2, ev=ev
        )
        _store_stage(state_path, state, "judge", judge_result)
    else:
        log.info("DOI=%s review resume: using cached synthesis review=%s", doi, judge_result.get("result"))

    if _is_review_error(judge_result):
        repaired = _single_substantive_after_process_failure(doi, validated1, validated2, judge_result)
        if repaired:
            _store_stage(state_path, state, "decision", {
                "flow": "synthesis_error_repaired",
                "reason": repaired.get("_review_warning"),
            })
            _store_stage(state_path, state, "final", repaired)
            log.info("DOI=%s final verdict=%s via process-failure repair", doi, repaired.get("result"))
            return repaired

    if _is_review_error(judge_result):
        _store_stage(state_path, state, "decision", {"flow": "synthesis_failed", "reason": judge_result.get("_review_error")})
        _store_stage(state_path, state, "final", judge_result)
        return judge_result

    final = _validate_review_result(doi, report_json_path, input_dir, judge_result, evidence)
    if final.get("_coverage_gaps") or final.get("_coverage_error"):
        log.warning(
            "DOI=%s synthesis still has coverage gaps; running one coverage-repair synthesis: %s",
            doi,
            final.get("_coverage_gaps") or final.get("_coverage_error"),
        )
        repaired_judge = await _run_judge(
            doi, report_json_path, input_dir, output_dir, final, validated2,
            ev=ev,
            coverage_gaps=list(final.get("_coverage_gaps") or [final.get("_coverage_error")]),
        )
        _store_stage(state_path, state, "coverage_repair", repaired_judge)
        repaired = _validate_review_result(
            doi, report_json_path, input_dir, repaired_judge, evidence
        )
        _store_stage(state_path, state, "coverage_repair_validated", repaired)
        if not repaired.get("_coverage_gaps") and not repaired.get("_coverage_error"):
            final = repaired
        elif (
            validated1.get("result") == "低风险"
            and validated2.get("result") == "低风险"
            and not validated1.get("_coverage_error")
            and not validated2.get("_coverage_error")
            and not validated1.get("_coverage_gaps")
            and not validated2.get("_coverage_gaps")
        ):
            # The two substantive passes agree on low risk and both passed the
            # deterministic coverage gate, but the synthesis model omitted
            # dimension_verdicts. Preserve the independently validated
            # consensus and fill only those formatting labels.
            final = _consensus_fallback(doi, validated1, validated2)
            final.setdefault("dimension_verdicts", validated1.get("dimension_verdicts") or {
                "image": "建议低风险",
                "data": "建议低风险",
                "ref": "建议低风险",
                "methodology": "建议低风险",
            })
            final["_review_warning"] = (
                "综合复核文本缺少维度标签；两轮独立复核均为低风险，已采用经验证的低风险共识。"
            )
        else:
            gaps = repaired.get("_coverage_gaps") or [repaired.get("_coverage_error")]
            final = _fallback_result(
                doi,
                "综合复核仍未覆盖全部维度或高风险线索：" + "；".join(str(x) for x in gaps if x),
            )
    final = _clean_internal_references(dict(final))
    failed = _is_review_error(final)
    decision_reason = final.get("reason") if failed else "两轮复核未形成可直接接受的一致结论，采用综合判定结果。"
    _store_stage(state_path, state, "decision", {"flow": "synthesis_failed" if failed else "synthesis", "reason": decision_reason})
    _store_stage(state_path, state, "final", final)

    log.info("DOI=%s review outcome: %s", doi, "review_error" if failed else final.get("result"))
    return final


async def run_review_single(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    resume: bool = False,
) -> dict:
    """Run review and expose the exact evidence artifact to PDF generation.

    The review stages already build a deterministic evidence bundle.  Returning
    its path prevents the report renderer from rebuilding large workbooks a
    second time, which was the source of recurring coverage timeouts.
    """
    if os.environ.get("REVIEW_ENGINE", "v2").strip().lower() == "v3":
        # Shared engine for API and batch review; see review_v3/DESIGN.md.
        from review_v3.engine import run_review
        return await run_review(doi, report_json_path, input_dir, output_dir, resume=resume)
    from api.services.review_lock import exclusive_review
    artifacts_dir = _review_artifacts_dir(output_dir)
    lock_path = _review_state_path(doi, report_json_path, input_dir, artifacts_dir).with_suffix(".lock")
    token = _REVIEW_CLI_CONTEXT.set(None)
    budget_token = paper_deadline.set(None)
    started = time.monotonic()
    try:
        with exclusive_review(lock_path):
            # Whole-paper wall clock, independent of the number of CLI stages.
            # Keep a paper from monopolising an API worker. The deadline is a
            # safety boundary; checkpointed recovery can continue later and
            # never turns a timeout into a paper-level risk conclusion.
            # Use the public single-paper default from api.config (90 minutes
            # at this deployment).  REVIEW_PAPER_TIMEOUT_SECONDS remains an
            # explicit override, including ``0``/``none``/``unlimited``.
            configured = os.environ.get(
                "REVIEW_PAPER_TIMEOUT_SECONDS", str(REVIEW_TIMEOUT_SECONDS)
            ).strip().lower()
            deadline = None if configured in {"0", "none", "unlimited"} else max(1, int(configured))
            paper_deadline.set(time.monotonic() + deadline if deadline else None)
            try:
                result = await asyncio.wait_for(_run_review_single_impl(
                    doi=doi, report_json_path=report_json_path, input_dir=input_dir,
                    output_dir=output_dir, resume=resume,
                ), timeout=deadline)
            except TimeoutError:
                result = _fallback_result(doi, f"Whole-paper deadline exceeded ({deadline}s); checkpoints retained")
                result["_retry_exhausted"] = True
                state_path = _review_state_path(doi, report_json_path, input_dir, artifacts_dir)
                state = _load_review_state(state_path, doi, report_json_path, input_dir)
                _store_stage(state_path, state, "final", result)
    finally:
        _REVIEW_CLI_CONTEXT.reset(token)
        paper_deadline.reset(budget_token)
        log.info('DOI=%s review wall_seconds=%.1f', doi, time.monotonic() - started)
    if not isinstance(result, dict):
        return result
    result.setdefault("_input_dir", input_dir)
    result.setdefault("_report_json", report_json_path)
    try:
        artifacts_dir = _review_artifacts_dir(output_dir)
        state_path = _review_state_path(doi, report_json_path, input_dir, artifacts_dir)
        state = _load_review_state(state_path, doi, report_json_path, input_dir)
        evidence_path = _get_stage(state, "evidence_path")
        if evidence_path and Path(evidence_path).is_file():
            result.setdefault("_evidence_path", evidence_path)
    except Exception as exc:
        # Missing state metadata must not discard a completed review; the
        # renderer can still fall back to its normal evidence lookup path.
        log.warning("DOI=%s could not attach evidence path to report result: %s", doi, exc)
    return result


async def _run_with_retry(
    doi: str, report_json_path: str, input_dir: str, output_dir: str, agent_id: int,
    ev: EvidencePaths | None = None,
    review_plan: dict | None = None,
) -> dict:
    """Run a single review with retry on timeout/error."""
    for attempt in range(1, MAX_RETRIES + 1):
        result = await _run_single_claude(
            doi, report_json_path, input_dir, output_dir, agent_id, ev=ev,
            review_plan=review_plan,
        )
        if result.get("trigger") != "review_error" or result.get('_retry_exhausted'):
            return result
        log.warning("Review pass %d attempt %d failed for DOI=%s, retrying...", agent_id, attempt, doi)

    log.error("Review pass %d exhausted retries for DOI=%s", agent_id, doi)
    return _fallback_result(doi, f"Review pass {agent_id} exhausted retries")


async def _run_deterministic_agent_review(
    doi: str,
    report_json_path: str,
    input_dir: str,
    verified_evidence: dict,
) -> dict:
    """Ask an LLM to judge a bounded, pre-verified deterministic evidence sample.

    No tools are exposed for this pass; the agent reviews the fixed sample
    instead of spending turns repeatedly reading a large workbook.
    """
    prompt = _build_deterministic_agent_prompt(
        doi, report_json_path, input_dir, verified_evidence
    )
    result = await _run_claude_prompt(
        doi,
        "Deterministic sampled review",
        prompt,
        max_turns=REVIEW_MAX_TURNS,
        allowed_tools=("Bash", "Read"),
    )
    if _usable_cached_review_result(result):
        result["_fastpath_version"] = DETERMINISTIC_FASTPATH_VERSION
        result["_deterministic_sampled_sheets"] = verified_evidence.get("checked_sheets", [])
        result.setdefault("dimension_verdicts", {
            "image": "建议低风险",
            "data": "建议高风险" if result.get("result") == "高风险" else "建议低风险",
            "ref": "建议低风险",
            "methodology": "建议低风险",
        })
    return result


async def _run_lowrisk_verifier_with_retry(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    prior_result: dict,
    ev: EvidencePaths | None = None,
) -> dict:
    """Run the second-pass low-risk verifier with retry on timeout/error."""
    for attempt in range(1, MAX_RETRIES + 1):
        result = await _run_lowrisk_verifier_claude(
            doi, report_json_path, input_dir, output_dir, prior_result, ev=ev,
        )
        if result.get("trigger") != "review_error" or result.get('_retry_exhausted'):
            return result
        log.warning("Low-risk verifier attempt %d failed for DOI=%s, retrying...", attempt, doi)

    log.error("Low-risk verifier exhausted retries for DOI=%s", doi)
    return _fallback_result(doi, "Low-risk verifier exhausted retries")


async def _run_judge(
    doi: str, report_json_path: str, input_dir: str, output_dir: str,
    result1: dict, result2: dict, ev: EvidencePaths | None = None,
    coverage_gaps: list[str] | None = None,
) -> dict:
    """Adjudicate through bounded recovery; validation remains mandatory."""
    prompt = _build_judge_prompt(
        doi, report_json_path, input_dir, result1, result2,
        ev=ev, coverage_gaps=coverage_gaps,
    )
    from api.services.review_cli import schema_only_gaps
    formatting_only = schema_only_gaps(coverage_gaps or [])
    result = await _run_claude_prompt(
        doi, "Coverage repair" if coverage_gaps else "Synthesis review", prompt,
        COVERAGE_REPAIR_MAX_TURNS if coverage_gaps else JUDGE_MAX_TURNS,
        evidence_path=ev.llm_summary if ev else "",
        allowed_tools=() if formatting_only else ('Bash', 'Read'),
    )
    return _clean_internal_references(result)


def _consensus_fallback(doi: str, result1: dict, result2: dict) -> dict:
    """If synthesis fails, fall back to review consensus (both agree) or high risk."""
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
    forbidden = re.compile(
        r"(专家\s*[AB]|Agent\s*\d+|agent\s*\d+|Reviewer\s*\d+|reviewer\s*\d+|"
        r"Reviewer|reviewer|Judge|judge|投票|vote|终审)"
    )
    for key in ["image_review", "data_review", "ref_review", "methodology_review", "verdict", "reason"]:
        val = result.get(key, "")
        # Models occasionally return a structured value for one of the prose
        # fields.  Preserve that value for schema validation instead of
        # passing it to the regular-expression scrubber as if it were text.
        if not isinstance(val, str):
            continue
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
    try:
        # The decoder understands braces inside quoted scientific prose;
        # counting braces manually can silently drop nested dimension labels.
        value, _ = json.JSONDecoder().raw_decode(text[brace_start:])
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    json_end = text.rfind("}") + 1
    if json_end > brace_start:
        try:
            return json.loads(text[brace_start:json_end])
        except json.JSONDecodeError:
            repaired = _extract_review_fields_tolerant(text[brace_start:json_end])
            if repaired:
                return repaired
    return None


def _compact_review_result_for_prompt(result: dict | None) -> dict:
    result = result or {}
    return {
        "doi": result.get("doi", ""),
        "result": result.get("result", ""),
        "trigger": _short_text(result.get("trigger", ""), 500),
        "image_review": _short_text(result.get("image_review", ""), MAX_PROMPT_FIELD_CHARS),
        "data_review": _short_text(result.get("data_review", ""), MAX_PROMPT_FIELD_CHARS),
        "ref_review": _short_text(result.get("ref_review", ""), 2000),
        "methodology_review": _short_text(result.get("methodology_review", ""), 2000),
        "dimension_verdicts": result.get("dimension_verdicts") or {},
        "verdict": result.get("verdict", ""),
        "reason": _short_text(result.get("reason", ""), MAX_PROMPT_FIELD_CHARS),
        "evidence_images": result.get("evidence_images") or [],
        "excel_evidence_regions": result.get("excel_evidence_regions") or [],
    }


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
    ev: EvidencePaths | None = None,
    review_plan: dict | None = None,
) -> dict:
    """Run one Claude CLI review subprocess."""
    prompt = _build_prompt(doi, report_json_path, input_dir, ev=ev, review_plan=review_plan)
    context = _REVIEW_CLI_CONTEXT.get()
    if context and context.get("resume"):
        prior = _load_json_file(context["scope"]).get("stages", {}).get(f"reviewer{agent_id}", {})
        notes = prior.get("_review_error") if _is_review_error(prior) else None
        if notes:
            prompt += (
                "\n## 上次未完成核查的交接记录\n"
                "下面是失败阶段保存的过程记录，不是正式风险结论。先定位记录中的已有核验产物，"
                "确认它们对应本篇论文，再针对未解决项补查；不要无故重做已有可验证依据的核查。"
                "不能把上一阶段的风险占位当作证据，也不能把缺少材料写成已完成检查。\n"
                + str(notes)
            )
    return await _run_claude_prompt(
        doi, f"Review pass {agent_id}", prompt,
        max_turns=(review_plan or {}).get("max_turns", REVIEW_MAX_TURNS),
        evidence_path=(ev.llm_summary if ev else ""),
        allowed_tools=(review_plan or {}).get("allowed_tools", ("Bash", "Read")),
    )


async def _run_lowrisk_verifier_claude(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
    prior_result: dict,
    ev: EvidencePaths | None = None,
) -> dict:
    """Run the focused second-pass verifier for a first-pass low-risk result."""
    prompt = _build_lowrisk_verifier_prompt(
        doi, report_json_path, input_dir, prior_result, ev=ev,
    )
    return await _run_claude_prompt(
        doi, "Low-risk verifier", prompt, max_turns=LOWRISK_VERIFIER_MAX_TURNS,
        evidence_path=(ev.llm_summary if ev else ""),
    )


async def _run_claude_prompt(
    doi: str, review_label: str, prompt: str, max_turns: int,
    evidence_path: str = "",
    allowed_tools: tuple[str, ...] | list[str] | None = ("Bash", "Read"),
) -> dict:
    """Use the same finite recovery/finalization transport as batch reviews."""
    from api.services.review_cli import run
    checkpoint = None
    state = None
    context = _REVIEW_CLI_CONTEXT.get()
    if context is not None:
        # Evidence paths are paper-specific and place the checkpoint beside
        # the immutable artifacts.  The prompt hash also separates reviewer,
        # verifier, synthesis, and coverage-repair sessions for the same DOI.
        from api.services.review_cli import prompt_key
        root = Path(evidence_path).parent if evidence_path else REVIEW_STATE_DIR
        root.mkdir(parents=True, exist_ok=True)
        safe_doi = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalize_doi(doi) or doi).strip("._-") or "paper"
        identity = "|".join([context["scope"], context["run_id"], review_label, prompt])
        checkpoint = root / f"cli_checkpoint_{safe_doi}_{prompt_key(identity)}.json"
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("doi") != doi:
                state = None
        except (OSError, ValueError, TypeError):
            state = None

        def persist(value: dict) -> None:
            temp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(checkpoint)

    result = await run(
        sys.modules[__name__], doi, review_label, prompt, max_turns, allowed_tools,
        state=state, persist=persist if checkpoint else None,
    )
    return result


def _fallback_result(doi: str, error_msg: str) -> dict:
    if error_msg:
        log.error("Review fallback for DOI=%s: %s", doi, error_msg)
    return {
        "doi": doi,
        "result": "高风险",
        "risk_conclusion": None,
        "review_status": "review_error",
        "trigger": "review_error",
        "image_review": "复审未完成，不能据此形成风险结论。",
        "data_review": "复审未完成，不能据此形成风险结论。",
        "ref_review": "复审未完成，不能据此形成风险结论。",
        "verdict": "建议高风险",
        "reason": f"复审未完成：{error_msg or '未知流程错误'}。流程错误不作为论文风险结论。",
        "_review_error": error_msg or "未知流程错误",
    }


def _find_claude_cli() -> str:
    import shutil
    candidates = []
    env_path = os.environ.get("CLAUDE_CLI")
    if env_path:
        candidates.append(env_path)
        p = Path(env_path)
        if p.is_file() and os.access(p, os.X_OK):
            return env_path
    candidates.extend([
        "/home/library/.vscode-server/extensions/anthropic.claude-code-2.1.186-linux-arm64/resources/native-binary/claude",
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ])
    for root in (
        Path("/home/library/.vscode-server/extensions"),
        Path("/root/.vscode-server/extensions"),
    ):
        try:
            candidates.extend(
                str(p) for p in sorted(
                    root.glob("anthropic.claude-code-*/resources/native-binary/claude"),
                    reverse=True,
                )
            )
        except PermissionError:
            log.debug("Skipping unreadable Claude extension dir: %s", root)
    for candidate in candidates:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return candidate
    found = shutil.which("claude")
    if found and Path(found).is_file() and os.access(found, os.X_OK):
        return found
    raise FileNotFoundError("claude CLI not found in PATH")
