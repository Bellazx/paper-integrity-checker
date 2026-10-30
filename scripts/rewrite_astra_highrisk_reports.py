#!/usr/bin/env python3
"""Rewrite the ten scholar rereview PDFs from the Astra adjudication.

This is a report-only operation: it does not update either database table.
Each report embeds the evidence images that support its conclusion.
"""
from __future__ import annotations

import json
import html
import hashlib
import re
import shutil
import sys
import zipfile
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

import fitz

BASE = Path("/home/sjtu/paper-integrity-checker")
sys.path.insert(0, str(BASE))
from api.services.review_validation import renderer_module
from modules.review_public_wording import public_text
from PIL import Image, ImageDraw, ImageFont
ROUND = BASE / "data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1"
EVIDENCE = ROUND / "all_high_risk_reports_20261022/evidence_screenshots"
OUT = BASE / "data/output/yujing_scholar/review_v2"
ARCHIVE = ROUND / "all_high_risk_reports_astra_20261022"
GROUPED = ROUND / "reports_by_scholar_20261022"
FONT = Path("/home/library/.local/share/fonts/paper-pecker/NotoSansCJK-Regular.ttc")
FONT_BOLD = Path("/home/library/.local/share/fonts/paper-pecker/NotoSansCJK-Bold.ttc")
FIXED_EVIDENCE = ROUND / 'template_evidence_20261022'

