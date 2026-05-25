#!/usr/bin/env python3
"""Generate manual review reports for two papers."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fitz
from modules.chinese_report_generator import (
    FONT_REGULAR, METHODS_HTML, DISCLAIMER_HTML,
    _compute_dimension_risk, _compute_overall_risk, _apply_data_caps,
    _build_risk_score_html,
)

TODAY = date.today().strftime("%Y年%m月%d日")
OUTPUT_DIR = Path("data/output/manual_review")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _header_html(paper: dict) -> str:
    title = paper.get("title", "未知")
    sjtu_authors = paper.get("sjtu_authors", [])
    sjtu_author_type = paper.get("sjtu_author_type", "")
    sjtu_departments = paper.get("sjtu_departments", [])
    authors_full = paper.get("authors_full", [])
    affiliations = paper.get("affiliations", [])
    journal = paper.get("journal", "未知")
    doi = paper.get("doi", "未知")
    total_images = paper.get("total_images", 0)
    total_refs = paper.get("total_references", 0)

    sjtu_html = ""
    if sjtu_authors:
        sjtu_html += f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>涉及的交大作者：</b>{", ".join(sjtu_authors)}</p>'
    if sjtu_author_type:
        sjtu_html += f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>交大作者类型：</b>{sjtu_author_type}</p>'
    if sjtu_departments:
        sjtu_html += '<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>交大作者单位：</b></p>'
        for dept in sjtu_departments:
            sjtu_html += f'<p style="font-size:8.5pt; color:#555; margin-bottom:1pt; margin-left:12pt;">{dept}</p>'

    all_author_line = ""
    if authors_full:
        all_author_line = f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>全部作者：</b>{", ".join(authors_full)}</p>'

    all_aff_html = ""
    if affiliations:
        all_aff_html = '<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>全部作者单位：</b></p>'
        for aff in affiliations:
            all_aff_html += f'<p style="font-size:8.5pt; color:#555; margin-bottom:1pt; margin-left:12pt;">{aff}</p>'

    stats = []
    if total_images:
        stats.append(f'<b>提取图片数：</b>{total_images}')
    if total_refs:
        stats.append(f'<b>参考文献数：</b>{total_refs}')
    stats_line = f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;">{"　　".join(stats)}</p>' if stats else ""

    return f"""
<h1 style="font-size:18pt; color:#1a1a1a; text-align:center; margin-bottom:4pt;">学术论文预警报告（人工复核版）</h1>
<p style="font-size:9pt; color:#999; text-align:center; margin-bottom:16pt;">Academic Paper Risk Alert Report — Manual Review</p>
<hr style="border:none; border-top:1px solid #ccc; margin:10pt 0;">
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">一、论文基本信息</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>论文标题：</b>{title}</p>
{sjtu_html}
{all_author_line}
{all_aff_html}
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>发表期刊：</b>{journal}</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>DOI：</b>{doi}</p>
{stats_line}
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>分析日期：</b>{TODAY}</p>
"""


def _render_pdf(html: str, output_path: str):
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


# ── Paper 1 ──────────────────────────────────────────────────────────────

PAPER1_ANALYSIS = """
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、检测结果总览</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">
本篇发表于 Nature Communications（DOI: 10.1038/s41467-021-25739-5）的论文，经自动检测系统标记后，
由人工逐项复核数据异常维度的全部HIGH级别告警。复核结论：<b>数据异常维度的全部29项HIGH级别告警
均为假阳性</b>，主要原因是检测系统未能识别生物信息学数据中的自变量列和序号列。
图像重复检测和参考文献核验的结果未做人工复核。
</p>

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">四、人工复核详情</h2>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（一）数据异常复核</p>

<p style="font-size:10pt; font-weight:bold; color:#c00; margin-top:10pt; margin-bottom:4pt;">原检测结果：29项HIGH级别告警</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">等差数列异常 21项、线性依赖 6项、变异系数 1项、等比数列 1项</p>

