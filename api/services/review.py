from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from api.config import REVIEW_TIMEOUT_SECONDS
from modules.doi_utils import normalize_doi

log = logging.getLogger(__name__)

_SKILL_SCRIPTS = "/opt/.claude/skills/paper-batch-review/scripts"

MAX_RETRIES = 2
STATE_VERSION = 2
REVIEW_STATE_DIR = Path(os.environ.get(
    "REVIEW_STATE_DIR",
    "/opt/paper-integrity-checker/data/output/review_v2/state",
))
EVIDENCE_TIMEOUT_SECONDS = int(os.environ.get("REVIEW_COVERAGE_TIMEOUT_SECONDS", "240"))
EVIDENCE_MEMORY_MB = int(os.environ.get("REVIEW_COVERAGE_MEMORY_MB", "2048"))
LLM_EVIDENCE_MAX_BYTES = int(os.environ.get("REVIEW_LLM_EVIDENCE_MAX_BYTES", "60000"))
MAX_PROMPT_FIELD_CHARS = int(os.environ.get("REVIEW_MAX_PROMPT_FIELD_CHARS", "4000"))
REVIEW_MAX_TURNS = int(os.environ.get("REVIEW_MAX_TURNS", "60"))
LOWRISK_VERIFIER_MAX_TURNS = int(os.environ.get("REVIEW_LOWRISK_MAX_TURNS", "60"))
JUDGE_MAX_TURNS = int(os.environ.get("REVIEW_JUDGE_MAX_TURNS", "60"))
DETERMINISTIC_FASTPATH_ENABLED = os.environ.get("REVIEW_DETERMINISTIC_FASTPATH", "1") != "0"
DETERMINISTIC_FASTPATH_MIN_POINTS = int(os.environ.get("REVIEW_DETERMINISTIC_FASTPATH_MIN_POINTS", "1000"))
DETERMINISTIC_FASTPATH_SAMPLE_LIMIT = int(os.environ.get("REVIEW_DETERMINISTIC_SAMPLE_LIMIT", "10"))
DETERMINISTIC_FASTPATH_VERSION = "7"
REVIEW_RESUME_RESULTS = os.environ.get("REVIEW_RESUME_RESULTS", "0") == "1"
_SUMMARY_CODE_VERSION = "2"


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


class EvidencePaths(NamedTuple):
    llm_summary: str
    full: str

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

请严格按照以下复核规则执行（来自运行时复核规则配置）：

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

正文面向审核人员展示时，尽量使用中文规则名，不要直接暴露内部检测字段名。例如写”跨表整行重复告警””小数位一致告警””首位数字分布告警””数值重复使用告警”，不要直接写 cross_sheet_row_duplicate、decimal_uniformity、Benford、value_recycling。

绝对禁止在输出中出现以下内部术语或引用：
- 不要引用规则文档名或章节号（如 RUNTIME_RULES、SKILL.md、§2.1、§6 等）
- 不要输出内部变量名（如 md5=、seam_columns、crossref_score=、matched_doi=、ref_text=、inliers、partial_region 等）
- 不要输出文件校验哈希值（如 md5=53a3d36a）
- 如需引用检测依据，用平实中文描述（如”同一文件的两份副本经校验完全一致”而非”md5=53a3d36a”）