PAPERS = {
    "10.1016/j.energy.2026.140018": {
        "scholar": "胡高伟", "title": "Experimental investigation on hydrate dissociation enhanced by deep gas geothermal energy transport via water circulation",
        "verdict": "高风险（图文及统计值需更正）", "risk": True,
        "summary": "（1）热效率与图中热量不一致。第6页Fig.6(a)中，Case 1的放热量约23.1 kJ、吸热量约35.8 kJ，相除约为64.4%，但Fig.6(e)相应点为48.63%；Case 4、5也有类似差异。需核对并更正柱图或效率数值。读图值为近似数，具体错误环节需结合绘图数据确认。\n（2）图中数字标签写错。第5页Fig.4(e)中，Case 3在1小时处标为58.98%，但圆点位于约54%；同页Fig.4(b)的放热量约32.6 kJ除以吸热量约60.4 kJ，也约为54%。标签与点位、热量计算均不符，应更正该标签；曲线实际持续上升。\n（3）统计值与补充数据不符。第7页Table 1的降压速率组CV（用于衡量不同工况结果的离散程度）写为0.057；补充表S2的Case 1、3中，t90分别为6.59、7.26小时，按其倒数的总体标准差除以均值，结果约为0.0484。需核对并更正统计值；复算后各因素影响大小的排序不变。",
        "evidence": [
            ("10.1016_j.energy.2026.140018_1_ev1_fig6.png", "第6页Fig.6(a)与Fig.6(e)：以Case 1为例，按热量轴精细读数复算效率约64.4%，与效率曲线相应点48.63%不一致。需计入热量轴截断的部分，不能直接拿柱段长度作比值。"),
            ("10.1016_j.energy.2026.140018_2_ev2_fig4e.png", "第5页Fig.4(e)：橙线第二点的标签为58.98%，点位却约在54%。需要更正的是数字标签，曲线本身没有回落。"),
            ("10.1016_j.energy.2026.140018_3_ev3_table1.png", "第7页Table 1：第二行CV为0.057，按补充表S2复算约0.048。CV用于比较不同工况结果的离散程度，不能当作重复实验显著性检验。"),
        ],
    },
    "10.1016/j.fuel.2021.121866": {
        "scholar": "胡高伟", "title": "Molecular study on the behavior of methane hydrate decomposition induced by ions electrophoresis",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）盐浓度与分子数量不一致。第2页方法写每个盐水池含1230个水分子、6个钠离子和6个氯离子，按质量分数计算约为1.56%，与标注的3.1%不符。需澄清是浓度标注、分子数量描述还是实际模拟设置有误；目前不能确定模拟结果是否受到影响。",
        "ref_review": "（2）参考文献著录需更正。第7页文献[40]的卷页等字段不符；文献[41]的DOI指向新生儿神经细胞研究，与所列食品科学百科题名不符。需更正对应信息，此项不作为高风险依据。",
        "evidence_dimensions": {"10.1016_j.fuel.2021.121866_1_ev_ref41.png": "ref"},
        "evidence": [
            ("10.1016_j.fuel.2021.121866_3_ev_method.png", "第2页方法：每池1230个水分子、6对NaCl，盐质量分数为6×58.44/(1230×18.015+6×58.44)×100%，约1.56%；合并两个同组成水池不会使浓度翻倍。"),
            ("10.1016_j.fuel.2021.121866_1_ev_ref41.png", "参考文献：[40]卷页等著录字段、[41]的DOI与题名对应关系需更正，不作为高风险依据。"),
        ],
    },
    "10.1016/j.oceaneng.2025.123854": {
        "scholar": "胡高伟", "title": "Method for determination of hydrate reservoir failure based on energy evolution",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）能量曲线与公开计算方法对不上。以围压3 MPa、轴向应变20%为例，第13页Fig.15(a)中，饱和度30%、20%、10%的总能量约为83、69、55 kJ/m³；按第12页公式（7）、Fig.5(a)和Fig.9计入轴向与侧向做功，复算约为908、835、804 kJ/m³。读图值为近似数，差异需结合原始曲线和单位换算解释。\n（2）刚度参数的取值需说明。第12页称用约1.25%应变处的割线模量；在围压3 MPa下，20%、10%饱和度按此复算约为144、108 MPa，但Table 3为74.548、36.801 MPa。需明确取点和E50计算方法。\n（3）公式（11）多写一项。公式（10）代入两个横向应力相等后，应有2(1−μ)σ3²，公式（11）又额外保留2σ3²。可确认公式不一致，但不能确定实际计算也用了错式；固定围压和模量下，该常量偏移在扣除初始值时可能抵消。",
        "evidence": [
            ("10.1016_j.oceaneng.2025.123854_1_energy_unit_10x.png", "图中807/668/549仅是偏应力曲线面积的近似计算，不能直接当作公式（7）的完整总能量。计入侧向做功后应按正文约908/835/804核对。图中“遗漏10倍换算”的归因和恒定应力下界论证均不能据此确认。"),
            ("10.1016_j.oceaneng.2025.123854_2_table3_eq10_eq11.png", "此图用于定位Table 3及公式（10）、（11）。右侧柱图把不同应变状态的能量放在一起，不能用作直接比较或风险依据；刚度参数请按正文给定的1.25%应变口径核对。"),
            ("10.1016_j.oceaneng.2025.123854_3_eq10_eq11_source.png", "第12页公式原文：2σ3²与−2μσ3²合并后已成为2(1−μ)σ3²，因此不能再加一次2σ3²。实际程序是否使用印刷错式，需作者提供计算文件确认。"),
        ],
    },
    "10.19657/j.geoscience.1000－8527.2021.173": {
        "scholar": "胡高伟", "title": "海域天然气水合物试采目标优选定量评价方法初探",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）按公开权重算不出文中的评分。PDF第9页表8、表9规定地质和工程因素各占50%；结合第8页评价矩阵，XX01—XX04的得分约为0.353、0.383、0.312、0.324，但第9页“（4）综合评价”写为0.364、0.435、0.353、0.385。\n（2）计算采用的权重需澄清。若改用地质25%、工程75%，复算约为0.3640、0.4347、0.3535、0.3854，更接近论文数值，但不能据此确认作者实际使用了该比例。XX02在两种权重下均为首选站位。",
        "evidence": [
            ("10.19657_j.geoscience.1000－8527.2021.173_1_10.19657_j.geoscience.1000－8527.2021.173_table8_mainfactor_weights.png", "PDF第9页表8：工程因素和地质因素的权重都写为0.5，即各占50%。"),
            ("10.19657_j.geoscience.1000－8527.2021.173_2_10.19657_j.geoscience.1000－8527.2021.173_table9_final_weights.png", "PDF第9页表9：最终权重等于次因素权重乘0.5；例如渗透率0.3961×0.5＝0.19805。将最终权重与第8页各站位评价矩阵相乘求和，即可复算评分。"),
            ("10.19657_j.geoscience.1000－8527.2021.173_3_10.19657_j.geoscience.1000－8527.2021.173_B_values_p9.png", "PDF第9页“（4）综合评价”：论文依次给出0.364、0.435、0.353、0.385；按公开50%:50%权重复算约为0.353、0.383、0.312、0.324。XX02在两组结果中均最高。"),
        ],
    },
    "10.31035/cg2022015": {
        "scholar": "胡高伟", "title": "Experimental investigation of hydrate formation in water-dominated pipeline and its influential factors",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "evidence_starts_on_new_page": True,
        "summary": "（1）图12的选点和适用范围需说明。PDF第6页表1中，浓度同为4.7‰时，Case 9—12的转速为400、600、750、1000转/分钟，压差依次为0.801、0.911、1.007、1.114 MPa；但第11页图12在横坐标4.7附近，随转速增加颜色由黄变绿，表示压差下降。Case 10—12标有ME，属于已形成、分解过水合物的重复实验，与Case 9条件不同，不能直接认定同组数据矛盾。需说明图12选了哪些实验、下降趋势适用于哪类实验。\n（2）浓度单位不一致。图12横轴写“%”，正文和表1写“‰”，同一数值代表的浓度相差10倍，需统一。\n（3）Case 2、Case 3的气体浓度按论文公式复算与表值不一致，需提供计算过程。",
        "evidence": [
            ("10.31035_cg2022015_1_EVID_fig12_marked.png", "Fig.12（PDF第11页、印刷页319）：横坐标4.7附近的颜色随转速增加由黄变绿，表示压差下降；需明确绘图使用的实验组。"),
            ("10.31035_cg2022015_2_EVID_table1.png", "Table 1（PDF第6页、印刷页314）：Case 9—12的压差依次升高，但Case 10—12标有ME，与Case 9实验历史不同，不能直接当作相同条件比较。"),
        ],
    },
    "10.3390/jmse12122239": {
        "scholar": "胡高伟", "title": "A Comparative Study on Acoustic Characteristics of Methane and Tetrahydrofuran Hydrate-Bearing Sediments",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）同一实验的图表数值对不上。PDF第9页Fig.5b中，30%饱和度的甲烷横波实验点约为1619 m/s，但第7页Table 1为1066.2 m/s，相差约553 m/s。Fig.5a的纵波实验点也有多处差异，而Fig.7与表1基本一致，需核实Fig.5的数据来源。\n（2）文字概括与表格不一致。第11页第4.3节称80%—100%饱和度时，THF的纵波、横波速度均高于甲烷；但表1在80%时，甲烷两项为3452.1、1716.6 m/s，均高于THF的3163.8、1691.6 m/s。需按实际数据修正适用区间。",
        "evidence": [
            ("10.3390_jmse12122239_1_evidence_fig5a_vs_table1.png", "Fig.5a：甲烷纵波实验点与Table 1不一致。"),
            ("10.3390_jmse12122239_2_evidence_fig5b_vs_table1.png", "Fig.5b：甲烷横波实验点与Table 1不一致。"),
            ("10.3390_jmse12122239_3_evidence_fig7a_vs_table1.png", "Fig.7a：实验点与Table 1基本一致，用于对照定位Fig.5的数据来源问题。"),
            ("10.3390_jmse12122239_4_evidence_table1_vs_text80.png", "Table 1与正文80%–100%反超区间的对照。"),
        ],
    },
    "10.1038/s41380-022-01489-8": {
        "scholar": "樊令仲", "title": "Uncovering the genetic profiles underlying the intrinsic organization of the human cerebellum",
        "verdict": "高风险（图文摘要数字需更正）", "risk": True,
        "allow_removed_evidence": True,
        "summary": "（1）图文摘要把3568写成了3586。Visual（视觉）皮层网络的特异基因数在图中标为3586，但源数据Sheet7_Gene_cort_Network的A2单元格为3568，A3:A3570也正好列出3568个不重复基因；按Sheet6中校正后p值小于0.05、logFC大于0的条件重新筛选，仍得到3568。因此应将图中3586更正为3568。可确认的是图中数字与源数据不一致，不能由此认定基因列表或统计结果被篡改。核验依据为已取得的图文摘要及公开源数据。",
        "evidence": [
            ("10.1038_s41380-022-01489-8_1_ga_visual_count_3586_vs_3568.png", "图文摘要的Visual计数为3586；Sheet7_Gene_cort_Network列表共3568个基因，按Sheet6的显著性及表达方向条件重算也是3568。此处为数字标注问题。"),
        ],
    },
    "10.1038/s41467-025-62812-9": {
        "scholar": "樊令仲", "title": "Mapping the coupling between tract reachability and cortical geometry of the human brain",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）标注人数与可复现统计结果的人数不一致。Fig.6B及方法写661人；源数据Figure6B中去掉15行异常记录、按646人复算，才能逐项得到图中18个配对t值，使用全部661行则不能。需披露实际分析人数、排除标准和对应记录。\n（2）这15行记录的来源需核查。其中14行在多个脑连接指标上出现跨随访时点逐位相同的数值；另1行的18个值约为105—310，而其余646人同类指标最大约0.6。需明确是导出、单位、数据录入还是其他处理环节造成，不能仅凭异常数值认定造假。\n（3）另有统计口径需说明。Fig.3D的HCP认知预测相关系数为0.24，补充数据1的S1表分别列左、右半球约0.362、0.371。可能涉及合并模型与分半球模型的区别，需明确对应关系，不作为独立高风险依据。",
        "evidence": [
            ("10.1038_s41467-025-62812-9_1_10.1038_s41467-025-62812-9_fig6b_15rows_duplicate_waves.png", "源数据Figure6B：BL、FU2、FU3是三个随访时点。14行至少在三个脑连接指标上有跨时点重复值；另1行存在明显不同的数值量级。截图中约3.6—421的范围为粗略展示，不代表逐单元格的完整最小值。"),
            ("10.1038_s41467-025-62812-9_2_10.1038_s41467-025-62812-9_fig6b_t_reproduction_n646_vs_n661.png", "图中按绝对值展示配对t统计量：中间n=646的18项与论文一致，右侧n=661不能复现。t值正负取决于前后时点相减的顺序；需核对的是分析样本及数值大小。"),
            ("10.1038_s41467-025-62812-9_3_10.1038_s41467-025-62812-9_fig3D_r_values.png", "Fig.3D的HCP认知相关系数为0.24；S1表按左右半球分别报告约0.362、0.371。模型范围可能不同，未经对应关系确认不能视作同一统计量矛盾。"),
        ],
    },
    "10.1016/j.cmet.2026.06.020": {
        "scholar": "王丹丹", "title": "Brown fat protects against hepatic oxidative stress by remodeling the circulating metabolome",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）不同剂量的整组数据几乎能按固定倍数互相换算。Fig.5C对应源数据mmc7.xlsx的Fig5C表中，0.70 μM的4个数分别除以0.85 μM的对应数，均约为1.014799155；0.75与0.90 μM的4个对应比值也均约为1.009513742。两对数据的比例一致到表格保存精度，需解释这些数值如何得到。\n（2）需核对四列重复值与测量记录的对应关系。不同剂量共用同一动物来源材料可以是合理设计，但不能单凭这一点解释如此精确的整组比例。建议提供原始读数、样本对应表及换算流程。目前不能认定样本数虚报；按公开数据拟合的EC50约0.51 μM，与论文接近，也不能据此否定效力结论。",
        "evidence": [
            ("10.1016_j.cmet.2026.06.020_1_fig5C_panel.png", "正文第9页Fig.5C：EC50标为0.51 μM，公开数据可近似复现。需核查的是对应源表中的整组比例关系，不能仅凭此曲线判定数据重复或效力结论错误。"),
            ("10.1016_j.cmet.2026.06.020_2_fig5C_sourcedata.png", "mmc7.xlsx的Fig5C表A16:E20：红色两行逐列相除约1.014799155，蓝色两行约1.009513742，各自4个比值几乎相同。表中是公开处理后数据，不能直接等同于仪器原始读数。"),
        ],
    },
    "10.1093/jmcb/mjac004": {
        "scholar": "王丹丹", "title": "HDL quality features revealed by proteome–lipidome connectivity are associated with atherosclerotic disease",
        "verdict": "保留高风险（需作者解释）", "risk": True,
        "summary": "（1）第三只兔的数据在哪里，需要说明。正文第5页Fig.3图注及补充Fig.S6写明，图中的点是3个个体的平均值；但附表S10中，饮食干预前的样本编号只见T1、T2，未见第三个编号。需提供样本编号与动物的对应表，才能核实平均值用了哪些记录；目前不能据此认定少做了一只兔的实验。\n（2）附表多出一组“2周”数据，用途没有交代。正文第3页及Fig.3介绍的是干预前、干预后4周、8周和14周，附表S10却还列有2W（2周）记录。需说明这组数据是否属于同一实验、是否参与论文的计算。\n（3）兔实验的蛋白数量暂时对不上。正文第3页写检测并定量了462个蛋白；附表S10去掉软件标记的反向匹配、污染物及仅凭修饰位点鉴定的条目，再保留总信号强度大于0的记录，可数到419个，比论文少43个。需说明462个的筛选方法和包含哪些实验批次。这是按当前方法无法复现数量，尚不能认定作者虚报。",
        "evidence": [
            ("10.1093_jmcb_mjac004_2_data_rabbit_sample_structure.png", "上方为正文第5页Fig.3图注截图，下方为附表S10列名摘录。图注写3个个体，干预前的编号只见T1、T2；附表另有2W记录。编号如何对应动物、2周数据用于哪里，需要作者说明，不能把表格列数直接当作动物数。"),
            ("10.1093_jmcb_mjac004_3_data_protein_peptide_counts.png", "上方为正文第3页截图，下方为附表复算对照表，由复核人员制作。兔实验按所列筛选方法为419个，与文中462个差43个；两个患者样本组可分别复现492、554个，不列为数量异常。差异是否来自筛选方法或实验批次，尚需作者确认。"),
        ],
    },
}

