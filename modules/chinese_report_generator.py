import json
import logging
import os
import re
import hashlib
import html as html_lib
from datetime import date
from pathlib import Path

import fitz

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from modules.doi_utils import normalize_doi
from utils.llm_client import chat

log = logging.getLogger(__name__)

# fitz 找不到 "Noto Sans CJK SC" 时会 fallback 到 mupdf 内置的 Droid Sans Fallback (含 CJK)
# 但 mupdf 内置 Nimbus Sans (Latin) 不含 Latin Extended (é/ó/í/ú/ñ), 西文重音会被替换为 U+FFFD
# 故把 Charis SIL (覆盖 Latin Extended) 放在 chain 里, 让西文重音字符能正常显示
FONT_REGULAR = '"Noto Sans CJK SC", "Charis SIL", "DejaVu Sans", sans-serif'
FONT_BOLD = '"Noto Sans CJK SC", "Charis SIL", "DejaVu Sans Bold", sans-serif'

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

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">4. 图像拼接检测</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">针对蛋白质印迹（Western blot）、凝胶电泳等条带类图像，初步筛查是否存在拼接迹象，重点关注以下特征：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>泳道分界：</b>泳道之间是否出现不自然的、贯穿上下的清晰竖直分界线。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>背景一致性：</b>图像不同区域的背景灰度与纹理是否连续一致，分界两侧是否出现突变。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>曝光水平：</b>相邻泳道之间的明暗（曝光）水平是否平滑过渡，有无突然跳变。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>分辨率与压缩：</b>图像各区域的清晰度、噪声颗粒与压缩质量是否一致，有无来自不同来源的拼接区域。本项为初步筛查，最终须经人工查看图像确认。</p>
"""

DISCLAIMER_HTML = """
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">六、免责声明</h2>
<p style="font-size:9pt; color:#666; margin-bottom:3pt;">本报告由学术论文预警系统自动生成，所有发现均基于算法分析，仅供参考。</p>
<p style="font-size:9pt; color:#666; margin-bottom:3pt;">部分图像相似性可能来源于合理的实验设计（如共用内参对照、标准分子量标记等），部分数据特征可能有合理的科学解释。</p>
<p style="font-size:9pt; color:#666; margin-bottom:3pt;">最终结论须结合原始实验材料、作者说明及领域专家的人工审核后方可作出。</p>
<p style="font-size:9pt; color:#999; margin-top:10pt;">报告生成日期：{date} | 学术论文预警系统</p>
"""

CN_SYSTEM_PROMPT = """你是学术论文风险检测领域的专家。
你需要完成两项任务：1) 从论文文本中提取作者和机构信息；2) 根据检测结果撰写中文预警报告。
要求：
1. 使用中文撰写，语言专业、严谨
2. 引用具体数据和位置（页码、列名、数值）
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