<p style="font-size:10pt; font-weight:bold; color:#080; margin-top:10pt; margin-bottom:4pt;">复核结论：29项全部为假阳性</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">逐项核查原始Excel数据后，发现以下假阳性来源：</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>1. 样本/实验编号列（约15项）：</b>
MOESM5_ESM.xlsx 中 TableS3 的 'Sample'、'Patient'、'No.' 列，以及 Fig.2 中的 'Experiment' 列等，
其值为 1,2,3,4,5... 形式的序号，被误判为等差数列异常。这些列是样本标识符，不是测量数据。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>2. 基因组坐标列（约6项）：</b>
MOESM5_ESM.xlsx 中 'TSS'（转录起始位点）、'TTS'（转录终止位点）列，
这些是基因组定位坐标，数值特征天然呈现等差或线性模式，不属于可疑异常。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>3. 质谱分馏编号列（约4项）：</b>
'Fraction' 列的值为 1-20 的分馏编号，是质谱实验的固定步骤编号，不是实验测量值。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>4. 线性依赖假阳性（6项）：</b>
由上述序号列和坐标列之间的天然线性关系产生。例如 'Sample' vs 'Fraction' 的线性相关（R²=1.0）
是因为两列都是从1开始的递增整数，并非数据伪造的证据。</p>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（二）图像重复检测（未复核）</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
自动检测发现 26 对可疑图像配对（HIGH 8项、MEDIUM 18项）。本次复核未覆盖图像维度，
建议由领域专家进一步核实。生物学实验中，共用内参对照（如GAPDH、Actin等loading control）
是常见的合理重复来源。
</p>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（三）参考文献核验（未复核）</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
自动检测发现 11 项中等严重程度的标题匹配问题，均为DOI存在但标题相似度偏低。
此类问题通常由期刊格式差异或标题缩写导致，整体风险较低。
</p>

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">五、综合评估</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b>数据异常维度：</b>经人工复核，全部29项HIGH级别告警均为假阳性，实际数据风险等级应为<b style="color:#080;">低风险</b>。
该论文的源数据为生物信息学高通量数据（蛋白质组学、基因表达谱），包含大量样本编号、基因组坐标、
实验分馏编号等非测量数据列，这些列的数学特征被检测系统误判为异常。
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b>图像重复维度：</b>8项HIGH级别告警未做人工复核，需进一步确认。
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b>参考文献维度：</b>风险较低，均为标题匹配度问题，非核心风险。
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">
<b>综合判断：</b>如果图像重复的8项HIGH经核实也是合理解释（如共用内参对照），
则该论文的综合风险等级可降为<b style="color:#080;">低风险</b>。目前因图像维度尚未复核，暂维持系统原始评级。
</p>
"""

# ── Paper 2 ──────────────────────────────────────────────────────────────

PAPER2_ANALYSIS = """
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、检测结果总览</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">
本篇发表于 Nature Communications（DOI: 10.1038/s41467-021-24680-x）的论文，经自动检测系统标记后，
由人工逐项复核数据异常维度的HIGH级别告警。复核结论：<b>数据异常维度的绝大部分HIGH级别告警
为假阳性</b>，主要原因是该论文属于材料力学领域，其数据包含大量仪器预设值（应变x轴、DMA频率扫描）
和归一化对照组。但<b>图像重复维度存在大量高风险告警（102项HIGH），是本论文的主要风险来源</b>，
该维度未做人工复核。另有1项数据线性依赖值得关注。
</p>

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">四、人工复核详情</h2>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（一）数据异常复核</p>

<p style="font-size:10pt; font-weight:bold; color:#c00; margin-top:10pt; margin-bottom:4pt;">原检测结果：92项HIGH级别告警</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">线性依赖 70项、等比数列 12项、等差数列 8项、变异系数 2项</p>

<p style="font-size:10pt; font-weight:bold; color:#e67e00; margin-top:10pt; margin-bottom:4pt;">复核结论：91项为假阳性，1项值得关注</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">逐项核查原始Excel数据后，发现以下假阳性来源：</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>1. 拉伸试验x轴应变列（约5项等差 + 大量线性依赖）：</b>
MOESM11_ESM.xlsx / Fig.2 中 'Fig.2B'、'col_2'、'col_4'、'col_6'、'col_8' 这5列数据均为
拉伸试验机的预设应变值，1725个数据点等间距排列（公差1.928499，最大偏差0.000260）是仪器正常输出。
5列数据完全相同（因为不同试样共用同一应变轴），导致两两之间 R²=1.0 的线性依赖也是正常现象。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>2. 归一化对照组 Sham（4项CV + 等比）：</b>
Fig.3D 和 Fig.3F 中 'Sham' 列的值全部为1.0（n=5），这是生物实验中常见的归一化处理——
将对照组统一设为1.0后其他组相对于对照组表达。CV=0%、恒定几何序列（公比1.0）均是归一化的必然结果，不是数据异常。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>3. DMA 对数频率扫描（约10项等比）：</b>
Fig.S3A 中 '25°C'、'37°C' 等列的数据呈几何序列（公比1.258925 = 10^(1/10)），
这是动态力学分析仪（DMA）的标准对数频率扫描设定——每个十倍频程取10个等对数间距的频率点，
是仪器固有特征，不是数据伪造。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>4. Fig.S2 共享x轴列（线性依赖）：</b>
col_13/col_15/col_17 和 col_20/col_22/col_24 是不同自愈合弹性体（SHE0、SHE0.2等）
应力-应变曲线的共享x轴位置数据，100%重叠和 R²=1.0 均因共用x轴所致。</p>