REVIEW_LOCATIONS = {
    '10.1016/j.energy.2026.140018': '第5页Fig.4(e)、第6页Fig.6(a)、第7页Table 1及补充表S2',
    '10.1016/j.fuel.2021.121866': '第2页模拟体系方法段',
    '10.1016/j.oceaneng.2025.123854': 'Fig.5(a)、Fig.9、第12页Table 3及公式（7）（10）（11）、第13页Fig.15(a)',
    '10.19657/j.geoscience.1000－8527.2021.173': 'PDF第8页评价矩阵、第9页表8、表9及“（4）综合评价”',
    '10.31035/cg2022015': 'PDF第6页Table 1、第11页Fig.12',
    '10.3390/jmse12122239': 'PDF第7页Table 1、第9页Fig.5、Fig.7及第11页第4.3节',
    '10.1038/s41380-022-01489-8': '图文摘要Visual计数及源数据Sheet6、Sheet7',
    '10.1038/s41467-025-62812-9': 'Fig.6B及对应源数据Figure6B、Fig.3D及补充数据1的S1表',
    '10.1016/j.cmet.2026.06.020': '正文第9页Fig.5C及mmc7.xlsx的Fig5C工作表',
    '10.1093/jmcb/mjac004': '正文Fig.3、补充Fig.S6、附表S10及兔模型方法描述',
}