请严格按照以下格式输出（不要包含```标记）：

===METADATA_JSON===
{{"authors": ["按论文原文抄录的作者1", "按论文原文抄录的作者2", ...], "affiliations": ["1. 按论文原文抄录的机构1", "2. 按论文原文抄录的机构2", ...]}}
===ANALYSIS_HTML===
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、检测结果总览</h2>
[用一段话概述整体风险等级和关键发现。注意：整体风险等级必须与系统计算的一致（{overall_level}），不要自行判断风险等级，不要出现任何分值/分数。然后用列表列出三个维度的问题数量和严重程度分布]

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">四、主要发现详情</h2>
[按图像分析、数据分析、参考文献三个小节详细描述发现]

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


def _generate_analysis_html(findings: dict, first_pages_text: str) -> tuple[dict, str]:
    """Generate analysis HTML and extract metadata in a single LLM call.
    Returns (metadata_dict, analysis_html)."""
    paper = findings.get("paper", {})

    image_risk = _compute_image_risk(findings)
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall_risk = _compute_overall_risk(findings)

    if overall_risk["level"] == "高风险":
        for dim in overall_risk.get("high_dimensions", []):
            if dim == "data" and data_risk["level"] != "高风险":
                data_risk["level"] = "高风险"
                data_risk["color"] = "#c00"
            elif dim == "image" and image_risk["level"] != "高风险":
                image_risk["level"] = "高风险"
                image_risk["color"] = "#c00"

    risk_kwargs = dict(
        overall_level=overall_risk["level"],
        image_level=image_risk["level"],
        data_level=data_risk["level"],
        ref_level=ref_risk["level"],
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
            high_medium = [x for x in items if x.get("severity") in ("high", "medium")]
            if len(high_medium) < len(items):
                findings_copy[key] = high_medium
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

    log.info("Generating combined metadata+analysis (prompt: %d chars)", len(prompt))
    response = chat(prompt, system=CN_SYSTEM_PROMPT, temperature=0.3)
    response = re.sub(r'^```(?:json|html)?\s*', '', response)
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
            import json as _json
            meta_obj = _json.loads(meta_part)
            metadata = {
                "authors_full": meta_obj.get("authors", []),
                "affiliations": meta_obj.get("affiliations", []),
            }
            log.info("Extracted %d authors, %d affiliations from combined call",
                     len(metadata["authors_full"]), len(metadata["affiliations"]))
        except Exception as e:
            log.warning("Failed to parse metadata JSON from combined response: %s", e)

    analysis_html = re.sub(r'<table[^>]*>.*?</table>', '', analysis_html, flags=re.DOTALL)
    analysis_html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', analysis_html)
    # Strip any leftover section markers that the LLM may have echoed from the
    # prompt template (===METADATA_JSON===, ===ANALYSIS_HTML===, and any other
    # custom ===SOMETHING=== delimiters). Without this, those tokens leak into
    # the rendered PDF as visible literal text.
    analysis_html = re.sub(r'={3,}\s*[A-Z_][A-Z0-9_]*\s*={3,}', '', analysis_html)
    log.info("Combined response parsed (analysis: %d chars)", len(analysis_html))
    return metadata, analysis_html


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
    return sum([has_cv, has_arith, has_geo, has_cgd, has_img, has_identity_dep,
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
    cross_page_unique_imgs = len(set(
        k for k in cross_page_img_counts if cross_page_img_counts[k] > 0
    ))
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
        data_high_trigger
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
        # 2026-06-24: 漏判修复 — fcimb 2025.1559182 图像 1 high + 数据 1 high 但被 image<30 拦在门外
        # 双维度各自至少有 1 条 high 实质证据是 hard signal,任一独立维度都不能被另一维的低分拉下来
        or (image_risk["high"] >= 1 and data_risk["high"] >= 1)
    )

    high_dims = set()
    if is_high:
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
    return {
        "score": score, "level": level, "color": color,
        "high": high, "medium": medium, "low": low,
        "high_dimensions": high_dims,
    }


def _build_risk_score_html(findings: dict) -> str:
    duplicate_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    image_risk = _compute_image_risk(findings)
    raw_data = findings.get("data_anomalies", [])
    raw_data_high = sum(1 for x in raw_data if x.get("severity") == "high")
    raw_data_medium = sum(1 for x in raw_data if x.get("severity") == "medium")
    raw_data_low = sum(1 for x in raw_data if x.get("severity") == "low")
    capped_data = _apply_data_caps(raw_data)
    capped_data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)
    if findings.get("_force_risk_level"):
        overall["level"] = findings["_force_risk_level"]
        overall["color"] = "#c00" if overall["level"] == "高风险" else "#080"

    if overall["level"] == "高风险":
        for dim in overall.get("high_dimensions", []):
            if dim == "data" and capped_data_risk["level"] != "高风险":
                capped_data_risk["level"] = "高风险"
                capped_data_risk["color"] = "#c00"
            elif dim == "image" and image_risk["level"] != "高风险":
                image_risk["level"] = "高风险"
                image_risk["color"] = "#c00"

    scored_high = capped_data_risk["high"]
    note_html = ""
    if scored_high != raw_data_high:
        note_html = f"""
<p style="font-size:8.5pt; color:#888; margin-top:2pt; margin-bottom:4pt;">
注：原始{raw_data_high}项HIGH告警经评分规则调整（含线性依赖重分类及同类型封顶），风险等级按{scored_high}项HIGH计算。
</p>"""

    # Splice pre-screen line — rendered deterministically (not via the LLM) so the
    # risk overview has a stable set of dimensions. Shows counts only (no score);
    # >=2 suspects is 高风险 (matches the is_high trigger), 0/1 suspects stay 低风险.
    splice = findings.get("image_splicing", [])
    n = len(splice)
    sp_level = "高风险" if n >= 2 else "低风险"
    sp_color = "#c00" if n >= 2 else "#080"
    if splice:
        sp_summary = f"检出 {n} 处疑似拼接图像（达到2处升级为高风险，须经人工查看标注图确认）"
        items = "".join(
            f'<li>第{s.get("page", "?")}页：{s.get("details", "疑似拼接")}</li>'
            for s in splice[:15]
        )
        more = f'<li>……另有 {n - 15} 处（略）</li>' if n > 15 else ""
        splice_items_html = f'\n<ul style="font-size:9pt; color:#555; margin:2pt 0 6pt 0;">{items}{more}</ul>'
    else:
        sp_summary = "未检出疑似拼接图像"
        splice_items_html = ""
    splice_html = f"""
<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">
<b>图像拼接检测：</b><span style="color:{sp_color}; font-weight:bold;">{sp_level}</span>
　　{sp_summary}
</p>
{splice_items_html}"""

    return f"""
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">二、初筛风险概览</h2>

<p style="font-size:9pt; color:#666; margin-bottom:8pt;">以下为代码确定性规则的初筛结果，最终以 AI 复核结论为准。</p>

<p style="font-size:16pt; font-weight:bold; color:{overall['color']}; text-align:center; margin:14pt 0 6pt 0;">
综合风险等级：{overall['level']}
</p>

<hr style="border:none; border-top:1px solid #eee; margin:8pt 0;">

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:6pt;">各维度风险评估：</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">
<b>图像重复检测：</b><span style="color:{duplicate_risk['color']}; font-weight:bold;">{duplicate_risk['level']}</span>
　　高风险 {duplicate_risk['high']}项　中等 {duplicate_risk['medium']}项　低风险 {duplicate_risk['low']}项
</p>
{splice_html}

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
<h1 style="font-size:18pt; color:#1a1a1a; text-align:center; margin-bottom:4pt;">学术论文预警报告</h1>
<p style="font-size:9pt; color:#999; text-align:center; margin-bottom:16pt;">Academic Paper Risk Alert Report</p>

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

    full_html = f"""
<body style='font-family: {FONT_REGULAR};'>
{header_html}
{risk_score_html}
{analysis_html}
{disclaimer}
{METHODS_HTML}
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
    max_bytes = 180
    suffix = ".pdf"
    doi_part = _limit_text_bytes(doi_to_slug(doi) or "unknown", 120)
    digest_src = f"{doi or ''}\n{title or ''}".encode("utf-8", errors="ignore")
    digest = hashlib.sha1(digest_src).hexdigest()[:8]
    title_part = _sanitize_filename_part(title, max_chars=72)
    fixed_bytes = len(f"{doi_part}_{digest}{suffix}".encode("utf-8"))
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
        metadata = _coalesce_metadata(paper, metadata)
        findings["paper"]["authors_full"] = metadata.get("authors_full", [])
        findings["paper"]["affiliations"] = metadata.get("affiliations", [])
        full_html = _build_full_html(findings, analysis_html)
        _render_pdf(full_html, output_path)
        log.info("Chinese PDF report generated: %s", filename)
        return output_path, metadata
    except Exception as e:
        log.error("Failed to generate Chinese PDF: %s", e)
        return None, {"authors_full": [], "affiliations": []}
