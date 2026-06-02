import json
import logging
import os
import re
from datetime import date
from pathlib import Path

import fitz

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from utils.llm_client import chat

log = logging.getLogger(__name__)

FONT_REGULAR = "Noto Sans CJK SC"
FONT_BOLD = "Noto Sans CJK SC"

METHODS_HTML = """
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">附录：检测方法说明</h2>

<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">本报告采用以下自动化检测方法对论文进行分析：</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">1. 图像重复检测</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">本系统通过三个步骤逐层筛查论文中的图像是否存在重复或异常：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>第一步"指纹比对"：</b>将每张图像压缩为一个简短的数字指纹，快速比较所有图像对之间的相似程度，初步筛选出可能重复的图像。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>第二步"局部特征匹配"：</b>对初筛通过的图像，提取图像中的关键特征点（如边缘、纹理等），逐一比对两张图像中是否存在相同的局部区域。即使图像被缩放、旋转或裁剪，该方法仍能识别出重复片段。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>第三步"精确验证"：</b>对第二步标记的可疑区域，在多种缩放比例下进行逐像素比对确认，最终判定是否存在真实的图像重复。</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">2. 数据异常检测</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">本系统对论文附带的源数据文件（Excel/CSV）进行以下统计检验：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>变异系数检验：</b>计算每组数据的离散程度。真实的实验测量数据天然存在波动，若一组重复测量值完全相同（变异系数为0）或极度接近，则高度可疑。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>等差/等比数列检验：</b>检查数据值是否排列成完美的数学数列。真实实验数据不可能呈现完美的等差或等比规律，出现此类模式提示数据可能非自然生成。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>本福特定律检验：</b>自然产生的数值数据中，首位数字"1"出现的频率远高于"9"。人为构造的数据往往不符合这一规律，通过统计检验可以识别出偏离自然分布的数据集。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>跨组重复检验：</b>比较不同实验组之间的数值重叠比例。若两组本应独立的实验数据中有超过50%的数值完全相同，则提示数据可能被复制或重复使用。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>跨列线性依赖检验：</b>对同一数据表中的各组数据列进行两两线性回归分析，检测是否存在近乎完美的线性关系（y = a×x + b, R²≥0.9999）。若两条本应独立采集的实验曲线之间R²接近1.0，尤其当斜率恰好为整数倍（如2、5、10）时，强烈提示其中一条曲线可能是通过对另一条曲线进行数学缩放/平移生成的，而非真实独立测量所得。</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>数值合理性检验：</b>对测量数据的末位数字分布、标准差列的取值规律等进行核查。真实测量数据的末位数字通常较为均匀，标准差也极少全为整数或完全相同的精度；若出现明显偏离，提示相关数值可能并非自然测量所得。</p>

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
5. 作者姓名和机构名称必须保持论文原文语言和拼写，不要翻译、意译或中英混写
6. 不要使用emoji表情符号"""

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
            slope = issue.get("details", {}).get("slope", 0)
            intercept = issue.get("details", {}).get("intercept", 0)
            if abs(slope - 1.0) < 0.01 and abs(intercept) < 0.01:
                return "high"
            return "medium"
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