<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>5. Benford定律偏离（8项MEDIUM）：</b>
材料力学的应力-应变数据、频率扫描数据本身不满足Benford定律的适用前提
（需要跨越多个数量级的自然计数数据），偏离结果不具有参考价值。</p>

<p style="font-size:10pt; font-weight:bold; color:#e67e00; margin-top:10pt; margin-bottom:4pt;">值得关注的发现（1项）：</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>TableS1: SHE1 vs SHE2 线性依赖</b>
（n=578, R²=0.999999, slope=1.000003, intercept=-0.0001）——
两个不同自愈合弹性体配方（SHE1 和 SHE2）的578个测量数据点几乎完全相同（y ≈ x）。
如果这两个配方确实不同，则独立实验不可能产生如此高度一致的结果。建议作者提供解释：
SHE1 和 SHE2 是否为同一样品的重复测量，或是否存在数据录入错误。</p>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（二）图像重复检测（未复核）</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
自动检测发现 134 对可疑图像配对（HIGH 102项、MEDIUM 32项），是本论文最显著的风险信号。
部分配对的相似度极高：
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;">- 配对28（第35页）：PHash距离 1.0，相似度 0.984</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;">- 配对6、配对15（第30页）：PHash距离 3.0，相似度 0.953</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;">- 第30页出现高密度图像重复集中现象</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b style="color:#c00;">建议优先对图像重复维度进行人工核实</b>，这是判定该论文风险等级的关键依据。
</p>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（三）参考文献核验（未复核）</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
自动检测发现 10 项中等严重程度和 2 项低严重程度的参考文献问题，均为DOI存在但标题相似度偏低。整体风险较低。
</p>

<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">五、综合评估</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b>数据异常维度：</b>经人工复核，92项HIGH级别告警中91项为假阳性，
均由材料力学实验的固有数据特征（仪器预设x轴、归一化对照、DMA对数频率）导致。
仅 TableS1 中 SHE1 vs SHE2 的恒等线性依赖值得进一步核实。
实际数据风险应降为<b style="color:#e67e00;">中低风险</b>。
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b>图像重复维度：</b>102项HIGH级别告警是本论文最主要的风险信号，未做人工复核。
考虑到高达134对可疑图像配对且多个配对相似度极高（PHash距离低至1.0），
<b style="color:#c00;">图像维度的风险等级维持高风险</b>。
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;">
<b>参考文献维度：</b>风险较低，均为标题匹配度问题，非核心风险。
</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;">
<b>综合判断：</b>虽然数据异常维度经复核后大幅降级，但图像重复维度的大量HIGH告警
使得该论文<b style="color:#c00;">仍维持高风险评级</b>。建议优先安排图像维度的专家复核。
</p>
"""


def generate_report(report_json_path: str, analysis_html: str, output_filename: str):
    with open(report_json_path) as f:
        findings = json.load(f)

    paper = findings.get("paper", {})
    header = _header_html(paper)
    risk_score = _build_risk_score_html(findings)
    disclaimer = DISCLAIMER_HTML.format(date=TODAY)

    full_html = f"""
<body style="font-family: '{FONT_REGULAR}', sans-serif;">
{header}
{risk_score}
{analysis_html}
{disclaimer}
{METHODS_HTML}
</body>
"""
    output_path = str(OUTPUT_DIR / output_filename)
    _render_pdf(full_html, output_path)
    print(f"Generated: {output_path}")


if __name__ == "__main__":
    generate_report(
        "data/output/0514/10.1038__s41467-021-25739-5/report.json",
        PAPER1_ANALYSIS,
        "10.1038_s41467-021-25739-5_manual_review.pdf",
    )
    generate_report(
        "data/output/0514/10.1038__s41467-021-24680-x/report.json",
        PAPER2_ANALYSIS,
        "10.1038_s41467-021-24680-x_manual_review.pdf",
    )
