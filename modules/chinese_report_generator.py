from __future__ import annotations

import json
import logging
import os
import re
import hashlib
import html as html_lib
import subprocess
import sys
from datetime import date
from pathlib import Path

import fitz

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from modules.doi_utils import normalize_doi
from utils.llm_client import chat

log = logging.getLogger(__name__)

REPORT_PYTHON_VERSION = (3, 12, 3)
REPORT_PYMUPDF_VERSION = "1.27.2.3"
REPORT_FONT_FAMILY = "Noto Sans CJK SC"
REPORT_FONT_HASHES = {
    "Regular": "b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a",
    "Bold": "faa5f3656a78b2e2d450d27fe8382c778bc2b6bb5ea29c986664a6a435056ceb",
}
FONT_REGULAR = f'"{REPORT_FONT_FAMILY}", sans-serif'
FONT_BOLD = f'"{REPORT_FONT_FAMILY}", sans-serif'


def _resolved_font_file(style: str) -> Path | None:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", f"{REPORT_FONT_FAMILY}:style={style}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    path = Path(result.stdout.strip())
    return path if path.is_file() else None


def validate_report_render_environment() -> None:
    """Reject PDF rendering when the pinned runtime or fonts are unavailable."""
    errors = []
    actual_python = sys.version_info[:3]
    if actual_python != REPORT_PYTHON_VERSION:
        errors.append(
            f"Python {'.'.join(map(str, REPORT_PYTHON_VERSION))} required; "
            f"got {'.'.join(map(str, actual_python))} ({sys.executable})"
        )

    actual_pymupdf = getattr(fitz, "__version__", "unknown")
    if actual_pymupdf != REPORT_PYMUPDF_VERSION:
        errors.append(f"PyMuPDF {REPORT_PYMUPDF_VERSION} required; got {actual_pymupdf}")

    for style, expected_hash in REPORT_FONT_HASHES.items():
        font_path = _resolved_font_file(style)
        if font_path is None:
            errors.append(f"font {REPORT_FONT_FAMILY} {style} is unavailable")
            continue
        actual_hash = hashlib.sha256(font_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            errors.append(
                f"font {REPORT_FONT_FAMILY} {style} hash mismatch at {font_path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )

    if errors:
        raise RuntimeError("Report rendering environment mismatch: " + "; ".join(errors))

METHODS_HTML = """
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">附录：检测方法说明</h2>

<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">本报告采用以下自动化检测方法对论文进行分析：</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">1. 图像重复检测</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">本系统通过三个步骤逐层筛查论文中的图像是否存在重复或异常：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>第一步"指纹比对"：</b>将每张图像压缩为一个简短的数字指纹，快速比较所有图像对之间的相似程度，初步筛选出可能重复的图像。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>第二步"局部特征匹配"：</b>对初筛通过的图像，提取图像中的关键特征点（如边缘、纹理等），逐一比对两张图像中是否存在相同的局部区域。即使图像被缩放、旋转或裁剪，该方法仍能识别出重复片段。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>第三步"精确验证"：</b>对第二步标记的可疑区域，在多种缩放比例下进行逐像素比对确认，最终判定是否存在真实的图像重复。</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">2. 数据异常检测</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">本系统对论文附带的源数据文件（Excel/CSV/DOCX/FCS/SAV/PDB/CIF）进行检查；其中表格类数据进行统计异常检验，PDB与CIF结构文件进行晶体或分子结构完整性核查：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>变异系数检验：</b>计算每组数据的离散程度。真实的实验测量数据天然存在波动，若一组重复测量值完全相同（变异系数为0）或极度接近，则高度可疑。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>等差/等比数列检验：</b>检查数据值是否排列成完美的数学数列。真实实验数据不可能呈现完美的等差或等比规律，出现此类模式提示数据可能非自然生成。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>本福特定律检验：</b>自然产生的数值数据中，首位数字"1"出现的频率远高于"9"。人为构造的数据往往不符合这一规律，通过统计检验可以识别出偏离自然分布的数据集。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>跨组重复检验：</b>比较不同实验组之间的数值重叠比例。若两组本应独立的实验数据中有超过50%的数值完全相同，则提示数据可能被复制或重复使用。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>跨列线性依赖检验：</b>对同一数据表中的各组数据列进行两两线性回归分析，检测是否存在近乎完美的线性关系（y = a×x + b, R²≥0.9999）。若两条本应独立采集的实验曲线之间R²接近1.0，尤其当斜率恰好为整数倍（如2、5、10）时，强烈提示其中一条曲线可能是通过对另一条曲线进行数学缩放/平移生成的，而非真实独立测量所得。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>数值合理性检验：</b>对测量数据的末位数字分布、标准差列的取值规律等进行核查。真实测量数据的末位数字通常较为均匀，标准差也极少全为整数或完全相同的精度；若出现明显偏离，提示相关数值可能并非自然测量所得。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>PDB结构文件核查：</b>解析ATOM/HETATM坐标记录，检查重复原子身份、重复坐标、异常occupancy/B-factor、零坐标和格式无法解析等可直接核实的问题。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>CIF晶体结构文件核查：</b>使用晶体学解析器检查文件语法、数据块、晶胞与空间群、原子坐标、占位率、位移参数、异常短接触、化学式量、密度及精修质量指标；检测结果属于风险线索，需结合论文正文和领域专家意见复核。</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">3. 参考文献核验</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">通过DOI（数字对象标识符）向CrossRef国际学术数据库查询每条参考文献的真实性，验证其是否真实存在、标题与作者是否匹配。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>中文及综述论文引文一致性：</b>识别“参考文献”“注释”等中文文献区段，核对正文编号引用与文末条目是否对应，提示重复条目、未被正文引用的条目及异常年份。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>综述方法完整性：</b>对具有综述特征的论文检查数据库、检索范围、检索词及纳入排除标准等方法信息；叙述性综述的适用性仍需人工判断。</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">4. 图像拼接检测</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">针对蛋白质印迹（Western blot）、凝胶电泳等条带类图像，初步筛查是否存在拼接迹象，重点关注以下特征：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>泳道分界：</b>泳道之间是否出现不自然的、贯穿上下的清晰竖直分界线。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>背景一致性：</b>图像不同区域的背景灰度与纹理是否连续一致，分界两侧是否出现突变。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>曝光水平：</b>相邻泳道之间的明暗（曝光）水平是否平滑过渡，有无突然跳变。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>分辨率与压缩：</b>图像各区域的清晰度、噪声颗粒与压缩质量是否一致，有无来自不同来源的拼接区域。本项为初步筛查，最终须经人工查看图像确认。</p>
"""

DISCLAIMER_HTML = """
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">六、免责声明</h2>
<p style="font-size:9pt; color:#666; margin-bottom:3pt;">本报告由Paper Pecker自动生成，所有发现均基于算法分析，仅供参考。</p>
<p style="font-size:9pt; color:#666; margin-bottom:3pt;">部分图像相似性可能来源于合理的实验设计（如共用内参对照、标准分子量标记等），部分数据特征可能有合理的科学解释。</p>
<p style="font-size:9pt; color:#666; margin-bottom:3pt;">最终结论须结合原始实验材料、作者说明及领域专家的人工审核后方可作出。</p>
<p style="font-size:9pt; color:#999; margin-top:10pt;">报告生成日期：{date} | Paper Pecker</p>
"""

CN_SYSTEM_PROMPT = """你是学术论文风险检测领域的专家。
你需要完成两项任务：1) 从论文文本中提取作者和机构信息；2) 根据检测结果撰写中文预警报告。
要求：
1. 使用中文撰写，但必须使用普通审核人员能直接理解的解释性语言，避免只堆砌检测术语
2. 每个具体问题都必须先写清位置，再写问题类型、具体事实、需关注原因和核查依据
3. 严格按照指定的格式输出
4. 使用"提示""可能""有待进一步调查"等客观措辞，避免直接下结论
5. ⚠️ 严格禁止使用"造假""篡改""伪造"等指控性词汇，无论肯定或否定语境均不得出现。应使用"数据异常""图像完整性问题""规范性不足""统计特征偏离预期"等中性表述替代
6. 作者姓名和机构名称必须保持论文原文语言和拼写，不要翻译、意译或中英混写
7. 不要使用emoji表情符号"""

CN_REPORT_PROMPT = """请完成以下两项任务。

## 任务一：提取作者和机构信息

从以下论文前几页文本中，提取所有作者姓名和机构信息。
必须逐字保留论文原文中的作者姓名、机构名称、城市和国家/地区写法；不要翻译机构名，不要把英文机构改写成中文。如果原文就是中文则保留中文。如果无法确认完整机构，宁可留空也不要补写或翻译。

论文文本：
{first_pages_text}

## 任务二：生成中文预警报告分析部分（第三至第五节）

### 论文基本信息
- 标题: {title}
- 期刊: {journal}
- DOI: {doi}
- 总页数: {total_pages}
- 提取图片数: {total_images}
- 参考文献数: {total_references}

### 系统计算的风险等级（你必须与此保持一致，报告中不要出现任何分值/分数）
- 综合风险等级：{overall_level}
- 图像重复检测：{image_level}
- 数据异常检测：{data_level}
- 参考文献核验：{ref_level}
- 必须逐项写入第四节的高风险维度：{high_dimensions}

### 检测结果

图像重复检测结果：
{image_section}

图像拼接检测结果：
{splice_section}

数据异常检测结果：
{data_section}

参考文献核验结果：
{reference_section}

---

### 详细问题的强制写法

    对每一个被保留的问题，必须按下面的顺序写，不能只写“存在异常”“数据重复”或“标题不匹配”：

1. **问题位置**：原文第几页、Fig./Table/Supplementary Fig. 编号、公式编号；涉及源数据时还要写文件名、sheet 名、列名和行号或行范围。
2. **问题类型**：例如图文不一致、样本量不一致、跨实验数据复用、公式计算不一致、图像重复、参考文献著录不一致。
3. **具体情况**：列出实际观察到的数值、标签、图像或文字，并与论文写法逐项对照；涉及计算时写出公式、代入值、计算结果、论文报告值和差异。
4. **需关注原因**：用通俗语言说明这会影响哪个结论、参数、图表或可重复性；不要只写“影响论文可靠性”。
5. **核查依据**：说明是如何用原文、图注、原始数据、图片或统计计算核对出来的。

涉及确定性规则给出的表号、场景名、数据集名、模型名、指标名和数值时，必须逐字采用“位置/描述/详情”中的证据；
不得补充、替换或创造证据中未出现的实体标签，也不得把证据中的TG/JA/BB等名称改写成其他缩写。
对于 quantitative_claim_mismatch，详情中 mismatched_metrics[].metric 是该问题指标名的唯一来源；
例如 metric=RMSE 时，该问题的标题、具体情况和核查依据都必须写 RMSE，绝不能写成 ERR、MAE 或其他指标。

如果某项告警经核查属于正常现象，也要写明核查位置和排除理由，例如“第5页 Fig.2A 与原始图片文件的匹配是同一张投稿图片的PDF嵌入版，不是两组实验重复使用”。

请严格按照以下格式输出（不要包含```标记）：

===METADATA_JSON===
{{"authors": ["按论文原文抄录的作者1", "按论文原文抄录的作者2", ...], "affiliations": ["1. 按论文原文抄录的机构1", "2. 按论文原文抄录的机构2", ...]}}
===ANALYSIS_HTML===
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、检测结果总览</h2>
[用一段话概述整体风险等级和关键发现。注意：整体风险等级必须与系统计算的一致（{overall_level}），不要自行判断风险等级，不要出现任何分值/分数。然后用列表列出三个维度的问题数量和严重程度分布。**注意**：图像重复和图像拼接合并为"图像问题"一个维度，不要拆成两个独立维度]

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">四、主要发现详情</h2>
[必须依次使用“（一）图像问题”“（二）数据分析”“（三）参考文献”三个小节详细描述发现。图像重复和图像拼接同属“图像问题”维度，不要拆成两个独立问题。凡上文列入“必须逐项写入第四节的高风险维度”的维度，都必须在对应小节中写出至少一个完整问题，并按“问题位置、问题类型、具体情况、需关注原因、核查依据”五项展开；不得只在总览中标为高风险而在详情中省略。]

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">五、综合风险评估</h2>
[综合风险等级必须与系统一致：{overall_level}。围绕此等级展开分析，说明各维度的风险贡献，分析各类发现之间的关联性。禁止出现任何具体分值/分数]

格式要求：
- 正文段落使用: <p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
- 加粗关键词使用: <b>关键词</b>
- 小节标题使用: <p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">
- 禁止使用<table>标签，改用段落和列表展示数据
- 禁止使用Markdown语法（如**加粗**、*斜体*、#标题），必须使用HTML标签
- 列表使用: <ul style="font-size:9.5pt; color:#333; margin:3pt 0;"><li>内容</li></ul>
- 高风险用红色<span style="color:#c00;">高风险</span>，低风险用绿色<span style="color:#080;">低风险</span>（不要使用"中风险"，结论仅高风险或低风险两类）"""


def _format_image_findings(findings: list[dict]) -> str:
    if not findings:
        return "未检测到可疑图像重复。"
    lines = [f"共发现 {len(findings)} 对可疑图像：\n"]
    for i, f in enumerate(findings[:30]):
        lines.append(f"配对{i+1}: 第{f['page_a']}页 ↔ 第{f['page_b']}页, "
                     f"类型={f['match_type']}, 相似度={f['similarity_score']:.3f}, "
                     f"严重程度={f['severity']}")
        if f.get("details"):
            lines.append(f"  详情: {f['details']}")
    if len(findings) > 30:
        lines.append(f"\n... 以及其他 {len(findings)-30} 对（已省略）")
    high = sum(1 for f in findings if f.get("severity") == "high")
    med = sum(1 for f in findings if f.get("severity") == "medium")
    lines.append(f"\n严重程度分布: 高={high}, 中={med}, 低={len(findings)-high-med}")
    return "\n".join(lines)


def _format_splice_findings(findings: list[dict]) -> str:
    if not findings:
        return "未检测到疑似图像拼接。"
    lines = [f"共发现 {len(findings)} 处疑似图像拼接：\n"]
    for i, f in enumerate(findings[:30]):
        lines.append(f"疑似拼接{i+1}: 第{f.get('page', '?')}页, 严重程度={f.get('severity', 'medium')}")
        if f.get("details"):
            lines.append(f"  详情: {f['details']}")
        if f.get("annotation_path"):
            lines.append(f"  标注图: {f['annotation_path']}")
    if len(findings) > 30:
        lines.append(f"\n... 以及其他 {len(findings)-30} 处（已省略）")
    return "\n".join(lines)


def _format_data_findings(findings: list[dict]) -> str:
    if not findings:
        return "未检测到数据异常。"
    # Reproducible manuscript/source contradictions must survive the representative
    # sample when hundreds of weak spreadsheet alerts precede them.
    findings = sorted(findings, key=lambda item: item.get("test") not in _QUANTITATIVE_DECISIVE_TESTS)
    MAX_DETAIL_ITEMS = 100
    if len(findings) <= MAX_DETAIL_ITEMS:
        lines = [f"共发现 {len(findings)} 个数据异常：\n"]
        for i, f in enumerate(findings):
            lines.append(f"异常{i+1}: [{f['severity'].upper()}] {f['test']}")
            lines.append(f"  位置: {f['location']}")
            lines.append(f"  描述: {f['description']}")
            key_details = {k: v for k, v in f['details'].items()
                           if k not in ('testable', 'reason') and not isinstance(v, (list, dict))}
            if key_details:
                safe = {k: (bool(v) if hasattr(v, '__bool__') and type(v).__module__ == 'numpy' else v)
                        for k, v in key_details.items()}
                lines.append(f"  指标: {json.dumps(safe, ensure_ascii=False, default=str)}")
            if f.get("test") in {
                "quantitative_claim_mismatch",
                "quantitative_summary_mismatch",
                "scenario_definition_conflict",
            } | _QUANTITATIVE_DECISIVE_TESTS:
                lines.append(
                    "  详情: " + json.dumps(f.get("details", {}), ensure_ascii=False, default=str)
                )
            lines.append("")
        return "\n".join(lines)

    from collections import Counter
    high = [f for f in findings if f.get("severity") == "high"]
    medium = [f for f in findings if f.get("severity") == "medium"]
    low = [f for f in findings if f.get("severity") == "low"]
    type_counts = Counter(f.get("test", "unknown") for f in findings)

    lines = [f"共发现 {len(findings)} 个数据异常（高风险{len(high)}个，中等{len(medium)}个，低风险{len(low)}个）。"]
    lines.append(f"因异常数量过多，以下按类别汇总并展示代表性样例：\n")
    lines.append("各类型异常分布：")
    for test_type, count in type_counts.most_common():
        sev_counts = Counter(f.get("severity") for f in findings if f.get("test") == test_type)
        sev_str = "/".join(f"{s}={n}" for s, n in sorted(sev_counts.items()))
        lines.append(f"  - {test_type}: {count}个 ({sev_str})")
    lines.append("")

    sample_n = min(10, len(high))
    if high:
        lines.append(f"高风险异常代表样例（共{len(high)}个，展示{sample_n}个）：")
        for i, f in enumerate(high[:sample_n]):
            lines.append(f"  [{i+1}] {f['test']} | {f['location']}")
            lines.append(f"      {f['description']}")
        lines.append("")

    sample_m = min(10, len(medium))
    if medium:
        lines.append(f"中等严重程度异常代表样例（共{len(medium)}个，展示{sample_m}个）：")
        for i, f in enumerate(medium[:sample_m]):
            lines.append(f"  [{i+1}] {f['test']} | {f['location']}")
            lines.append(f"      {f['description']}")
        lines.append("")

    return "\n".join(lines)


def _format_reference_findings(findings: list[dict]) -> str:
    if not findings:
        return "所有参考文献核验通过，未发现问题。"
    lines = [f"共发现 {len(findings)} 个参考文献问题：\n"]
    for i, f in enumerate(findings):
        lines.append(f"问题{i+1}: [{f['severity'].upper()}] {f['issue_type']}")
        lines.append(f"  参考文献 #{f['ref_number']}: {f['ref_text'][:120]}...")
        lines.append(f"  描述: {f['description']}")
        lines.append("")
    return "\n".join(lines)


def _parse_combined_report_response(response: str) -> tuple[dict, str]:
    response = re.sub(r'^```(?:json|html)?\s*', '', response or '')
    response = re.sub(r'\s*```$', '', response)
    metadata = {"authors_full": [], "affiliations": []}
    analysis_html = response

    if "===METADATA_JSON===" in response and "===ANALYSIS_HTML===" in response:
        parts = response.split("===ANALYSIS_HTML===", 1)
        meta_part = parts[0].split("===METADATA_JSON===", 1)[-1].strip()
        analysis_html = parts[1].strip()
        try:
            meta_part = re.sub(r'^```(?:json)?\s*', '', meta_part)
            meta_part = re.sub(r'\s*```$', '', meta_part)
            meta_obj = json.loads(meta_part)
            metadata = {
                "authors_full": meta_obj.get("authors", []),
                "affiliations": meta_obj.get("affiliations", []),
            }
        except Exception as exc:
            log.warning("Failed to parse metadata JSON from combined response: %s", exc)

    analysis_html = re.sub(r'<table[^>]*>.*?</table>', '', analysis_html, flags=re.DOTALL)
    analysis_html = analysis_html.replace("为什么需要关注", "需关注原因")
    analysis_html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', analysis_html)
    analysis_html = re.sub(r'={3,}\s*[A-Z_][A-Z0-9_]*\s*={3,}', '', analysis_html)
    return metadata, analysis_html


def _protect_formula_references_html(value: str) -> str:
    """Keep equation references together when MuPDF lays out a line."""
    value = re.sub(
        r"(公式|式)\s*[（(]\s*\n\s*(\d+)\s*[）)]",
        r"\1(\2)",
        value or "",
    )
    return re.sub(
        r"(?<![A-Za-z0-9])((?:公式|式)\s*[（(]\s*\d+\s*[）)])",
        r'<span style="white-space:nowrap;">\1</span>',
        value,
    )


def _analysis_explanation_gaps(
    analysis_html: str,
    overall_level: str,
    high_dimensions: set[str] | None = None,
) -> list[str]:
    if overall_level != "高风险":
        return []
    plain = re.sub(r'<[^>]+>', ' ', analysis_html or '')
    required = {
        "问题位置": r"问题位置\s*[：:]",
        "问题类型": r"问题类型\s*[：:]",
        "具体情况": r"具体情况\s*[：:]",
        "需关注原因": r"需关注原因\s*[：:]",
        "核查依据": r"核查依据\s*[：:]",
    }
    gaps = [name for name, pattern in required.items() if not re.search(pattern, plain)]
    location_re = re.compile(
        r"(?:第\s*\d+\s*页|page\s*\d+|Fig(?:ure)?\.?\s*[A-Za-z0-9.-]+|"
        r"Table\s*[A-Za-z0-9.-]+|图\s*[A-Za-z0-9.-]+|表\s*[A-Za-z0-9.-]+|"
        r"公式?\s*[（(]?\d+|式\s*[（(]?\d+|sheet\s*['\"“”]?[^，,。;；\n]+|"
        r"(?:参考)?文献\s*(?:第|#)?\s*\d+\s*(?:条|项|号)?)",
        re.I,
    )
    if not location_re.search(plain):
        gaps.append("具体页码/图表/公式/sheet/参考文献编号")

    section_patterns = {
        "image": (r"（一）\s*图像(?:问题|分析)", r"（二）\s*数据"),
        "data": (r"（二）\s*数据(?:分析|异常)?", r"（三）\s*参考文献"),
        "reference": (r"（三）\s*参考文献", r"五、\s*综合风险评估"),
    }
    required_labels = ("问题位置", "问题类型", "具体情况", "需关注原因", "核查依据")
    for dimension in high_dimensions or set():
        patterns = section_patterns.get(dimension)
        if not patterns:
            continue
        start = re.search(patterns[0], plain)
        if not start:
            gaps.append(f"{dimension}高风险维度详情")
            continue
        end = re.search(patterns[1], plain[start.end():])
        block = plain[start.end():start.end() + end.start()] if end else plain[start.end():]
        if any(not re.search(rf"{label}\s*[：:]", block) for label in required_labels):
            gaps.append(f"{dimension}高风险维度完整说明")
    return gaps


def _build_explainability_downgrade_html() -> str:
    """Render low risk when a high signal has no complete, verifiable detail."""
    paragraph = (
        '<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">'
        '现有材料核查范围内未形成能够具体定位、逐项核对并独立复核的完整性问题说明，'
        '因此相关自动告警不作为高风险结论，综合判定为<span style="color:#080;">低风险</span>。'
        '</p>'
    )
    detail = (
        '<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">'
        '在现有可读材料范围内，未形成完整的“问题位置、问题类型、具体情况、需关注原因、核查依据”五项说明，'
        '未发现可以具体确认的论文完整性问题。'
        '</p>'
    )
    return f"""
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、检测结果总览</h2>
{paragraph}
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">四、主要发现详情</h2>
<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">（一）图像问题</p>
{detail}
<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">（二）数据分析</p>
{detail}
<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">（三）参考文献</p>
{detail}
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">五、综合风险评估</h2>
{paragraph}
"""


def _generate_analysis_html(findings: dict, first_pages_text: str) -> tuple[dict, str]:
    """Generate analysis HTML and extract metadata in a single LLM call.
    Returns (metadata_dict, analysis_html)."""
    paper = findings.get("paper", {})

    image_risk = _compute_image_risk(findings)
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall_risk = _compute_overall_risk(findings)
    _reconcile_risk_levels(findings, overall_risk, image_risk, data_risk, ref_risk)

    risk_kwargs = dict(
        overall_level=overall_risk["level"],
        image_level=image_risk["level"],
        data_level=data_risk["level"],
        ref_level=ref_risk["level"],
        high_dimensions="、".join(
            {"image": "图像", "data": "数据", "reference": "参考文献"}[dim]
            for dim in ("image", "data", "reference")
            if dim in overall_risk.get("high_dimensions", set())
        ) or "无",
    )

    truncated_text = first_pages_text[:6000]

    prompt = CN_REPORT_PROMPT.format(
        first_pages_text=truncated_text,
        title=paper.get("title", "未知"),
        journal=paper.get("journal", "未知"),
        doi=paper.get("doi", "未知"),
        total_pages=paper.get("total_pages", 0),
        total_images=paper.get("total_images", 0),
        total_references=paper.get("total_references", 0),
        image_section=_format_image_findings(findings.get("image_duplicates", [])),
        splice_section=_format_splice_findings(findings.get("image_splicing", [])),
        data_section=_format_data_findings(findings.get("data_anomalies", [])),
        reference_section=_format_reference_findings(findings.get("reference_issues", [])),
        **risk_kwargs,
    )
    prompt_len = len(prompt)
    if prompt_len > 20000:
        log.warning("Combined prompt too long (%d chars), rebuilding with compact format", prompt_len)
        findings_copy = findings.copy()
        for key in ("image_duplicates", "image_splicing", "data_anomalies", "reference_issues"):
            items = findings_copy.get(key, [])
            # Keep low-severity manuscript errata (including ordinary typos)
            # in the compact prompt so they remain visible in the report even
            # when a paper has many detector findings. They never affect the
            # high-risk gate, but dropping them would violate the errata policy.
            retained = [
                x for x in items
                if x.get("severity") in ("high", "medium")
                or x.get("test") == "verified_manuscript_content_conflict"
            ]
            if len(retained) < len(items):
                findings_copy[key] = retained
        prompt = CN_REPORT_PROMPT.format(
            first_pages_text=truncated_text,
            title=paper.get("title", "未知"),
            journal=paper.get("journal", "未知"),
            doi=paper.get("doi", "未知"),
            total_pages=paper.get("total_pages", 0),
            total_images=paper.get("total_images", 0),
            total_references=paper.get("total_references", 0),
            image_section=_format_image_findings(findings_copy.get("image_duplicates", [])),
            splice_section=_format_splice_findings(findings_copy.get("image_splicing", [])),
            data_section=_format_data_findings(findings_copy.get("data_anomalies", [])),
            reference_section=_format_reference_findings(findings_copy.get("reference_issues", [])),
            **risk_kwargs,
        )

    if findings.get("analysis_warnings"):
        prompt += (
            "\n材料覆盖限制（须在报告中如实说明，不能写成已完整核验，也不能把读取失败当作论文风险证据）：\n"
            + "\n".join(str(x) for x in findings["analysis_warnings"])
        )
    log.info("Generating combined metadata+analysis (prompt: %d chars)", len(prompt))
    response = chat(prompt, system=CN_SYSTEM_PROMPT, temperature=0.3)
    metadata, analysis_html = _parse_combined_report_response(response)
    gaps = _analysis_explanation_gaps(
        analysis_html,
        overall_risk["level"],
        overall_risk.get("high_dimensions", set()),
    )
    if gaps:
        log.warning("Initial report explanation is incomplete (%s); retrying once", ", ".join(gaps))
        repair_prompt = prompt + (
            "\n\n上一次输出不符合可解释性要求。请完整重写，并确保每个确认问题都按"
            "问题位置、问题类型、具体情况、需关注原因、核查依据五项依次展开。"
        )
        response = chat(repair_prompt, system=CN_SYSTEM_PROMPT, temperature=0.2)
        metadata, analysis_html = _parse_combined_report_response(response)
        gaps = _analysis_explanation_gaps(
            analysis_html,
            overall_risk["level"],
            overall_risk.get("high_dimensions", set()),
        )
        if gaps:
            # A report-writing failure must not erase reproducible calculations.
            # Use a bounded evidence-only fallback when data is the sole high
            # dimension. Other dimensions still require their own explanations.
            quantitative = [x for x in findings.get("data_anomalies", [])
                            if x.get("test") in _QUANTITATIVE_DECISIVE_TESTS
                            and x.get("severity") == "high" and x.get("description")
                            and x.get("details", {}).get("source_pdf")]
            if quantitative and overall_risk.get("high_dimensions") == {"data"}:
                analysis_html = _quantitative_evidence_html(quantitative, findings.get("analysis_warnings", []))
                return metadata, analysis_html
            if quantitative:
                raise ValueError("报告缺少其他风险维度说明；保留已复算的数据发现，报告生成失败，不降级检测结果")
            log.error(
                "Initial high-risk result lacks complete evidence detail after retry; "
                "downgrading effective conclusion to low risk: %s",
                ", ".join(gaps),
            )
            findings["_explainability_downgraded"] = True
            analysis_html = _build_explainability_downgrade_html()

    log.info("Extracted %d authors, %d affiliations from combined call",
             len(metadata["authors_full"]), len(metadata["affiliations"]))
    log.info("Combined response parsed (analysis: %d chars)", len(analysis_html))
    return metadata, analysis_html


def _quantitative_evidence_html(issues: list[dict], warnings: list[str]) -> str:
    esc = html_lib.escape
    blocks = []
    for issue in issues:
        blocks.append(
            f"<p><b>问题位置：</b>{esc(issue['location'])}</p>"
            "<p><b>问题类型：</b>公开数值、单位或计算关系不一致。</p>"
            f"<p><b>具体情况：</b>{esc(issue['description'])}</p>"
            "<p><b>需关注原因：</b>该差异需要作者更正或说明计算及数据处理口径。</p>"
            "<p><b>核查依据：</b>以上所列正文、公式、图表或随文源数据；逐项复算结果见具体情况。</p>"
        )
    coverage = "".join(f"<p>{esc(str(w))}</p>" for w in warnings)
    return (
        '<h2>三、检测结果总览</h2><p>有风险：发现可定位的数量或标注差异，具体如下。</p>'
        '<h2>四、主要发现详情</h2><p>（一）图像问题</p><p>当前图像筛查未形成独立风险判定依据。</p>'
        '<p>（二）数据分析</p>' + "".join(blocks) +
        '<p>（三）参考文献</p><p>当前引用核查未形成独立风险判定依据。</p>'
        '<h2>五、综合风险评估</h2><p>有风险：需核对上述具体差异，不等同于已经证明存在数据造假。</p>'
        + coverage
    )


WEIGHT_IMAGE = 0.20
WEIGHT_DATA = 0.65
WEIGHT_REF = 0.05

FRAUD_BONUS_3 = 0
FRAUD_BONUS_4 = 5

_DATA_HIGH_CAPS = {
    "arithmetic_sequence": 8,
    "coefficient_of_variation": 6,
    "short_curve_duplicate": 12,
    "rowwise_value_reuse": 8,
    # terminal_digit / sd_regularity emit only medium/low (never high), so they are
    # bounded by the medium/low score ceilings (min(25,..)/min(12,..)) in
    # _compute_dimension_risk and cannot, on their own, reach the data high-risk gate.
}

# These checks establish a direct, reproducible contradiction rather than a weak
# statistical signal.  One finding is therefore sufficient to put its dimension
# through the high-risk gate.  The rule names are generic; no DOI is special-cased.
from modules.quantitative_consistency import DECISIVE_TESTS as _QUANTITATIVE_DECISIVE_TESTS

_DECISIVE_DATA_TESTS = {
    "semantic_transform_conflict",
    "same_sheet_channel_duplicate",
    "cross_document_quantitative_mismatch",
    "group_sign_polarity_outlier",
} | _QUANTITATIVE_DECISIVE_TESTS
_DECISIVE_REFERENCE_TYPES = {"citation_entity_type_conflict"}


def _has_decisive_issue(issues: list[dict]) -> bool:
    return any(
        (issue.get("test") in _DECISIVE_DATA_TESTS
         and (issue.get("test") != "verified_manuscript_content_conflict"
              or issue.get("details", {}).get("verification_status") in {
                  "source_grounded_and_counterchecked", "unresolved_needs_review"})
         and (issue.get("test") not in _QUANTITATIVE_DECISIVE_TESTS or issue.get("severity") == "high"))
        or issue.get("issue_type") in _DECISIVE_REFERENCE_TYPES
        for issue in issues
    )


def _apply_data_caps(issues: list[dict]) -> list[dict]:
    """Cap per-type HIGH counts for data anomalies; excess HIGHs become MEDIUMs."""
    type_counts: dict[str, int] = {}
    result = []
    for issue in issues:
        sev = _reclassify_severity(issue)
        test_type = issue.get("test", "")
        cap = _DATA_HIGH_CAPS.get(test_type)
        if sev == "high" and cap is not None:
            n = type_counts.get(test_type, 0)
            if n >= cap:
                downgraded = dict(issue)
                downgraded["severity"] = "medium"
                result.append(downgraded)
            else:
                type_counts[test_type] = n + 1
                result.append(issue)
        else:
            result.append(issue)
    return result


def _reclassify_severity(issue: dict) -> str:
    """Runtime severity reclassification for scoring consistency."""
    sev = issue.get("severity", "low")
    if issue.get("test") == "linear_dependency":
        r2 = issue.get("details", {}).get("r_squared", 0)
        if r2 > 0.99999:
            # r²>0.99999 即便是严格线性缩放（slope=0.85, 0.8, 1.2 ...）也属于
            # 真实生物重复不可能出现的强模式（s41467-025-67914-y 漏判教训）。
            # 2026-06-23: 之前仅在 slope≈1.0 且 intercept≈0 时升 high，会漏掉
            # 比例缩放（BX = 0.85 * BW）和平移缩放等合成痕迹。
            return "high"
        return "low"
    if issue.get("test") == "cross_group_duplicate":
        overlap = issue.get("details", {}).get("overlap_ratio", 0)
        if overlap >= 0.95:
            return "high"
        if overlap >= 0.90:
            return "medium"
        return "low"
    issue_type = issue.get("issue_type", "")
    if issue_type in ("title_mismatch", "low_match_score"):
        ref_text = issue.get("ref_text", "")
        words_r = ref_text.split()
        if words_r:
            avg_wl = sum(len(w) for w in words_r) / len(words_r)
            space_r = ref_text.count(' ') / max(len(ref_text), 1)
            if avg_wl > 20 or space_r < 0.04:
                return "low"
        # Title-less (Vancouver-style) citations carry no article title, so a low
        # title-similarity is not a mismatch — never escalate these to high here.
        try:
            from modules.reference_checker import _is_titleless_citation
            if _is_titleless_citation(ref_text):
                return "medium" if issue.get("severity") == "high" else issue.get("severity", "low")
        except Exception:
            pass
        sim = issue.get("details", {}).get("title_similarity", 1.0)
        crossref_title = issue.get("details", {}).get("crossref_title", "")
        if crossref_title and "<" in crossref_title:
            import re as _re
            import difflib as _dl
            cr_clean = _re.sub(r'<[^>]+>', '', crossref_title)
            cr_clean = _re.sub(r'\s+', ' ', cr_clean).strip().lower()
            ref_text = issue.get("ref_text", "").lower()
            if cr_clean in ref_text:
                sim = 1.0
            elif ref_text and cr_clean:
                sim = _dl.SequenceMatcher(None, ref_text[:200], cr_clean).ratio()
        if sim < 0.5:
            return "high"
        if sim < 0.75:
            return "medium"
    return sev


def _compute_dimension_risk(issues: list[dict]) -> dict:
    high = sum(1 for x in issues if _reclassify_severity(x) == "high")
    medium = sum(1 for x in issues if _reclassify_severity(x) == "medium")
    low = sum(1 for x in issues if _reclassify_severity(x) == "low")

    high_contrib = min(80, high * 10)
    medium_contrib = min(25, medium * 4)
    low_contrib = min(12, round(low * 0.5))

    total = high + medium + low
    if total > 150:
        volume_bonus = 20
    elif total > 80:
        volume_bonus = 12
    elif total > 30:
        volume_bonus = 6
    else:
        volume_bonus = 0

    score = min(100, high_contrib + medium_contrib + low_contrib + volume_bonus)
    if _has_decisive_issue(issues):
        score = max(61, score)

    if score <= 30:
        level, color = "低风险", "#080"
    elif score <= 60:
        # No 中风险 outcome anywhere: a dimension scoring 31-60 is labelled 低风险
        # (it stays below the high-risk gate; high-risk is decided by is_high on
        # scores, not on this label). Kept the score for the breakdown display.
        level, color = "低风险", "#080"
    else:
        level, color = "高风险", "#c00"
    return {
        "score": score, "level": level, "color": color,
        "high": high, "medium": medium, "low": low,
    }


def _is_cross_image_duplicate(issue: dict) -> bool:
    """True for duplicate evidence across distinct images/pages; false for same-page layout echoes."""
    if not isinstance(issue, dict):
        return False
    if issue.get("risk_evidence") is False:
        return False
    a, b = issue.get("image_a", ""), issue.get("image_b", "")
    pa, pb = issue.get("page_a", 0), issue.get("page_b", 0)
    source_a, source_b = issue.get("source_a", ""), issue.get("source_b", "")
    if a and b and a == b:
        return False
    same_document = source_a == source_b if source_a and source_b else True
    if same_document and pa == pb and pa > 0:
        return False
    return True


def _compute_image_risk(findings: dict) -> dict:
    """Combined image-dimension risk for DB/API summaries.

    Only cross-image/page duplicate evidence contributes to the duplicate score. Same-page
    echoes are too often layout/template artefacts. Splice findings remain conservative
    medium findings, with >=2 corroborated suspects as an explicit high-risk gate.
    """
    cross_duplicates = [
        d for d in findings.get("image_duplicates", [])
        if _is_cross_image_duplicate(d)
    ]
    issues = cross_duplicates + list(findings.get("image_splicing", []))
    risk = _compute_dimension_risk(issues)
    splice_count = len(findings.get("image_splicing", []))
    if splice_count >= 2 and risk["score"] < 56:
        risk = dict(risk)
        risk["score"] = 56
        risk["level"] = "高风险"
        risk["color"] = "#c00"
    return risk


def _apply_explainability_dimension_downgrade(findings: dict, *risks: dict) -> None:
    """Make dimension levels match an explainability-based overall downgrade."""
    if not findings.get("_explainability_downgraded"):
        return
    for risk in risks:
        risk.update(score=0, level="低风险", color="#080")


def _count_fraud_indicators(findings: dict) -> int:
    data = findings.get("data_anomalies", [])
    img = findings.get("image_duplicates", [])
    has_cv = any(x.get("test") == "coefficient_of_variation" and x.get("severity") == "high" for x in data)
    has_arith = any(x.get("test") == "arithmetic_sequence" and x.get("severity") == "high" for x in data)
    has_geo = any(x.get("test") == "geometric_sequence" and x.get("severity") == "high" for x in data)
    has_cgd = any(
        x.get("test") == "cross_group_duplicate"
        and x.get("details", {}).get("overlap_ratio", 0) >= 0.80
        for x in data
    )
    has_channel_duplicate = any(
        x.get("test") == "same_sheet_channel_duplicate"
        and x.get("severity") == "high"
        for x in data
    )
    has_img = any(x.get("severity") == "high" and _is_cross_image_duplicate(x) for x in img)
    # 2026-06-23 修复：原本只判 slope≈1.0+intercept≈0 的恒等依赖，
    # 漏掉了 BX=0.85*BW 这种比例缩放（s41467-025-67914-y 漏判教训）。
    # 现在 r²>0.99999 任何 slope 都算强线性依赖。
    has_identity_dep = any(
        x.get("test") == "linear_dependency"
        and x.get("details", {}).get("r_squared", 0) > 0.99999
        for x in data
    )
    has_volume = len(data) > 100
    has_benford = any(
        x.get("test") == "benfords_law"
        and x.get("details", {}).get("p_value", 1) < 0.001
        for x in data
    )
    return sum([has_cv, has_arith, has_geo, has_cgd, has_channel_duplicate, has_img, has_identity_dep,
                has_volume, has_benford])


def _has_data_integrity_burden(capped_data: list[dict], reference_issues: list[dict]) -> bool:
    """Escalate broad, corroborated data-integrity burdens that lack one dominant HIGH hit.

    Some papers evade the single-dimension >60 gate because the evidence is spread across
    repeated medium-strength data reuse/distribution anomalies plus multiple reference
    verification failures. This trigger requires several independent signals so that
    proteomics-scale decimal/noise artefacts alone do not become high risk.
    """
    from collections import Counter

    data_severity = Counter(_reclassify_severity(x) for x in capped_data)
    tests = Counter(
        x.get("test")
        for x in capped_data
        if _reclassify_severity(x) in ("high", "medium")
    )
    ref_med_or_high = sum(
        1 for x in reference_issues
        if _reclassify_severity(x) in ("high", "medium")
    )

    return (
        len(capped_data) >= 15
        and data_severity["high"] + data_severity["medium"] >= 7
        and tests["value_recycling"] >= 3
        and tests["benfords_law"] >= 2
        and ref_med_or_high >= 4
    )


def _compute_overall_risk(findings: dict) -> dict:
    image_risk = _compute_image_risk(findings)
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_issues = findings.get("reference_issues", [])
    ref_risk = _compute_dimension_risk(ref_issues)
    decisive_data_trigger = _has_decisive_issue(capped_data)
    decisive_reference_trigger = _has_decisive_issue(ref_issues)

    max_dim = max(image_risk["score"], data_risk["score"], ref_risk["score"])
    weighted_avg = (
        image_risk["score"] * WEIGHT_IMAGE +
        data_risk["score"] * WEIGHT_DATA +
        ref_risk["score"] * WEIGHT_REF +
        max_dim * 0.10
    )
    base_score = min(100, round(max(
        weighted_avg,
        data_risk["score"] * 0.95,
        image_risk["score"] * 0.85,
        ref_risk["score"] * 0.85,
    )))

    fraud_count = _count_fraud_indicators(findings)
    has_high_evidence = data_risk["high"] > 0 or image_risk["high"] > 0
    if has_high_evidence:
        bonus = FRAUD_BONUS_4 if fraud_count >= 4 else (FRAUD_BONUS_3 if fraud_count >= 3 else 0)
    else:
        bonus = 0

    score = min(100, base_score + bonus)

    if decisive_data_trigger or decisive_reference_trigger:
        score = max(61, score)

    if max_dim > 60 and score <= 60:
        score = 61

    has_img_issues = any(_is_cross_image_duplicate(x) for x in findings.get("image_duplicates", []))
    has_data_issues = len(findings.get("data_anomalies", [])) >= 5
    if has_img_issues and has_data_issues and score < 56:
        score = 56

    from collections import Counter
    cross_page_img_counts = Counter()
    cross_page_page_pairs = set()
    for d in findings.get("image_duplicates", []):
        if not isinstance(d, dict):
            continue
        if not _is_cross_image_duplicate(d):
            continue
        a, b = d.get("image_a", ""), d.get("image_b", "")
        pa, pb = d.get("page_a", 0), d.get("page_b", 0)
        cross_page_img_counts[a] += 1
        cross_page_img_counts[b] += 1
        cross_page_page_pairs.add((min(pa, pb), max(pa, pb)) if (pa > 0 and pb > 0) else (min(a, b), max(a, b)))
    cross_page_imgs = 0
    if len(cross_page_page_pairs) <= 8:
        seen_pairs = set()
        for d in findings.get("image_duplicates", []):
            if not isinstance(d, dict):
                continue
            if not _is_cross_image_duplicate(d):
                continue
            a, b = d.get("image_a", ""), d.get("image_b", "")
            pa, pb = d.get("page_a", 0), d.get("page_b", 0)
            pair_key = (min(a, b), max(a, b))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            if cross_page_img_counts.get(a, 0) > 4 or cross_page_img_counts.get(b, 0) > 4:
                continue
            img_path = a
            if img_path and os.path.exists(img_path):
                fsize = os.path.getsize(img_path)
                if fsize < 2000:
                    continue
            cross_page_imgs += 1
    cross_page_unique_imgs = len({
        k for k in cross_page_img_counts if cross_page_img_counts[k] > 0
    })
    if cross_page_unique_imgs > 10:
        cross_page_imgs = 0
    if cross_page_imgs > 0:
        imgs_per_pair = cross_page_unique_imgs / max(len(cross_page_page_pairs), 1)
        if cross_page_imgs < 10 or imgs_per_pair > 3:
            cross_page_imgs = 0
    if cross_page_imgs >= 10 and score < 56:
        score = 56

    ref_high_genuine = 0
    ref_high_filtered = 0
    for r in ref_issues:
        if _reclassify_severity(r) != "high":
            continue
        # A resolved DOI means the citation points to a REAL existing paper. A low
        # title-similarity in that case is usually an extraction/format artifact
        # (e.g. authors-first citation styles), NOT fabrication. Genuine reference
        # fabrication shows up as an unresolvable DOI (doi_not_found / not_found).
        # The runtime "verified" flag is popped before report.json is written, so we
        # detect resolution via the recorded DOI in details instead.
        issue_type = r.get("issue_type", "")
        _det = r.get("details", {})
        _resolved_doi = _det.get("doi") or _det.get("matched_doi")
        if issue_type in ("title_mismatch", "low_match_score") and _resolved_doi:
            ref_high_filtered += 1
            continue
        ref_text = r.get("ref_text", "")
        words = ref_text.split()
        avg_word_len = sum(len(w) for w in words) / max(len(words), 1)
        if len(ref_text) < 20 or len(words) < 3 or avg_word_len > 25:
            ref_high_filtered += 1
            continue
        max_word_len = max((len(w) for w in words), default=0)
        if max_word_len > 30:
            ref_high_filtered += 1
            continue
        if len(re.findall(r'\(\d{4}\)', ref_text)) >= 2:
            ref_high_filtered += 1
            continue
        if ref_text.lstrip().startswith("doi:") or ref_text.lstrip().startswith("DOI:"):
            ref_high_filtered += 1
            continue
        if re.search(r'\d{1,3}\.\s+[A-Z]', ref_text[30:]):
            ref_high_filtered += 1
            continue
        if 'doi:' in ref_text[20:].lower() or 'doi.org/' in ref_text[20:]:
            ref_high_filtered += 1
            continue
        if re.search(r'(?:pone|s\d{4})[.-]\d{4,}', ref_text):
            ref_high_filtered += 1
            continue
        if re.search(r'\b\d{4}/[a-z]+\.\d{4,}', ref_text):
            ref_high_filtered += 1
            continue
        if re.search(r'\b\d{3,}/[A-Za-z]\d{3,}', ref_text):
            ref_high_filtered += 1
            continue
        ref_high_genuine += 1
    ref_total = findings.get("paper", {}).get("total_references", 0) or len(ref_issues)
    ref_high_ratio = ref_high_genuine / ref_total if ref_total > 0 else 0
    ref_high_all = ref_high_genuine + ref_high_filtered
    garbled_ratio = ref_high_filtered / ref_high_all if ref_high_all > 0 else 0
    if garbled_ratio > 0.5:
        genuine_threshold = 10
    elif garbled_ratio > 0.3:
        genuine_threshold = 8
    else:
        genuine_threshold = 5
    ref_fabrication_trigger = (
        ref_high_genuine >= genuine_threshold
        and ref_high_ratio > 0.08
        and ref_high_ratio < 0.5
    )

    cross_page_full_dup = sum(
        1 for d in findings.get("image_duplicates", [])
        if isinstance(d, dict) and d.get("match_type") == "full_duplicate"
        and d.get("severity") == "high"
        and _is_cross_image_duplicate(d)
    )
    full_dup_trigger = 4 <= cross_page_full_dup <= 30

    # Splice pre-screen: a single suspect is too false-positive-prone to escalate
    # (legitimate lane dividers can trip one seam), so require >=2 confirmed findings.
    # check_splicing already gates each finding on a corroborated seam; two or more
    # suspects raise the image dimension and overall score to a consistent review gate.
    splice_count = len(findings.get("image_splicing", []))
    splice_trigger = splice_count >= 2
    if splice_trigger and score < 56:
        score = 56

    data_high_trigger = data_risk["score"] > 60
    data_integrity_burden_trigger = _has_data_integrity_burden(capped_data, ref_issues)
    if data_integrity_burden_trigger and score < 61:
        score = 61
    is_high = (
        decisive_data_trigger
        or decisive_reference_trigger
        or data_high_trigger
        or data_integrity_burden_trigger
        or cross_page_imgs >= 10
        or full_dup_trigger
        or ref_fabrication_trigger
        or (fraud_count >= 4 and 0 < image_risk["score"] < 80)
        or (score >= 60 and data_risk["score"] >= 60)
        or (image_risk["score"] >= 30 and data_risk["score"] >= 5)
        or splice_trigger
        # 2026-06-07: 漏判修复 — 18篇确认高风险论文因单一维度高分但组合条件
        # 不满足而被判低风险。新增独立触发条件：
        or image_risk["score"] >= 80     # 图像维度极高（如 sciimmunol.adh1306 pic=100）
        or (data_risk["high"] >= 1 and data_risk["score"] >= 20)  # 数据维度需有 high 证据
        or (data_risk["score"] >= 45 and data_risk["medium"] >= 8)  # 大量中等数据信号
        or (ref_risk["score"] >= 40 and data_risk["score"] >= 15)  # 引用+数据组合
        or score >= 56                   # 总分接近阈值（如 41587-020-00781-8 total=56）
    )

    high_dims = set()
    if is_high:
        if decisive_data_trigger:
            high_dims.add("data")
        if decisive_reference_trigger:
            high_dims.add("reference")
        if data_high_trigger or data_integrity_burden_trigger:
            high_dims.add("data")
        if data_integrity_burden_trigger:
            high_dims.add("reference")
        if cross_page_imgs >= 10:
            high_dims.add("image")
        if full_dup_trigger:
            high_dims.add("image")
        if ref_fabrication_trigger:
            high_dims.add("reference")
        if fraud_count >= 4 and 0 < image_risk["score"] < 80:
            high_dims.add("data")
        if score >= 60 and data_risk["score"] >= 60:
            high_dims.add("data")
        if image_risk["score"] >= 30 and data_risk["score"] >= 5:
            high_dims.add("image")
        if splice_trigger:
            high_dims.add("image")
        # 2026-06-07 新增触发条件的维度追踪
        if image_risk["score"] >= 80:
            high_dims.add("image")
        if data_risk["high"] >= 1 and data_risk["score"] >= 20:
            high_dims.add("data")
        if data_risk["score"] >= 45 and data_risk["medium"] >= 8:
            high_dims.add("data")
        if ref_risk["score"] >= 40 and data_risk["score"] >= 15:
            high_dims.add("reference")
            high_dims.add("data")
        if score >= 56:
            # 总分触发时，标记贡献最大的维度
            best = max(
                ("image", image_risk["score"]),
                ("data", data_risk["score"]),
                ("ref", ref_risk["score"]),
                key=lambda x: x[1],
            )
            if best[1] > 0:
                high_dims.add({"image": "image", "data": "data", "ref": "reference"}[best[0]])
    # A caller may preserve an externally established high-risk result through
    # _force_risk_level. Keep the overview internally consistent by identifying
    # the dimensions that actually carry evidence instead of showing all green.
    if findings.get("_force_risk_level") == "高风险" and not high_dims:
        if image_risk["high"]:
            high_dims.add("image")
        if data_risk["high"]:
            high_dims.add("data")
        if ref_risk["high"]:
            high_dims.add("reference")
        if not high_dims:
            best = max(
                ("image", image_risk["score"]),
                ("data", data_risk["score"]),
                ("reference", ref_risk["score"]),
                key=lambda item: item[1],
            )
            if best[1] > 0:
                high_dims.add(best[0])

    if findings.get("_force_risk_level"):
        level = findings["_force_risk_level"]
        color = "#c00" if level == "高风险" else "#080"
    elif is_high:
        level, color = "高风险", "#c00"
    else:
        level, color = "低风险", "#080"

    all_issues = (findings.get("image_duplicates", []) +
                  findings.get("image_splicing", []) +
                  findings.get("data_anomalies", []) +
                  findings.get("reference_issues", []))
    high = sum(1 for x in all_issues if x.get("severity") == "high")
    medium = sum(1 for x in all_issues if x.get("severity") == "medium")
    low = sum(1 for x in all_issues if x.get("severity") == "low")
    if findings.get("_explainability_downgraded"):
        # A high detector signal without a complete, location-based explanation
        # is not publishable as a high-risk conclusion. Keep raw issue counts
        # for auditability, but make the effective risk and score low.
        level, color, score, high_dims = "低风险", "#080", 0, set()
    elif level == "高风险" and not high_dims:
        # Never publish a high overall result with no named high-risk dimension.
        level, color = "低风险", "#080"
    return {
        "score": score, "level": level, "color": color,
        "high": high, "medium": medium, "low": low,
        "high_dimensions": high_dims,
    }


def _reconcile_risk_levels(
    findings: dict,
    overall: dict,
    image_risk: dict,
    data_risk: dict,
    ref_risk: dict,
) -> dict:
    """Enforce one consistent overall/dimension risk vocabulary everywhere."""
    dimensions = {
        "image": image_risk,
        "data": data_risk,
        "reference": ref_risk,
    }
    high_dimensions = set(overall.get("high_dimensions", set())) & set(dimensions)

    if overall.get("level") != "高风险" or not high_dimensions:
        overall["level"] = "低风险"
        overall["color"] = "#080"
        overall["high_dimensions"] = set()
        for risk in dimensions.values():
            risk.update(level="低风险", color="#080")
        return overall

    overall["high_dimensions"] = high_dimensions
    for name, risk in dimensions.items():
        if name in high_dimensions:
            risk.update(level="高风险", color="#c00")
        else:
            risk.update(level="低风险", color="#080")
    return overall


def _build_risk_score_html(findings: dict) -> str:
    image_risk = _compute_image_risk(findings)
    raw_data = findings.get("data_anomalies", [])
    raw_data_high = sum(1 for x in raw_data if x.get("severity") == "high")
    raw_data_medium = sum(1 for x in raw_data if x.get("severity") == "medium")
    raw_data_low = sum(1 for x in raw_data if x.get("severity") == "low")
    capped_data = _apply_data_caps(raw_data)
    capped_data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)
    _apply_explainability_dimension_downgrade(
        findings, image_risk, capped_data_risk, ref_risk
    )
    _reconcile_risk_levels(
        findings, overall, image_risk, capped_data_risk, ref_risk
    )

    scored_high = capped_data_risk["high"]
    note_html = ""
    if scored_high != raw_data_high:
        note_html = f"""
<p style="font-size:8.5pt; color:#888; margin-top:2pt; margin-bottom:4pt;">
注：原始{raw_data_high}项HIGH告警经评分规则调整（含线性依赖重分类及同类型封顶），风险等级按{scored_high}项HIGH计算。
</p>"""

    # Image-dimension block: combine 图像重复 (image_duplicates) and 图像拼接
    # (image_splicing) into a single "图像问题" line so readers see one overall
    # image-risk verdict. Show only the total count — no per-sub-dimension split.
    splice = findings.get("image_splicing", [])
    splice_n = len(splice)
    duplicate_pairs = len(findings.get("image_duplicates", []))
    total_image_issues = duplicate_pairs + splice_n
    image_risk_combined = image_risk
    if splice_n >= 2 and image_risk_combined["level"] != "高风险":
        image_risk_combined = dict(image_risk_combined)
        image_risk_combined["level"] = "高风险"
        image_risk_combined["color"] = "#c00"
    splice_items_html = ""
    if splice:
        items = "".join(
            f'<li>第{s.get("page", "?")}页：{s.get("details", "疑似拼接")}</li>'
            for s in splice[:15]
        )
        more = f'<li>……另有 {splice_n - 15} 处（略）</li>' if splice_n > 15 else ""
        splice_items_html = f'\n<ul style="font-size:9pt; color:#555; margin:2pt 0 6pt 0;">{items}{more}</ul>'

    return f"""
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">二、初筛风险概览</h2>

<p style="font-size:9pt; color:#666; margin-bottom:8pt;">以下为代码确定性规则的初筛结果，最终以 AI 复核结论为准。</p>

<p style="font-size:16pt; font-weight:bold; color:{overall['color']}; text-align:center; margin:14pt 0 6pt 0;">
综合风险等级：{overall['level']}
</p>

<hr style="border:none; border-top:1px solid #eee; margin:8pt 0;">

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:6pt;">各维度风险评估：</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">
<b>图像问题：</b><span style="color:{image_risk_combined['color']}; font-weight:bold;">{image_risk_combined['level']}</span>
　　共 {total_image_issues} 项可疑问题
</p>
{splice_items_html}

<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">
<b>数据异常检测：</b><span style="color:{capped_data_risk['color']}; font-weight:bold;">{capped_data_risk['level']}</span>
　　高风险 {raw_data_high}项　中等 {raw_data_medium}项　低风险 {raw_data_low}项
</p>{note_html}

<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">
<b>参考文献核验：</b><span style="color:{ref_risk['color']}; font-weight:bold;">{ref_risk['level']}</span>
　　高风险 {ref_risk['high']}项　中等 {ref_risk['medium']}项　低风险 {ref_risk['low']}项
</p>
<hr style="border:none; border-top:1px solid #eee; margin:10pt 0;">
"""


def _build_full_html(findings: dict, analysis_html: str) -> str:
    paper = findings.get("paper", {})
    title = paper.get("title", "未知")
    authors_full = paper.get("authors_full", [])
    affiliations = paper.get("affiliations", [])
    sjtu_authors = paper.get("sjtu_authors", [])
    sjtu_author_type = paper.get("sjtu_author_type", "")
    sjtu_departments = paper.get("sjtu_departments", [])
    journal = paper.get("journal", "未知")
    doi = paper.get("doi", "未知")
    total_pages = paper.get("total_pages", 0)
    total_images = paper.get("total_images", 0)
    total_refs = paper.get("total_references", 0)
    today = date.today().strftime("%Y年%m月%d日")

    sjtu_html = ""
    if sjtu_authors:
        sjtu_html += f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>涉及的交大作者：</b>{", ".join(sjtu_authors)}</p>'
    if sjtu_author_type:
        sjtu_html += f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>交大作者类型：</b>{sjtu_author_type}</p>'
    if sjtu_departments:
        sjtu_html += '<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>交大作者单位：</b></p>'
        for dept in sjtu_departments:
            sjtu_html += f'<p style="font-size:8.5pt; color:#555; margin-bottom:1pt; margin-left:12pt;">{dept}</p>'

    if authors_full:
        all_author_line = f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>全部作者：</b>{", ".join(authors_full)}</p>'
    else:
        all_author_line = ""

    all_affiliation_html = ""
    if affiliations:
        all_affiliation_html = '<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>全部作者单位：</b></p>'
        for aff in affiliations:
            all_affiliation_html += f'<p style="font-size:8.5pt; color:#555; margin-bottom:1pt; margin-left:12pt;">{aff}</p>'

    stats_parts = []
    if total_pages:
        stats_parts.append(f'<b>论文页数：</b>{total_pages}')
    if total_images:
        stats_parts.append(f'<b>提取图片数：</b>{total_images}')
    if total_refs:
        stats_parts.append(f'<b>参考文献数：</b>{total_refs}')
    stats_line = ""
    if stats_parts:
        stats_line = f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;">{"　　".join(stats_parts)}</p>'

    header_html = f"""
<h1 style="font-size:18pt; color:#1a1a1a; text-align:center; margin-bottom:4pt;">论文复核报告</h1>
<p style="font-size:10.5pt; color:#555; text-align:center; margin-top:0; margin-bottom:16pt;">Paper Review Report</p>

<hr style="border:none; border-top:1px solid #ccc; margin:10pt 0;">

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">一、论文基本信息</h2>

<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>论文标题：</b>{title}</p>
{sjtu_html}
{all_author_line}
{all_affiliation_html}
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>发表期刊：</b>{journal}</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>DOI：</b>{doi}</p>
{stats_line}
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>分析日期：</b>{today}</p>
"""

    risk_score_html = _build_risk_score_html(findings)
    disclaimer = DISCLAIMER_HTML.format(date=today)

    analysis_html = _protect_formula_references_html(
        analysis_html.replace("为什么需要关注", "需关注原因")
    )
    full_html = f"""
<body style='font-family: {FONT_REGULAR};'>
{header_html}
{risk_score_html}
{analysis_html}
{disclaimer}
</body>
"""
    return full_html


def _soften_risk_wording(html: str) -> str:
    """Display-only wording: keep "高风险" / "建议高风险" in the final PDF
    verbatim. Per the user's 2026-06-26 rule, the report must say
    "建议高风险" (DB=高风险) and "建议低风险" (DB=低风险) — never
    "疑似高风险". This function is now a no-op (kept for call-site
    compatibility)."""
    return html


def _sanitize_internal_references(html: str) -> str:
    """Remove internal file/field names that should never appear in user-facing reports."""
    html = re.sub(r"report\.json", "初检报告", html)
    replacements = (
        ("image_duplicates", "图像重复"),
        ("image_splicing", "图像拼接"),
        ("data_anomalies", "数据异常"),
        ("reference_issues", "参考文献问题"),
        ("cross_sheet_row_duplicate", "跨表整行重复"),
        ("cross_sheet_column_duplicate", "跨表整列重复"),
        ("cross_sheet_reuse", "跨表数据复用"),
        ("cross_group_duplicate", "跨组重复"),
        ("duplicate_column_pairs", "重复数据列"),
        ("decimal_uniformity", "小数位一致性"),
        ("benfords_law", "首位数字分布"),
        ("value_recycling", "数值重复使用"),
        ("terminal_digit", "末位数字分布"),
        ("sd_regularity", "标准差规律性"),
        ("arithmetic_sequence", "等差数列"),
        ("geometric_sequence", "等比数列"),
        ("coefficient_of_variation", "变异系数"),
        ("not_found", "未收录"),
        ("doi_mismatch", "DOI不匹配"),
        ("citation_without_reference", "正文引用缺少参考文献条目"),
        ("uncited_references", "参考文献未在正文引用"),
        ("duplicate_reference", "重复参考文献"),
        ("duplicate_reference_doi", "重复参考文献 DOI"),
        ("future_reference_year", "异常参考文献年份"),
        ("review_method_incomplete", "综述方法信息不完整"),
        ("text_repetition", "正文段落重复"),
        ("PHash", "感知哈希"),
    )
    for old, new in replacements:
        html = html.replace(old, new)
    # "Benford定律" → "首位数字分布定律"; avoid double prefix like "首位数首位数字分布定律"
    html = re.sub(r"首位数字?Benford定律", "首位数字分布定律", html)
    html = re.sub(r"Benford定律", "首位数字分布定律", html)
    html = html.replace("Benford", "首位数字分布")
    # Internal file names and script paths
    html = re.sub(r"review_evidence\.json", "复核证据包", html)
    html = re.sub(r"scan_columns\.py", "列对扫描工具", html)
    html = re.sub(r"forensic_checks\.py", "统计核查工具", html)
    html = re.sub(r"coverage_validator", "覆盖率校验", html)
    # English severity labels that agents sometimes leave in text
    html = re.sub(r"\bHIGH级别", "高风险级别", html)
    html = re.sub(r"\bMEDIUM级别", "中等级别", html)
    html = re.sub(r"\bLOW级别", "低级别", html)
    html = re.sub(r"\bseverity\s*=\s*high\b", "严重程度=高", html)
    html = re.sub(r"\bseverity\s*=\s*medium\b", "严重程度=中", html)
    html = re.sub(r"\bseverity\s*=\s*low\b", "严重程度=低", html)
    html = re.sub(r"\blow_match_score\b", "标题匹配度偏低", html)
    html = re.sub(r"\bdoi_truncated\b", "DOI截断", html)
    html = re.sub(r"\bdoi_unverified\b", "DOI未验证", html)
    html = re.sub(r"\btitle_mismatch\b", "标题不匹配", html)
    html = re.sub(r"\blinear_dependency\b", "线性依赖", html)
    return html


def _normalize_analysis_consistency(findings: dict, html: str) -> str:
    """Align free-form narrative claims with deterministic scores and counts."""
    overall = _compute_overall_risk(findings)
    high_dimensions = overall.get("high_dimensions", set())
    dimension_aliases = {
        "image": r"图像(?:问题)?",
        "data": r"数据(?:异常检测|分析)?",
        "reference": r"参考文献(?:核验)?",
    }
    for dimension, alias in dimension_aliases.items():
        expected = "高风险" if dimension in high_dimensions else "低风险"
        html = re.sub(
            rf"({alias}维度\s*(?:的风险等级)?\s*(?:为|：))\s*(?:高风险|低风险|中等风险)",
            rf"\g<1>{expected}",
            html,
        )

    # The product has only high/low conclusions.  "中等" remains valid as an
    # issue severity, but must never be presented as a dimension risk level.
    html = re.sub(r"中等风险", "低风险", html)

    total = int(findings.get("summary", {}).get("total_issues", 0) or 0)
    html = re.sub(
        r"(检测范围覆盖[^。]{0,160}?，\s*共发现)\s*\d+\s*(处|项)问题",
        rf"\g<1>{total}\2问题",
        html,
    )
    return html


def _sanitize_paper_text(s: str) -> str:
    """删除 source 里 control chars + 修特定已知错编码.

    例: report.json paper.title 里 EuBa2Cu3O7\\x01d 实际是 EuBa2Cu3O7δ
    (Elsevier extraction 把 U+03B4 δ 错编为 U+0001 SOH + 'd').
    也处理 U+0000-U+001F 范围内除 \\t\\n\\r 外的所有 control chars.
    """
    if not s:
        return s
    # 1) 修特定已知错: SOH + 字母 模式 -> 对应希腊字母
    fix_map = {
        '\x01d': 'δ',
        '\x01D': 'Δ',
        '\x01a': 'α',
        '\x01A': 'Α',
        '\x01b': 'β',
        '\x01B': 'Β',
        '\x01g': 'γ',
        '\x01G': 'Γ',
        '\x01s': 'σ',
        '\x01S': 'Σ',
        '\x01m': 'μ',
        '\x01M': 'Μ',
        '\x01p': 'π',
        '\x01P': 'Π',
        '\x01l': 'λ',
        '\x01L': 'Λ',
        '\x01t': 'τ',
        '\x01T': 'Τ',
    }
    for bad, good in fix_map.items():
        s = s.replace(bad, good)
    # 2) 删剩余 control chars (除 \t \n \r)
    s = ''.join(c for c in s if (c >= ' ' or c in '\t\n\r'))
    return s


def _sanitize_paper_dict(paper: dict) -> dict:
    """对 paper dict 里所有字符串字段做 _sanitize_paper_text"""
    if not paper:
        return paper
    out = dict(paper)
    str_keys = ('title', 'author', 'author_type', 'department',
                'author_all', 'department_all', 'journal')
    for k in str_keys:
        if k in out and isinstance(out[k], str):
            out[k] = _sanitize_paper_text(out[k])
    return out


def _render_pdf(html: str, output_path: str):
    validate_report_render_environment()
    html = _soften_risk_wording(html)
    html = _sanitize_internal_references(html)
    story = fitz.Story(html)
    writer = fitz.DocumentWriter(output_path)
    mediabox = fitz.paper_rect("a4")
    margin = 72
    where = mediabox + (margin, margin, -margin, -margin)

    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()

    writer.close()
    log.info("Chinese PDF saved to %s", output_path)


def _sanitize_filename_part(value: str, max_chars: int = 80) -> str:
    """Return a compact filename-safe component for report PDFs.

    Some publisher metadata puts journal, issue, date, and abstract text into
    the title field. Keep a short normalized fragment and strip punctuation that
    has caused brittle cleanup behavior in batch runs.
    """
    value = html_lib.unescape(str(value or ""))
    value = re.sub(r"[\u2010-\u2015\u2212]", "-", value)
    value = re.sub(r"<[^>]{0,200}>", " ", value)
    value = re.split(r"\b(?:Graphical\s+Abstract|Abstract|BACKGROUND|CONTEXT)\b", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value)
    value = re.sub(r"[,;，；]+", " ", value)
    value = re.sub(r"[^\w.\-\u4e00-\u9fff ]+", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value).strip("._-")
    return value[:max_chars]


def _limit_text_bytes(value: str, max_bytes: int) -> str:
    """Trim text to a UTF-8 byte limit without splitting multibyte characters."""
    value = str(value or "")
    while len(value.encode("utf-8")) > max_bytes and value:
        value = value[:-1]
    return value


def _make_filename(doi: str, title: str) -> str:
    # Rendering sanitizes metadata before naming its PDF; URL builders must use
    # the same title when computing the filename digest.
    title = _sanitize_paper_text(title or "")
    max_bytes = 180
    suffix = ".pdf"
    doi_part = _limit_text_bytes(doi_to_slug(doi) or "unknown", 120)
    digest_src = f"{doi or ''}\n{title or ''}".encode()
    digest = hashlib.sha1(digest_src).hexdigest()[:8]
    title_part = _sanitize_filename_part(title, max_chars=72)
    fixed_bytes = len(f"{doi_part}_{digest}{suffix}".encode())
    if title_part:
        title_budget = max(0, max_bytes - fixed_bytes - 1)
        title_part = _limit_text_bytes(title_part, title_budget).strip("._-")
    if title_part:
        return f"{doi_part}_{title_part}_{digest}{suffix}"
    return f"{doi_part}_{digest}{suffix}"


def doi_to_slug(doi: str) -> str:
    """Filesystem-safe slug for a DOI, shared by the 初审 and 复核 report paths so a
    stored URL always matches the file actually written. Strips the doi.org prefix,
    maps '/' to '_', and removes the same unsafe characters as _make_filename (a plain
    doi.replace('/','_') diverged for DOIs containing < > : " \\ | ? *).

    Always returns a lowercased slug: DOIs are case-insensitive per ISO 26324, and
    some batch inputs arrive uppercased (e.g. ``CAN-20-3960``) which used to produce
    a PDF whose filename did not match the lowercased URL in the DB (nginx is
    case-sensitive), causing 404. Normalize here so the PDF, the URL in
    review_report_url, and the slug in evidence.json all line up. The canonical DOI
    string in the database keeps its original case.
    """
    raw = str(doi or "").strip()
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.I).strip()
    doi_clean = normalize_doi(raw) or re.split(r"[\s,;，；]+", raw, maxsplit=1)[0]
    doi_clean = doi_clean.rstrip(".")
    doi_clean = doi_clean.replace("/", "_")
    return _sanitize_filename_part(doi_clean, max_chars=120).lower()