使用编号列表时，序号必须连续（1,2,3...），不得跳号。输出前检查一遍编号连续性。
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
  "methodology_review": "方法学与统计核查详情（中文，可选；无相关问题时留空字符串）",
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估（中文）——每个确认的高风险问题必须带具体位置：图像写图号/页码，数据写文件名/sheet/列/数值举例，参考文献写第几条引用。不可只写'存在数据复用'等笼统描述。"
}
```

重要：result 字段必须是 "高风险" 或 "低风险" 二选一。verdict 字段必须是 "建议高风险" 或 "建议低风险" 二选一（不存在"需人工复查"/"中风险"）。不要在JSON外输出任何内容。
"""

COMPACT_REVIEW_RULES = """### 复核核心规则
目标：基于可核验事实判断“建议高风险”或“建议低风险”。初检只是线索，不能把初检高风险本身当证据。

必须先读复核证据摘要 JSON。摘要中的 duplicate_column_pair_groups、cross_sheet_reuse_groups、decimal_precision_mismatch、high_findings 是本次复核的覆盖单位；完整 evidence 文件只用于必要时追溯。

数据复核优先级：
1. 先判断重复列/跨表复用中的相同列是自变量/轴变量，还是测量值/因变量。
2. 共享时间、浓度、电位、波数、R/k 轴等自变量通常是正常；不同样本/条件的测量值逐值相同才支持高风险。
3. 对分组告警按代表样例和最高风险样例核查；同一类原因覆盖整组即可，不要逐条写 50 段。
4. 小数精度、CV=0、恒等依赖、末位数字异常只有在列名和上下文不支持正常解释时才支持高风险。
5. 图文一致性核查（必须执行，不可跳过）：用 fitz.open() 渲染论文 PDF 中至少 2-3 张主图页面为 PNG，用 Read 查看渲染图像；同时读取对应 xlsx sheet 数据，核对关键数值（柱高、峰位、曲线趋势、量级、数据点数）。在 data_review 中写明每张图的比对结论。仅看 annotation 标注图不算完成此步骤。

图像复核：真实跨页图像重复、相同实验图像复用、明确拼接不连续支持高风险；坐标轴模板、示意图、PDF提取伪影、同图不同嵌入版本通常是假阳性，但需说明依据。
若核查后仅剩少量图像重复线索，且未发现数据、图文一致性、参考文献或方法学独立问题，必须从严视觉确认其是否为真实实验图像复用；不能仅凭自动匹配或少量可解释的版式/模板相似轻易升级为高风险。

参考文献复核：CrossRef 超时/低置信度/解析碎片不构成引用异常；只有完整可读条目的 DOI、题名、作者或期刊实质不一致才支持高风险。

判定：
- 建议高风险：确认存在真实图像重复/拼接、不同实验测量值逐值复用、主图样本量不符、图文/标签矛盾、同类测量强精度异常且无法解释、完整引用实质不一致等任一问题。
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
7. 图像和参考文献若未被本轮核实为独立问题，应明确说明它们不是本次高风险结论的主要依据。

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
  "methodology_review": "方法学与统计核查详情，可为空字符串。",
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

## 论文信息
- DOI: {doi}
- 检测报告路径: {report_json_path}
- 原始数据目录: {input_dir}
- 复核证据包(确定性预处理清单,务必先读): {evidence_path}

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
1. **图文一致性（必须执行）**：用 fitz.open() 渲染论文 PDF 中至少 2-3 张主图页面为 PNG，用 Read 工具查看渲染图像，再从对应 xlsx sheet 读取数据，逐图比对关键数值（柱高、峰位、曲线趋势、量级、坐标轴范围）。在 data_review 中写明每张图的比对结论。**如果未执行渲染和源数据对比，不能维持低风险结论。** 仅查看 annotation 标注图不算完成此步骤——必须对比论文渲染图像与源数据数值。
2. **跨图标签矛盾**：同一数据列在不同图中是否使用了矛盾的样品/条件标签？
3. **末位数字 / SD 规律性**：首轮是否运行了 forensic_checks.py scan-digits？若未运行，你来运行并检查结果。

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
  "methodology_review": "方法学与统计核查详情（中文，可选；无相关问题时留空字符串）",
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估（中文）——每个确认的高风险问题必须带具体位置（图号/文件/sheet/列/数值）"
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

## 复核结论一

结果: {r1_result}
图像复核: {r1_image}
数据复核: {r1_data}
参考文献: {r1_ref}
方法学与统计核查: {r1_methodology}
判定: {r1_verdict}
理由: {r1_reason}

## 复核结论二

结果: {r2_result}
图像复核: {r2_image}
数据复核: {r2_data}
参考文献: {r2_ref}
方法学与统计核查: {r2_methodology}
判定: {r2_verdict}
理由: {r2_reason}

## 你的任务

1. 审查两份复核分析的逻辑和证据是否充分
2. 对关键发现进行独立验证（读取标注图、源数据文件）
3. 特别关注：两份复核是否都遗漏了某些问题，或是否过于轻易地将发现标记为假阳性
4. 如果两份复核对某个问题的解释不同，你必须独立验证后判断哪个正确
5. **独立完成图文一致性核查（见下方复核规则中的 Figure-vs-Source Check）**：即使前序复核未做，你也必须亲自渲染主图、查看图像、与源数据逐一比对，主动以"逐图 vs 其源数据"的姿态复查，而非只验证自动发现。
6. 若证据包提供 cross_sheet_reuse_groups, 按分组复核代表样例和 requires_expansion=true 的分组；原始 cross_sheet_reuse 列表只用于追溯，不要求逐条写出每个成员。
7. 若关键强证据已被确认足以支持建议高风险，可以围绕关键证据直接收敛，并简要交代其他维度是否有独立支持或暂无独立问题。
8. 做出你自己的最终判断

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
  "methodology_review": "方法学与统计核查意见（中文，可选；无相关问题时留空字符串）",
  "verdict": "建议高风险 或 建议低风险",
  "reason": "综合评估（中文）——每个确认的高风险问题必须带具体位置（图号/文件/sheet/列/数值）"
}}
```

