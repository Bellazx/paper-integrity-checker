#!/usr/bin/env python3
"""Build a downloadable archive for the 48-paper scholar rereview round."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
sys.path.insert(0, str(BASE))
from utils.db import DB_CONFIG

ROUND = BASE / "data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1"
ARCHIVE = ROUND / "all_high_risk_reports_20261022"
REPORT_DIR = BASE / "data/output/yujing_scholar/review_v2"

ATTENTION = {
    "胡高伟": [
        ("10.1007/s11802-021-4354-7", "低风险；正文压差范围/稳态表述与表3复算不完全一致，建议更正实验条件表述。"),
        ("10.1016/j.jngse.2021.103844", "低风险；Table 2 局部记录疑似重复录入，另有一条参考文献 DOI 粘贴错误。"),
        ("10.1016/j.marpetgeo.2023.106169", "低风险；两条参考文献 DOI 指向无关论文，建议核对并更正。"),
        ("10.1016/j.jgsce.2025.205818", "低风险；补充表峰高小数点、重复实验标签及计算口径需核对。"),
        ("10.1016/j.oceaneng.2023.116545", "低风险；第10页正负方向用词及底鼓峰值位置表述与图示不一致。"),
        ("10.1016/j.oceaneng.2024.116791", "低风险；内摩擦角增幅34.88%无法由图和公式复算，部分体变图纵轴正负方向相反。"),
        ("10.3390/en15144938", "低风险；Figure 6 波形起始时间读数约6.81 μs，正文写6.31 μs，属于可更正的图文标注问题。"),
        ("10.1155/2022/7746386", "低风险；部分重复实验条件栏与公开曲线逐位相同，Table S7/S8全同，且一处结论被Table S10b反驳，需补充记录说明。"),
        ("10.1016/j.earscirev.2025.105058", "低风险；参考文献作者姓氏 Collett 被著录为 Collect，属于编校错误。"),
        ("10.31035/cg2025183", "低风险；界面名称及时间步长存在局部表述不一致，建议统一。"),
    ],
    "樊令仲": [
        ("10.1016/j.scib.2024.03.031", "低风险；存在基因数量、图号、样本图像数及统计 P 值等内部标注不一致，未影响结论方向。"),
        ("10.1523/JNEUROSCI.1510-24.2024", "低风险；“方差最大/均值最大”、筛选比例5%/0.83%等文字需统一。"),
    ],
    "王丹丹": [
        ("10.1016/j.cell.2024.03.030", "低风险；图5D图注样本量字母排版错误，补充表与图注统计检验名称不一致。"),
        ("10.1016/j.cell.2026.02.017", "低风险；少数 P 值和显著性括号标注与复算值存在轻微差异，不改变结论方向。"),
    ],
}

HIGH_SUMMARY = {
    "胡高伟": [
        ("10.1016/j.energy.2026.140018", "Energy 实验研究。第13页 Fig.15 的应变能密度读数约为 82.4/68.8/54.8 kJ/m³，但按第12页 Eq.(7) 与 Fig.5(a) 积分应为 807.2/668.4/549.2 kJ/m³，整体差约10倍；Table 3 的 E50 及 Eq.(11) 也无法与图表复算，直接影响储层失效判定。"),
        ("10.1016/j.fuel.2021.121866", "Fuel 分子模拟。第2页写每池 1230 个水分子、6 Na⁺、6 Cl⁻并称盐度3.1 wt%，按公开组成复算约1.56 wt%，相差约一倍；第7页参考文献40、41的 DOI/题名/刊名/卷页互相不匹配，Fig.5B 拟合线和误差棒也与数据不自洽。"),
        ("10.1016/j.oceaneng.2025.123854", "Ocean Engineering 数值研究。第13页 Fig.15 的应变能密度比 Eq.(7) 积分结果小约10倍；Table 3 的 E50 无法由正文声明的1.25%应变复算；第12页 Eq.(11) 推导多出 +2σ3² 项，使弹性能结果差约23–30倍，影响核心失效判定。"),
        ("10.19657/j.geoscience.1000－8527.2021.173", "第8–9页表8、表9声明地质/工程权重为0.5:0.5，但按公开数据重算无法得到论文四个综合评价值，结果反而接近未披露的0.25:0.75权重，核心评价不可按公开方法复现。"),
        ("10.31035/cg2022015", "第11页 Fig.12 等值面只使用16组实验中的7组，导致4.7‰系列图面趋势与Table 1及结论相反；图轴单位写 %，正文和表格写 ‰，相差100倍，另有Case 2/3浓度复算不一致。"),
        ("10.3390/jmse12122239", "第9页 Fig.5a/5b 甲烷“Experiment data”与第7页Table 1系统性不符，纵波差243–557 m/s、横波差86–553 m/s（约52%）；摘要、4.3节和结论对反超区间的表述也互相矛盾。"),
    ],
    "樊令仲": [
        ("10.1038/s41380-022-01489-8", "图文摘要将 Visual 基因数写为3586，但补充源数据和独立重算均为3568；Fig.5B Spearman 色标正负方向与其他相关图相反，可能导致相关性正负被反向解读。"),
        ("10.1038/s41467-025-62812-9", "Figure6B 源数据661行中15行出现跨随访时点逐位重复记录；图中统计量只有剔除这15行、按n=646才能复现，但论文多处声明n=661且未披露剔除；另有图3D与Table S1相关系数不一致。"),
    ],
    "王丹丹": [
        ("10.1016/j.cmet.2026.06.020", "源数据 Fig.5C 中0.70/0.85 μM及0.75/0.90 μM两组重复值分别呈精确常数倍关系，不能直接视为独立生物学重复，影响 EC50 及不确定性；补充 Fig.S1E 两个 DHA 区块72个数值完全重复。"),
        ("10.1093/jmcb/mjac004", "正文第4页 Fig.2A 与附表S8的 APOB/APOA1 比值相差3–53倍，且图面未体现源数据中的 APOB 富集；兔模型声明每组3只但附表仅见2只，另有未说明的第2周时间点；蛋白/肽段计数也无法由源数据复现。"),
    ],
}


def main() -> int:
    if ARCHIVE.exists():
        shutil.rmtree(ARCHIVE)
    reports = ARCHIVE / "high_risk_reports"
    evidence = ARCHIVE / "evidence_screenshots"
    reports.mkdir(parents=True)
    evidence.mkdir(parents=True)

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute("""SELECT scholar,doi,title,review_result,review_report_url
                       FROM yujing_scholar
                       WHERE scholar IN (N'胡高伟',N'樊令仲',N'王丹丹')
                         AND review_result=N'高风险' ORDER BY scholar,id""")
        rows = cur.fetchall()
    finally:
        conn.close()
    if len(rows) != 10:
        raise RuntimeError(f"expected 10 final high-risk rows, got {len(rows)}")

    manifest = []
    for row in rows:
        url_name = str(row["review_report_url"]).rsplit("/", 1)[-1]
        src = REPORT_DIR / url_name
        if not src.exists():
            raise FileNotFoundError(src)
        dst = reports / url_name
        shutil.copy2(src, dst)
        manifest.append({"scholar": row["scholar"], "doi": row["doi"], "title": row["title"], "pdf": str(dst.relative_to(ARCHIVE))})

        # Copy all judge evidence screenshots for the same DOI.
        for result_file in (ROUND / "results_judge").glob("batch_*.json"):
            data = json.loads(result_file.read_text(encoding="utf-8"))
            item = data[0] if isinstance(data, list) else data
            if str(item.get("doi", "")).lower() != str(row["doi"]).lower():
                continue
            for idx, ev in enumerate(item.get("evidence_images") or [], 1):
                ev_src = Path(str(ev.get("path", "")))
                if ev_src.exists():
                    ev_dst = evidence / f"{row['doi'].replace('/', '_')}_{idx}_{ev_src.name}"
                    shutil.copy2(ev_src, ev_dst)
            break

    lines = [
        "# 胡高伟、樊令仲、王丹丹批量复审汇总",
        "",
        "本批共48篇论文，最终复审高风险10篇、低风险38篇。高风险表示存在可定位、可复算、需要作者解释的异常，不等同于已经证明存在数据造假。",
        "",
        "## 胡高伟",
        "### 有风险论文",
    ]
    for doi, note in HIGH_SUMMARY["胡高伟"]:
        lines.append(f"- **{doi}**：{note}")
    lines += ["", "### 需要注意的论文（复审低风险）"]
    for doi, note in ATTENTION["胡高伟"]:
        lines.append(f"- **{doi}**：{note}")
    lines += ["", "## 樊令仲", "### 有风险论文"]
    for doi, note in HIGH_SUMMARY["樊令仲"]:
        lines.append(f"- **{doi}**：{note}")
    lines += ["", "### 需要注意的论文（复审低风险）"]
    for doi, note in ATTENTION["樊令仲"]:
        lines.append(f"- **{doi}**：{note}")
    lines += ["", "## 王丹丹", "### 有风险论文"]
    for doi, note in HIGH_SUMMARY["王丹丹"]:
        lines.append(f"- **{doi}**：{note}")
    lines += ["", "### 需要注意的论文（复审低风险）"]
    for doi, note in ATTENTION["王丹丹"]:
        lines.append(f"- **{doi}**：{note}")
    lines += [
        "", "## 归档内容", "",
        "- `high_risk_reports/`：10篇高风险复审 PDF。",
        "- `evidence_screenshots/`：高风险判定所引用的证据截图。",
        "- `manifest.json`：论文、学者与 PDF 文件对应关系。",
    ]
    (ARCHIVE / "summary_all_high_risk.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ARCHIVE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(ARCHIVE)
    print(f"high_risk_pdfs={len(manifest)} evidence_files={len(list(evidence.iterdir()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