def _coalesce_metadata(existing: dict, extracted: dict) -> dict:
    """Prefer deterministic upstream metadata over the report-writing LLM output."""
    existing_authors = existing.get("authors_full") or []
    existing_affiliations = existing.get("affiliations") or []
    return {
        "authors_full": existing_authors or extracted.get("authors_full", []) or [],
        "affiliations": existing_affiliations or extracted.get("affiliations", []) or [],
    }


def generate_chinese_pdf(findings: dict, chinese_reports_dir: str, first_pages_text: str = ""):
    """Generate Chinese PDF report. Returns (output_path, metadata_dict) or (None, metadata_dict)."""
    caller_findings = findings
    # 修 source 里 control char 错编码 (e.g. δ 错编为 \x01d) + 删 control chars
    paper = _sanitize_paper_dict(findings.get("paper", {}))
    findings = dict(findings)
    findings["paper"] = paper
    doi = paper.get("doi", "unknown")
    title = paper.get("title", "unknown")

    cn_dir = Path(chinese_reports_dir)
    cn_dir.mkdir(parents=True, exist_ok=True)

    filename = _make_filename(doi, title)
    output_path = str(cn_dir / filename)

    try:
        metadata, analysis_html = _generate_analysis_html(findings, first_pages_text)
        if findings.get("_explainability_downgraded"):
            caller_findings["_explainability_downgraded"] = True
            caller_findings["initial_risk_level"] = "低风险"
            caller_findings["initial_risk_score"] = 0
        metadata = _coalesce_metadata(paper, metadata)
        findings["paper"]["authors_full"] = metadata.get("authors_full", [])
        findings["paper"]["affiliations"] = metadata.get("affiliations", [])
        analysis_html = _normalize_analysis_consistency(findings, analysis_html)
        full_html = _build_full_html(findings, analysis_html)
        _render_pdf(full_html, output_path)
        log.info("Chinese PDF report generated: %s", filename)
        return output_path, metadata
    except Exception as e:
        log.error("Failed to generate Chinese PDF: %s", e)
        return None, {"authors_full": [], "affiliations": []}