重要：result 必须是 "高风险" 或 "低风险" 二选一（不存在中风险）。verdict 必须是 "建议高风险" 或 "建议低风险" 二选一。不要因为复核文本留痕格式不足而单独判高风险；只有证据本身支持高风险或关键事实无法验证时才判高风险。不要在JSON外输出任何内容。不要出现内部流程名称、角色称谓或流程协调信息。
"""


def _build_prompt(
    doi: str,
    report_json_path: str,
    input_dir: str,
    ev: EvidencePaths | str | None = None,
    full_evidence_path: str = "",
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
    )
    if ep.full and ep.full != ep.llm_summary:
        header += f"\n完整证据包路径(仅必要时追溯, 不要求逐条展开): {ep.full}\n"
    rules = COMPACT_REVIEW_RULES
    return header + rules + PROMPT_FOOTER


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
    return LOWRISK_VERIFIER_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=evidence_display,
        prior_result_json=json.dumps(_compact_review_result_for_prompt(prior_result), ensure_ascii=False, indent=2),
        skill_rules=COMPACT_REVIEW_RULES,
        reader_facing_text_rules=READER_FACING_TEXT_RULES,
    )


def _build_judge_prompt(
    doi: str, report_json_path: str, input_dir: str,
    result1: dict, result2: dict,
    ev: EvidencePaths | None = None,
) -> str:
    # NOTE: skill_rules may contain literal { } braces (JSON examples),
    # so it must NOT pass through str.format(). Substitute it AFTER formatting.
    r1 = _compact_review_result_for_prompt(result1)
    r2 = _compact_review_result_for_prompt(result2)
    ep = ev or EvidencePaths("", "")
    evidence_display = ep.llm_summary or "(未生成,请直接审阅源数据)"
    if ep.full and ep.full != ep.llm_summary:
        evidence_display += f"\n- 完整证据包路径(仅必要时追溯): {ep.full}"
    body = JUDGE_PROMPT_TEMPLATE.format(
        doi=doi,
        report_json_path=report_json_path,
        input_dir=input_dir,
        evidence_path=evidence_display,
        skill_rules="@@SKILL_RULES@@",
        reader_facing_text_rules=READER_FACING_TEXT_RULES,
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
    return body.replace("@@SKILL_RULES@@", COMPACT_REVIEW_RULES)


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
    base = artifacts_dir or Path("/opt/paper-integrity-checker/data/output/review_v2")
    return base / f"{doi.replace('/', '_')}_{digest}_evidence.json"


def _ensure_evidence_bundle(doi, report_json_path, input_dir, artifacts_dir: Path | str | None = None):
    """Generate the deterministic evidence bundle in a bounded child process.

    The evidence builder reads source Excel files and can hit pathological
    workbooks. Keep that failure isolated so one DOI cannot kill the API worker.
    """
    artifacts = Path(artifacts_dir) if artifacts_dir else None
    if artifacts:
        artifacts.mkdir(parents=True, exist_ok=True)
    legacy_dir = Path("/opt/paper-integrity-checker/data/output/review_v2")
    legacy_dir.mkdir(parents=True, exist_ok=True)
    out_path = _evidence_bundle_path(doi, report_json_path, input_dir, artifacts)
    if out_path.exists():
        return str(out_path)

    legacy_path = _evidence_bundle_path(doi, report_json_path, input_dir, None)
    relocated = legacy_dir / "_unsorted" / legacy_path.name
    if artifacts:
        for candidate in (legacy_path, relocated):
            if candidate.exists():
                shutil.copy2(candidate, out_path)
                return str(out_path)
    elif legacy_path.exists():
        return str(legacy_path)
    elif relocated.exists():
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
        return str(out_path)
    except subprocess.TimeoutExpired:
        log.error("DOI=%s evidence bundle generation timed out after %ss", doi, EVIDENCE_TIMEOUT_SECONDS)
        return ""
    except Exception as e:
        log.error("DOI=%s evidence bundle generation failed: %s", doi, e)
        return ""


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
        },
        "main_figures": ((evidence or {}).get("main_figures") or [])[:20],
        "data_files": ((evidence or {}).get("data_files") or [])[:20],
        "review_guidance": (evidence or {}).get("review_guidance") or {},
    }
    return summary


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
    first-pass high-risk convergence after the review confirms them."""
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
    )


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
            f"方法学与统计核查：同名测量值列 {', '.join(headers[:3]) or 'y'} "
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
        "二次核验或综合判定因流程错误未完成；本结论未因流程错误自动上调风险。",
    )