def _plain(text: str) -> str:
    """Use unambiguous scientific notation even with limited CJK fonts."""
    return html.unescape(text).replace("Na⁺", "Na+").replace("Cl⁻", "Cl-").replace("⁻¹⁰", "^(-10)").replace("σ3²", "σ3^2")


def _paper_metadata(doi: str, item: dict) -> dict:
    rows = json.loads((ROUND / 'papers.json').read_text(encoding='utf-8'))
    row = next(row for row in rows if row['doi'] == doi)
    paper = json.loads(Path(row['report_json']).read_text(encoding='utf-8'))['paper']
    paper = renderer_module()._sanitize_paper_dict_for_render(paper)
    paper['title'] = _plain(paper.get('title') or item['title']).replace(' | Molecular Psychiatry', '')
    paper['affiliations'] = [a for a in paper.get('affiliations', []) if '未包含' not in a]
    if doi == '10.3390/jmse12122239':
        paper['journal'] = 'Journal of Marine Science and Engineering, 12 (2024), 2239'
    if doi == '10.19657/j.geoscience.1000－8527.2021.173':
        paper['journal'] = '现代地质（Geoscience）'
    if doi == '10.31035/cg2022015':
        paper['title'] = item['title']
        paper['journal'] = 'China Geology'
        # The supplied PDF has a publisher cover; metadata from that page is
        # not a reliable author list. Omit rather than repeat unrelated names.
        paper['authors_full'] = []
        paper['affiliations'] = []
        paper['total_images'] = 0
        paper['total_references'] = 0
    item['title'] = paper['title']
    return paper