def _compute_image_risk(findings: dict) -> dict:
    """Combined image-dimension risk for DB/API summaries.

    Image duplicates still carry their normal score. Splice findings are conservative
    medium findings, but >=2 corroborated splice suspects is an explicit high-risk gate.
    """
    issues = list(findings.get("image_duplicates", [])) + list(findings.get("image_splicing", []))
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
    has_img = any(x.get("severity") == "high" for x in img)
    has_identity_dep = any(
        x.get("test") == "linear_dependency"
        and x.get("details", {}).get("r_squared", 0) > 0.99999
        and abs(x.get("details", {}).get("slope", 0) - 1.0) < 0.01
        and abs(x.get("details", {}).get("intercept", 0)) < 0.01
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


def _compute_overall_risk(findings: dict) -> dict:
    image_risk = _compute_image_risk(findings)
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))

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

    has_img_issues = len(findings.get("image_duplicates", [])) > 0
    has_data_issues = len(findings.get("data_anomalies", [])) >= 5
    if has_img_issues and has_data_issues and score < 56:
        score = 56

    from collections import Counter
    cross_page_img_counts = Counter()
    cross_page_page_pairs = set()
    for d in findings.get("image_duplicates", []):
        if not isinstance(d, dict):
            continue
        if d.get("page_a") == d.get("page_b"):
            continue
        pa, pb = d.get("page_a", 0), d.get("page_b", 0)
        if pa == 0 or pb == 0:
            continue
        a, b = d.get("image_a", ""), d.get("image_b", "")
        cross_page_img_counts[a] += 1
        cross_page_img_counts[b] += 1
        cross_page_page_pairs.add((min(pa, pb), max(pa, pb)))
    cross_page_imgs = 0
    if len(cross_page_page_pairs) <= 8:
        seen_pairs = set()
        for d in findings.get("image_duplicates", []):
            if not isinstance(d, dict):
                continue
            if d.get("page_a") == d.get("page_b"):
                continue
            pa, pb = d.get("page_a", 0), d.get("page_b", 0)
            if pa == 0 or pb == 0:
                continue
            a, b = d.get("image_a", ""), d.get("image_b", "")
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

    ref_issues = findings.get("reference_issues", [])
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
        and d.get("severity") == "high" and d.get("page_a") != d.get("page_b")
        and d.get("page_a", 1) > 0 and d.get("page_b", 1) > 0
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

    is_high = (
        data_risk["score"] >= 30
        or cross_page_imgs >= 10
        or full_dup_trigger
        or ref_fabrication_trigger
        or (fraud_count >= 4 and 0 < image_risk["score"] < 80)
        or (score >= 60 and data_risk["score"] >= 60)
        or (image_risk["score"] >= 40 and data_risk["score"] >= 10)
        or splice_trigger
    )

    high_dims = set()
    if is_high:
        if data_risk["score"] >= 30:
            high_dims.add("data")
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
        if image_risk["score"] >= 40 and data_risk["score"] >= 10:
            high_dims.add("image")
        if splice_trigger:
            high_dims.add("image")

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
<body style="font-family: '{FONT_REGULAR}', sans-serif;">
{header_html}
{risk_score_html}
{analysis_html}
{disclaimer}
{METHODS_HTML}
</body>
"""
    return full_html


def _soften_risk_wording(html: str) -> str:
    """Display-only wording: render "高风险" / "建议高风险" as "疑似高风险" in the final
    report HTML. This runs at the PDF render boundary ONLY — it never touches the risk
    *values* used for logic, comparisons, DB writes or queries (those stay "高风险").
    The negative lookbehind makes it idempotent (won't produce "疑似疑似高风险")."""
    return re.sub(r"(?<!疑似)(?:建议)?高风险", "疑似高风险", html)


def _render_pdf(html: str, output_path: str):
    html = _soften_risk_wording(html)
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


def _make_filename(doi: str, title: str) -> str:
    doi_clean = doi.replace("https://doi.org/", "").replace("/", "_")
    title_30 = title[:30].strip()
    safe_chars = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', f"{doi_clean}_{title_30}")
    return safe_chars + ".pdf"


def doi_to_slug(doi: str) -> str:
    """Filesystem-safe slug for a DOI, shared by the 初审 and 复核 report paths so a
    stored URL always matches the file actually written. Strips the doi.org prefix,
    maps '/' to '_', and removes the same unsafe characters as _make_filename (a plain
    doi.replace('/','_') diverged for DOIs containing < > : " \\ | ? *)."""
    doi_clean = (doi or "").replace("https://doi.org/", "").replace("/", "_")
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', doi_clean)


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
    paper = findings.get("paper", {})
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