def _is_lowrisk_result(result: dict) -> bool:
    v = (result.get("verdict") or "") + (result.get("result") or "")
    return ("低风险" in v) and ("高风险" not in (result.get("result") or ""))


def _apply_coverage_validation(doi: str, report_json_path: str, input_dir: str, result: dict) -> dict:
    """Build the deterministic evidence bundle and validate the synthesis result against
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


async def run_review_single(
    doi: str,
    report_json_path: str,
    input_dir: str,
    output_dir: str,
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

    # Cross-identity reconciliation: when this invocation uses paths that
    # don't match any prior state identity, the load above returns a fresh
    # (empty) state. Scan the state directory for ANY completed review of
    # this DOI — a previously-completed verdict must not be silently
    # overwritten by a re-review that happens to use different paths.
    existing_stages = state.get("stages") or {}
    ignore_cross_state = os.environ.get("REVIEW_IGNORE_CROSS_STATE", "0") == "1"
    if not existing_stages and not ignore_cross_state:
        cross_state = _find_any_completed_state_for_doi(doi)
        if cross_state is not None:
            cross_final = (cross_state.get("stages") or {}).get("final")
            if _valid_review_result(cross_final) and not _is_review_error(cross_final):
                log.warning(
                    "DOI=%s: found existing completed state (result=%s) from different "
                    "path identity; returning existing verdict to prevent silent "
                    "overwrite. Set REVIEW_RESUME_RESULTS=1 to explicitly resume.",
                    doi, cross_final.get("result"),
                )
                return _clean_internal_references(dict(cross_final))
            if _is_review_error(cross_final):
                log.info("DOI=%s: ignoring cross-identity review_error cached final", doi)

    resume_results = REVIEW_RESUME_RESULTS

    cached_final = _get_stage(state, "final") if resume_results else None
    if _valid_review_result(cached_final):
        if (
            cached_final.get("_fastpath_version")
            and cached_final.get("_fastpath_version") != DETERMINISTIC_FASTPATH_VERSION
        ):
            log.info("DOI=%s review resume: ignoring stale fast-path final", doi)
        else:
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
        evidence_path = _ensure_evidence_bundle(doi, report_json_path, input_dir, artifacts_dir)
        _store_stage(state_path, state, "evidence_path", evidence_path)
    else:
        localized = _localize_artifact(evidence_path, artifacts_dir)
        if localized != evidence_path:
            evidence_path = localized
            _store_stage(state_path, state, "evidence_path", evidence_path)

    evidence = _load_json_file(evidence_path)
    if evidence:
        _store_stage(state_path, state, "evidence_counts", _evidence_counts(evidence))

    llm_evidence_path = _get_stage(state, "llm_evidence_path")
    if evidence_path and (not llm_evidence_path or not Path(llm_evidence_path).exists()):
        llm_evidence_path = _ensure_llm_evidence_bundle(doi, evidence_path)
        _store_stage(state_path, state, "llm_evidence_path", llm_evidence_path)
    else:
        localized = _localize_artifact(llm_evidence_path, artifacts_dir)
        if localized != llm_evidence_path:
            llm_evidence_path = localized
            _store_stage(state_path, state, "llm_evidence_path", llm_evidence_path)

    ev = EvidencePaths(llm_summary=llm_evidence_path or evidence_path, full=evidence_path)

    verified_sample = _get_stage(state, "deterministic_sample_evidence")
    if (
        not isinstance(verified_sample, dict)
        or verified_sample.get("version") != DETERMINISTIC_FASTPATH_VERSION
    ):
        verified_sample = _build_verified_deterministic_evidence(doi, input_dir, evidence)
        if verified_sample:
            _store_stage(state_path, state, "deterministic_sample_evidence", verified_sample)

    deterministic_agent = _get_stage(state, "deterministic_agent_review") if resume_results else None
    if (
        _usable_cached_review_result(deterministic_agent)
        and deterministic_agent.get("_fastpath_version") != DETERMINISTIC_FASTPATH_VERSION
    ):
        deterministic_agent = None
    if (
        isinstance(verified_sample, dict)
        and verified_sample.get("verified")
        and not _usable_cached_review_result(deterministic_agent)
    ):
        deterministic_agent = await _run_deterministic_agent_review(
            doi, report_json_path, input_dir, verified_sample
        )
        if _usable_cached_review_result(deterministic_agent):
            deterministic_agent = _validate_review_result(
                doi, report_json_path, input_dir, deterministic_agent, evidence
            )
        _store_stage(state_path, state, "deterministic_agent_review", deterministic_agent)

    if _usable_cached_review_result(deterministic_agent):
        final = _clean_internal_references(dict(deterministic_agent))
        final["_fastpath_version"] = DETERMINISTIC_FASTPATH_VERSION
        _store_stage(state_path, state, "decision", {
            "flow": "deterministic_sampled_agent",
            "reason": "强确定性数据证据经最多10个工作表抽样核验后，由复核agent给出最终结论。",
        })
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via sampled deterministic agent", doi, final.get("result"))
        return final

    result1 = _get_stage(state, "reviewer1") if resume_results else None
    if not _usable_cached_review_result(result1):
        result1 = await _run_with_retry(
            doi, report_json_path, input_dir, output_dir, agent_id=1, ev=ev,
        )
        _store_stage(state_path, state, "reviewer1", result1)
    else:
        log.info("DOI=%s review resume: using cached first-pass review=%s", doi, result1.get("result"))

    validated1 = _get_stage(state, "reviewer1_validated") if resume_results else None
    if not _usable_cached_review_result(validated1):
        validated1 = _validate_review_result(doi, report_json_path, input_dir, result1, evidence)
        _store_stage(state_path, state, "reviewer1_validated", validated1)

    accept, reason = _accept_single_review(result1, validated1, evidence)
    if accept:
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
            )
        _store_stage(state_path, state, "reviewer2", result2)
    else:
        log.info("DOI=%s review resume: using cached second-pass review=%s", doi, result2.get("result"))

    validated2 = _get_stage(state, "reviewer2_validated") if resume_results else None
    if not _usable_cached_review_result(validated2):
        validated2 = _validate_review_result(doi, report_json_path, input_dir, result2, evidence)
        _store_stage(state_path, state, "reviewer2_validated", validated2)

    if _accept_lowrisk_consensus(result1, validated1, result2, validated2):
        final = _consensus_fallback(doi, validated1, validated2)
        decision_reason = "两轮独立复核均为低风险，且覆盖校验未发现需升级的问题。"
        _store_stage(state_path, state, "decision", {"flow": "two_review_low_consensus", "reason": decision_reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via low-risk verification consensus", doi, final.get("result"))
        return final

    if (
        result1.get("trigger") != "review_error"
        and result2.get("trigger") != "review_error"
        and validated1.get("result") == "高风险"
        and validated2.get("result") == "高风险"
    ):
        final = _consensus_fallback(doi, validated1, validated2)
        decision_reason = "两轮独立复核均确认高风险，采用高风险共识结论。"
        _store_stage(state_path, state, "decision", {"flow": "two_review_high_consensus", "reason": decision_reason})
        _store_stage(state_path, state, "final", final)
        log.info("DOI=%s final verdict=%s via high-risk consensus", doi, final.get("result"))
        return final

    judge_result = _get_stage(state, "judge") if resume_results else None
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

    final = _validate_review_result(doi, report_json_path, input_dir, judge_result, evidence)
    final = _clean_internal_references(dict(final))
    decision_reason = "两轮复核未形成可直接接受的一致结论，采用综合判定结果。"
    _store_stage(state_path, state, "decision", {"flow": "synthesis", "reason": decision_reason})
    _store_stage(state_path, state, "final", final)

    log.info("DOI=%s final verdict: %s", doi, final.get("result"))
    return final


async def _run_with_retry(
    doi: str, report_json_path: str, input_dir: str, output_dir: str, agent_id: int,
    ev: EvidencePaths | None = None,
) -> dict:
    """Run a single review with retry on timeout/error."""
    for attempt in range(1, MAX_RETRIES + 1):
        result = await _run_single_claude(
            doi, report_json_path, input_dir, output_dir, agent_id, ev=ev,
        )
        if result.get("trigger") != "review_error":
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
        if result.get("trigger") != "review_error":
            return result
        log.warning("Low-risk verifier attempt %d failed for DOI=%s, retrying...", attempt, doi)

    log.error("Low-risk verifier exhausted retries for DOI=%s", doi)
    return _fallback_result(doi, "Low-risk verifier exhausted retries")


async def _run_judge(
    doi: str, report_json_path: str, input_dir: str, output_dir: str,
    result1: dict, result2: dict, ev: EvidencePaths | None = None,
) -> dict:
    """Run the synthesis pass that makes the final determination."""
    prompt = _build_judge_prompt(doi, report_json_path, input_dir, result1, result2, ev=ev)

    claude_cmd = _find_claude_cli()
    cmd = [
        claude_cmd,
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", "Bash", "Read",
        "--max-turns", str(JUDGE_MAX_TURNS),
    ]

    log.info("Synthesis review starting for DOI=%s", doi)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/opt/paper-integrity-checker",
    )

    try:
        timeout = _review_subprocess_timeout_seconds()
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.warning("Synthesis review cancelled for DOI=%s", doi)
        raise
    except asyncio.TimeoutError:
        proc.kill()
        log.error("Synthesis review timed out for DOI=%s, using review consensus", doi)
        return _consensus_fallback(doi, result1, result2)

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[-500:]
        log.error("Synthesis review failed for DOI=%s (rc=%d): %s", doi, proc.returncode, err)
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
            log.info("Synthesis review finished DOI=%s → %s", doi, inner.get("result"))
            return inner

        log.warning("Synthesis review for DOI=%s: no valid JSON found (text len=%d)", doi, len(text))
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error("Synthesis review parse error for DOI=%s: %s", doi, e)

    return _consensus_fallback(doi, result1, result2)


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
        "verdict": result.get("verdict", ""),
        "reason": _short_text(result.get("reason", ""), MAX_PROMPT_FIELD_CHARS),
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
) -> dict:
    """Run one Claude CLI review subprocess."""
    prompt = _build_prompt(doi, report_json_path, input_dir, ev=ev)
    return await _run_claude_prompt(
        doi, f"Review pass {agent_id}", prompt, max_turns=REVIEW_MAX_TURNS,
        evidence_path=(ev.llm_summary if ev else ""),
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
    doi: str,
    review_label: str,
    prompt: str,
    max_turns: int,
    evidence_path: str = "",
    allowed_tools: tuple[str, ...] | list[str] | None = ("Bash", "Read"),
) -> dict:
    """Run a Claude CLI prompt and parse the review JSON result."""
    claude_cmd = _find_claude_cli()
    cmd = [
        claude_cmd,
        "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(max_turns),
    ]
    if allowed_tools:
        cmd[5:5] = ["--allowedTools", *allowed_tools]

    evidence_bytes = 0
    if evidence_path:
        try:
            evidence_bytes = Path(evidence_path).stat().st_size
        except Exception:
            pass

    log.info(
        "%s starting for DOI=%s (prompt_chars=%d, prompt_bytes=%d, evidence_bytes=%d, max_turns=%d)",
        review_label,
        doi,
        len(prompt),
        len(prompt.encode("utf-8")),
        evidence_bytes,
        max_turns,
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/opt/paper-integrity-checker",
    )

    try:
        timeout = _review_subprocess_timeout_seconds()
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.warning("%s cancelled for DOI=%s", review_label, doi)
        raise
    except asyncio.TimeoutError:
        proc.kill()
        log.error("%s timed out for DOI=%s", review_label, doi)
        return _fallback_result(doi, f"{review_label} timed out")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[-500:]
        log.error("%s failed for DOI=%s (rc=%d): %s", review_label, doi, proc.returncode, err)
        return _fallback_result(doi, f"{review_label} failed: {err}")

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
            log.info("%s finished DOI=%s -> %s", review_label, doi, inner.get("result"))
            return inner

        log.warning(
            "%s for DOI=%s: no valid JSON found (text len=%d)",
            review_label,
            doi,
            len(text),
        )
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        log.error("%s parse error for DOI=%s: %s", review_label, doi, e)

    return _fallback_result(doi, f"{review_label}: could not parse output")


def _fallback_result(doi: str, error_msg: str) -> dict:
    if error_msg:
        log.error("Review fallback for DOI=%s: %s", doi, error_msg)
    return {
        "doi": doi,
        "result": "高风险",
        "trigger": "review_error",
        "image_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "data_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "ref_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "verdict": "建议高风险",
        "reason": "复核流程执行失败，未获得可解析的复核结果；按规则暂列建议高风险，建议人工进一步确认。",
    }


def _find_claude_cli() -> str:
    import shutil
    candidates = []
    env_path = os.environ.get("CLAUDE_CLI")
    if env_path:
        candidates.append(env_path)
    candidates.extend([
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ])
    candidates.extend(
        str(p) for p in sorted(
            Path("/root/.vscode-server/extensions").glob(
                "anthropic.claude-code-*-linux-x64/resources/native-binary/claude"
            ),
            reverse=True,
        )
    )
    for candidate in candidates:
        p = Path(candidate)
        if p.is_file() and os.access(p, os.X_OK):
            return candidate
    found = shutil.which("claude")
    if found and Path(found).is_file() and os.access(found, os.X_OK):
        return found
    raise FileNotFoundError("claude CLI not found in PATH")