def _source_crop(paper: dict, page: int, rect: tuple, target: Path) -> None:
    with fitz.open(paper['filepath']) as doc:
        doc[page-1].get_pixmap(matrix=fitz.Matrix(4, 4), clip=fitz.Rect(rect), alpha=False).save(target)


def _fig5c_source_table(target: Path) -> None:
    from openpyxl import load_workbook
    workbook_path = BASE / 'data/input/scholar/王丹丹/10.1016_j.cmet.2026.06.020/source data/1-s2.0-S1550413126002469-mmc7.xlsx'
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        sheet = workbook['Fig5C']
        rows = [[sheet.cell(r, c).value for c in range(1, 6)] for r in range(16, 21)]
    finally:
        workbook.close()
    font = ImageFont.truetype(str(FONT), 24)
    canvas = Image.new('RGB', (1400, 450), 'white')
    draw = ImageDraw.Draw(canvas)
    draw.text((25, 18), 'Fig5C | A16:E20 | Source data excerpt', font=font, fill='black')
    widths = [100, 200, 260, 260, 260, 260]
    values = [['Row', 'Dose (uM)', 'Replicate 1', 'Replicate 2', 'Replicate 3', 'Replicate 4']]
    values += [[str(r+16), f'{row[0]:.2f}'] + [f'{v:.8f}' for v in row[1:]] for r, row in enumerate(rows)]
    for i, row in enumerate(values):
        y = 75 + 55*i
        x = 25
        fill = '#eeeeee' if i == 0 else ('#ffe7e7' if i in (1, 4) else '#e6efff' if i in (2, 5) else '#ffffff')
        for width, value in zip(widths, row):
            draw.rectangle((x, y, x+width, y+55), fill=fill, outline='#bbbbbb')
            draw.text((x+8, y+12), value, font=font, fill='#222222')
            x += width
    canvas.save(target)


def prepare_evidence(doi: str, item: dict, paper: dict, *, reuse_existing: bool = True) -> list[dict]:
    FIXED_EVIDENCE.mkdir(parents=True, exist_ok=True)
    prepared = []
    for index, (filename, caption) in enumerate(item['evidence']):
        target = FIXED_EVIDENCE / filename
        if reuse_existing:
            # Wording-only revisions must keep the reviewed screenshots byte
            # for byte, including the corrected Fuel and China Geology crops.
            if not target.is_file():
                raise FileNotFoundError(target)
            dimension = item.get('evidence_dimensions', {}).get(filename, 'data')
            prepared.append({'dimension': dimension, 'path': target, 'caption': _plain(caption)})
            continue
        source = EVIDENCE / filename
        if not source.is_file():
            raise FileNotFoundError(source)
        if doi == '10.1016/j.fuel.2021.121866':
            if filename.endswith('_ev_method.png'):
                _source_crop(paper, 2, (35, 289, 294, 380), target)
                caption = ('正文第2页方法原文：每池1230个水分子、6个钠离子（Na+）和6个氯离子（Cl-），标为3.1 wt%。'
                           '按质量分数复算：6×58.44 / (1230×18.015 + 6×58.44) ×100% = 1.56%。')
            elif filename.endswith('_ev_ref41.png'):
                _source_crop(paper, 7, (304, 244, 562, 307), target)
                caption = '正文第7页参考文献[40]—[42]原文：[40]卷页等著录字段不符；[41]的DOI指向新生儿神经细胞研究，与所列食品科学百科题名不符。'
            else:
                raise ValueError(f'Unexpected Fuel evidence: {filename}')
        elif doi == '10.31035/cg2022015' and index == 0:
            _source_crop(paper, 11, (315, 431, 557, 615), target)
            caption = '图12原图（PDF第11页、印刷页319）：横轴找4.7，向上看转速从约400增加到1000转/分钟，颜色由黄变绿；右侧色标显示压差下降。压差是形成压力减去平衡压力。横轴单位写为%，表1写为‰，需更正。'
        elif doi == '10.31035/cg2022015' and index == 1:
            _source_crop(paper, 6, (30, 519, 550, 771), target)
            caption = '表1原文（PDF第6页、印刷页314）：看Case 9—12的Gas concentration、Circulation velocity、Delta pressure和Notes列。浓度均为4.7‰，转速400/600/750/1000对应压差0.801/0.911/1.007/1.114 MPa。后三组标有ME，表示经历过水合物形成、分解的重复实验；因此不能据此直接判定图12画错。表中7组未标ME、9组标ME，但图12实际选点尚待作者确认。'
        elif doi == '10.1016/j.cmet.2026.06.020' and index == 1:
            _fig5c_source_table(target)
            caption = ('源数据1-s2.0-S1550413126002469-mmc7.xlsx，Fig5C工作表A16:E20，保留原行号，显示至8位小数。'
                       '红色两行比值约1.014799155，蓝色两行约1.009513742；各对4个比值几乎相同。0.80一行作为相邻数据对照。需核查重复身份和原始拟合文件。')
        elif doi == '10.1038/s41380-022-01489-8' and index == 1:
            # Discard the old annotation banner, which overlapped panel labels.
            # The four original colour bars and their numeric endpoints remain.
            with Image.open(source) as im:
                im.crop((0, 445, im.width, im.height)).save(target)
            caption = '原图色标从左至右为A基因表达、B基因共表达、A功能连接、B功能连接。第二个色标上蓝下红，其他三个相反；这不等于数据被反向处理。'
        else:
            shutil.copy2(source, target)
        dimension = item.get('evidence_dimensions', {}).get(filename, 'data')
        prepared.append({'dimension': dimension, 'path': target, 'caption': _plain(caption)})
    return prepared


def make_pdf(doi: str, item: dict, destination: Path) -> None:
    renderer = renderer_module()
    paper = _paper_metadata(doi, item)
    evidence = prepare_evidence(doi, item, paper)
    evidence[0]['start_on_new_page'] = item.get('evidence_starts_on_new_page', True)
    evidence[-1]['max_height'] = 300
    result = '高风险' if item['risk'] else '低风险'
    # Reformat the already adjudicated evidence, without invoking a new review
    # or silently restoring an older judge's rejected findings.
    info = {
        'doi': doi, 'result': result, 'verdict': '建议' + result,
        'dimension_verdicts': {'image': '低风险', 'data': result, 'ref': '低风险', 'methodology': '低风险'},
        'image_review': '现有核验材料未提供独立支持图像重复或拼接异常的证据。',
        'data_review': (
            '问题位置：' + REVIEW_LOCATIONS[doi] + '。\n'
            '问题类型：图文、数据或计算说明的一致性核查。\n'
            '具体情况：' + _plain(item['summary']) + '\n'
            '需关注原因：需明确相应数值、实验条件或统计口径，以便按公开材料复核。\n'
            '核查依据：下列论文原图和源数据摘录；复算或标注由复核人员补充，与论文原文区别呈现。'
        ),
        'ref_review': ('具体情况：' + item['ref_review']) if item.get('ref_review') else '现有核验材料未提供独立支持参考文献高风险的证据。',
        'methodology_review': '与数据相关的实验条件、样本数和统计口径问题合并见数据复核部分。',
        'reason': _plain(item['summary']),
        'include_low_risk_evidence_images': not item['risk'] or any(ev['dimension'] == 'ref' for ev in evidence),
    }
    renderer._validate_review_info_consistency(info)
    # Reuse the exact production header, overview, dimension sections and
    # disclaimer instead of another fixed-coordinate imitation of the template.
    header = renderer._header_html(paper)
    overview = renderer._review_overview_html(None, info)
    analysis = renderer._analysis_html(info, evidence)
    analysis = analysis.replace(
        '<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、复核详情</h2>',
        '<h2 style="page-break-before:always; font-size:13pt; color:#1a1a1a; margin-top:0; margin-bottom:6pt;">三、复核详情</h2>',
    )
    disclaimer = renderer.REVIEW_DISCLAIMER_HTML.format(date=renderer.TODAY)
    full_html = f"<body style='font-family: {renderer.FONT_REGULAR};'>{header}{overview}{analysis}{disclaimer}</body>"
    renderer._render_pdf(full_html, str(destination))
    with fitz.open(destination) as doc:
        text = ''.join(page.get_text() for page in doc)
        if '\ufffd' in text or 'Astra重写版' in text:
            raise ValueError(f'{doi}: invalid PDF text')
        for section in ['一、论文基本信息', '二、复核风险结论', '三、复核详情', '四、综合评估', '五、免责声明']:
            if section not in text:
                raise ValueError(f'{doi}: missing section {section}')
        if sum(len(page.get_image_info()) for page in doc) != len(evidence):
            raise ValueError(f'{doi}: missing evidence images')


def _normalized(text: str) -> str:
    return re.sub(r'\s+', '', public_text(_plain(text))).replace('ﬂ', 'fl').replace('ﬁ', 'fi')


def public_summary(item: dict) -> str:
    """The summary and PDF share their exact issue paragraphs."""
    parts = [item['summary']]
    if item.get('ref_review'):
        parts.append(item['ref_review'])
    return '\n'.join(parts)


def validate_pdf(path: Path, item: dict, previous: Path, *, preserve_screenshots: bool = True) -> dict:
    with fitz.open(path) as doc, fitz.open(previous) as old:
        full_text = '\n'.join(page.get_text() for page in doc)
        for phrase in ['原报告', '旧报告', '原复审', '此前误判', '建议降级', 'Astra重写版']:
            if phrase in full_text:
                raise ValueError(f'{path.name}: process wording {phrase}')
        for paragraph in public_summary(item).splitlines():
            if _normalized(paragraph) not in _normalized(full_text):
                raise ValueError(f'{path.name}: PDF/summary mismatch: {paragraph[:50]}')
        def image_digests(pdf):
            return sorted(im['digest'].hex() for page in pdf for im in page.get_image_info(hashes=True))
        screenshots_unchanged = image_digests(doc) == image_digests(old)
        removed_only = (item.get('allow_removed_evidence')
                        and len(image_digests(doc)) < len(image_digests(old))
                        and set(image_digests(doc)).issubset(set(image_digests(old))))
        if preserve_screenshots and not screenshots_unchanged and not removed_only:
            raise ValueError(f'{path.name}: evidence image pixels changed')
        for page in doc:
            for span in page.get_texttrace():
                for char, glyph, _, _ in span['chars']:
                    if glyph == 0 and not chr(char).isspace():
                        raise ValueError(f'{path.name}: missing glyph {chr(char)!r}')
            for im in page.get_image_info():
                for block in page.get_text('blocks'):
                    if block[6] == 0 and not (fitz.Rect(block[:4]) & fitz.Rect(im['bbox'])).is_empty:
                        raise ValueError(f'{path.name}: image/text overlap')
        return {'pages': len(doc), 'images': len(image_digests(doc)),
                'summary_matches_pdf': True, 'screenshots_unchanged': screenshots_unchanged,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


SUMMARY_INTRO = ('本次整理胡高伟、樊令仲、王丹丹三位学者的共计48篇论文，其中10篇列为高风险论文。'
                 '其中，胡高伟共31篇，6篇列为高风险；樊令仲共10篇，2篇列为高风险；王丹丹共7篇，2篇列为高风险。'
                 '本次高风险归类包括需作者更正的图文、数字错误，以及需作者解释或补充材料的具体异常，'
                 '不等同于已经证明存在数据造假。')


def public_pdf_url(doi: str) -> str:
    name = 'review_' + doi.replace('/', '_').replace('－', '') + '.pdf'
    return 'http://202.120.57.100/review_reports/yujing_scholar/' + quote(name)


def write_summary(destination: Path, *, grouped: bool) -> None:
    lines = ['# 论文复核汇总', '',
             SUMMARY_INTRO, '',
             '下列问题按重要程度排列，与各篇PDF中的表述一致。']
    for scholar in ['胡高伟', '樊令仲', '王丹丹']:
        lines += ['', f'## {scholar}']
        for risk, category in [(True, '高风险论文')]:
            selected = [(doi, item) for doi, item in PAPERS.items()
                        if item['scholar'] == scholar and item['risk'] == risk]
            if not selected:
                continue
            lines += ['', f'### {category}（{len(selected)}篇）', '']
            for n, (doi, item) in enumerate(selected, 1):
                name = 'review_' + doi.replace('/', '_').replace('－', '') + '.pdf'
                folder = Path(scholar) / category if grouped else Path('high_risk_reports' if risk else 'attention_reports')
                lines += [f'#### {n}. {item["title"]}', '', f'DOI：{doi}', '',
                          f'[查看复核PDF](<{public_pdf_url(doi)}>)', '']
                for paragraph in public_summary(item).splitlines():
                    lines += [paragraph, '']
    destination.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    backup = ROUND / ('report_layout_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    backup.mkdir()
    staging = backup / 'regenerated'
    staging.mkdir()
    evidence_hashes = {name: hashlib.sha256((FIXED_EVIDENCE / name).read_bytes()).hexdigest()
                       for item in PAPERS.values() for name, _ in item['evidence']}
    checks = []
    for doi, item in PAPERS.items():
        slug = doi.replace("/", "_").replace("－", "")
        name = f"review_{slug}.pdf"
        if (OUT / name).exists():
            shutil.copy2(OUT / name, backup / name)
        make_pdf(doi, item, staging / name)
        checks.append({'doi': doi, **validate_pdf(staging / name, item, backup / name)})

    for filename, expected in evidence_hashes.items():
        if hashlib.sha256((FIXED_EVIDENCE / filename).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Screenshot file changed: {filename}')

    for pdf in staging.glob('*.pdf'):
        shutil.copy2(pdf, OUT / pdf.name)

    if ARCHIVE.exists():
        shutil.move(str(ARCHIVE), str(backup / 'previous_archive'))
    if GROUPED.exists():
        shutil.move(str(GROUPED), str(backup / 'previous_grouped_archive'))
    for label, folder in [('previous_archive', ARCHIVE), ('previous_grouped_archive', GROUPED)]:
        zip_path = folder.with_suffix('.zip')
        if zip_path.exists():
            shutil.copy2(zip_path, backup / (label + '.zip'))
    high = ARCHIVE / "high_risk_reports"
    attention = ARCHIVE / "attention_reports"
    evidence = ARCHIVE / "evidence_screenshots"
    high.mkdir(parents=True); evidence.mkdir()
    if any(not item['risk'] for item in PAPERS.values()):
        attention.mkdir()
    manifest = []
    grouped_manifest = []
    for doi, item in PAPERS.items():
        slug = doi.replace("/", "_").replace("－", "")
        src = OUT / f"review_{slug}.pdf"
        folder = high if item["risk"] else attention
        dst = folder / src.name
        shutil.copy2(src, dst)
        manifest.append({"scholar": item["scholar"], "doi": doi, "verdict": item["verdict"], "risk": item["risk"], "pdf": str(dst.relative_to(ARCHIVE))})
        category = '高风险论文' if item['risk'] else '需要注意的论文'
        scholar_folder = GROUPED / item['scholar'] / category
        scholar_folder.mkdir(parents=True, exist_ok=True)
        grouped_pdf = scholar_folder / src.name
        shutil.copy2(src, grouped_pdf)
        grouped_manifest.append({**manifest[-1], 'pdf': str(grouped_pdf.relative_to(GROUPED)),
                                 'sha256': hashlib.sha256(src.read_bytes()).hexdigest()})
        for filename, _caption in item["evidence"]:
            ev = FIXED_EVIDENCE / filename
            if ev.exists(): shutil.copy2(ev, evidence / f"{slug}_{filename}")

    write_summary(ARCHIVE / 'summary_astra_20261022.md', grouped=False)
    write_summary(GROUPED / '论文复核汇总.md', grouped=True)
    (ARCHIVE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (GROUPED / 'manifest.json').write_text(json.dumps(grouped_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (GROUPED / '校验记录.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    for folder, records in [(ARCHIVE, manifest), (GROUPED, grouped_manifest)]:
        shutil.make_archive(str(folder), 'zip', root_dir=folder)
        with zipfile.ZipFile(folder.with_suffix('.zip')) as zipped:
            assert zipped.testzip() is None
            for row in records:
                assert zipped.read(row['pdf']) == (folder / row['pdf']).read_bytes()
    print(json.dumps({"reports": len(PAPERS), "final_high_risk": sum(x["risk"] for x in PAPERS.values()),
                      "archive": str(GROUPED), "backup": str(backup)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
