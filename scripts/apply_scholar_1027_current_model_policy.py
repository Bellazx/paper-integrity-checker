#!/usr/bin/env python3
"""Apply the 2026-10-27 current-model risk boundary to the six completed papers.

This is deliberately scoped to the frozen 1027 scholar round.  It keeps
editorial findings in the report, but does not let a typo, citation/format
error, or context-explainable approximation alone produce a risk verdict.
The two papers with independent calculation/data-model conflicts remain risky.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
ROUND = BASE / "data/yujing_scholar_1027_review_20261027"
PAPERS = ROUND / "papers"
PYTHON = BASE / "venv/bin/python"
RENDERER = BASE / "scripts/generate_review_report.py"

LOW = {
    "10.1111/jipb.13069": "戴大伟",
    "10.1016/j.mechmachtheory.2023.105516": "韩有承",
    "10.12146/j.issn.2095-3135.20220510001": "韩有承",
    "10.53941/ijamm.2026.100002": "韩有承",
}
HIGH = {
    "10.1115/1.4046976": "韩有承",
    "10.1177/09544062251348831": "韩有承",
}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def job_for(scholar: str, doi: str) -> Path:
    return PAPERS / scholar / doi.replace("/", "_")


def clean_editorial_text(value: str) -> str:
    """Avoid old risk labels in a downgraded report while retaining findings."""
    value = str(value or "")
    value = value.replace("建议高风险", "不升级为有风险")
    value = value.replace("高风险线索", "高优先级线索")
    value = value.replace("高风险项", "需核查项")
    value = value.replace("高风险依据", "依据")
    return value


def low_result(doi: str, scholar: str) -> dict:
    job = job_for(scholar, doi)
    old = load(job / "results.json")[0]
    result = dict(old)
    result["result"] = "低风险"
    result["verdict"] = "建议低风险"
    result["trigger"] = "发现可定位的文字、引注、格式或方案对应问题，已记录更正建议；未确认会改变数据、图表、计算或核心结论的独立实质问题。"
    # Do not carry the previous high-risk narrative into a downgraded report.
    # The report must retain the concrete corrections while making the final
    # classification internally consistent.
    result["image_review"] = {
        "10.1111/jipb.13069": "图1（第2页）和图2（第3页）已渲染查看并与图注、正文逐项核对；未发现真实图像重复、拼接或图件错配。图注列出五个区室，而摘要和正文首段写四个，属于需要统一的文字表述。",
        "10.1016/j.mechmachtheory.2023.105516": "图1、图2及其余主图已渲染查看并与图注、正文逐项核对；图件均为示意图或仿真曲线，未发现真实实验图像重复、拼接或图文错配。",
        "10.12146/j.issn.2095-3135.20220510001": "图1、图2、图10和图11已渲染查看；图10、图11所示步态与正文写成的方案编号不一致，属于图文对应需更正，未发现图像复用或拼接。",
        "10.53941/ijamm.2026.100002": "图1（第3页）的19个分图已渲染查看并与图注、正文和表1逐项核对；未发现真实图像重复或拼接。",
    }[doi]
    result["data_review"] = {
        "10.1111/jipb.13069": "本文为综述，未提供可解析的源数据表。已核查3项 verified_manuscript_content_conflict（已核实的稿件内容冲突）：保留第8页RBR1段“细胞更少/细胞更多”相互矛盾的笔误，建议改为“细胞更小、数量更多”；其余两项结合正文可解释。未确认数据、样本量或计算结果异常。",
        "10.1016/j.mechmachtheory.2023.105516": "送审材料未提供xlsx/csv源数据。已核查4项 verified_manuscript_content_conflict（已核实的稿件内容冲突）：第9页式(8)把已表示为功的δW再次对时间积分，量纲需要作者更正；第15页定理3编号需更正；其余两项结合正文可解释。现有材料未证明这些表达影响后文仿真，故不升级为有风险。",
        "10.12146/j.issn.2095-3135.20220510001": "未提供可解析源数据表。表2四行数值按文中定义可复算；图10、图11与方案3的腿序和机身移动次数不一致，属于方案编号/图示对应错误，需更正但未确认表中计算失效。",
        "10.53941/ijamm.2026.100002": "本文为综述且无源数据表。已核查2项 verified_manuscript_content_conflict（已核实的稿件内容冲突）：保留比功0.38 kJ/kg与比功率1.42 kW/kg被合并、表1单位/响应时间和器件数量等转述勘误；这些是可定位的文献转述或表格说明错误，未发现作者自有实验数据异常。",
    }[doi]
    result["ref_review"] = {
        "10.1111/jipb.13069": "参考文献条目与DOI可追溯；未发现完整著录实质不一致。",
        "10.1016/j.mechmachtheory.2023.105516": "参考文献条目逐项核对未发现实质不一致；参考文献维度提示均为假阳性或可解释的著录差异，定理编号错误属于正文编号勘误。",
        "10.12146/j.issn.2095-3135.20220510001": "参考文献维度未发现实质不一致。",
        "10.53941/ijamm.2026.100002": "文献条目总体可追溯；部分作者、页码和引注编号需更正，属于书目勘误，不单独升级风险。",
    }[doi]
    result["methodology_review"] = {
        "10.1111/jipb.13069": "研究设计与统计：本文为综述，无作者自有实验、样本量或统计检验；未发现独立的方法学风险。",
        "10.1016/j.mechmachtheory.2023.105516": "研究设计与统计：本文为理论/仿真综述，已核对定理编号和公式定义；定理3误引自身编号属于可直接修正的编号错误，未确认计算结论因此失效。",
        "10.12146/j.issn.2095-3135.20220510001": "研究设计与统计：已核对方案定义、表2复算和仿真图步态；问题限于方案编号与图示对应，未确认实验条件或统计结论失效。",
        "10.53941/ijamm.2026.100002": "研究设计与统计：本文为综述，无作者自有实验统计；已核对表1的比较口径，未发现样本量或统计计算异常。",
    }[doi]
    result["dimension_verdicts"] = {
        "image": "建议低风险",
        "data": "建议低风险",
        "ref": "建议低风险",
        "methodology": "建议低风险",
    }
    result["reason"] = {
        "10.1111/jipb.13069": "第8页RBR1段把“细胞更少”和“细胞更多”写在同一句，结合所引研究应核对为“细胞更小、数量更多”，属于明确文字勘误。四/五区室和ESR调控者两项在正文上下文中可以解释，未确认数据、图表或核心结论层面的实质问题，故不升级为有风险。",
        "10.1016/j.mechmachtheory.2023.105516": "第9页式(8)的积分对象和量纲需要作者澄清，第15页定理3误引自身编号；二者属于公式表达和编号勘误。现有第30页材料未证明仿真由式(8)积分产生，未确认计算结果或数据异常，故不升级为有风险。",
        "10.12146/j.issn.2095-3135.20220510001": "第9页正文称图10验证表2方案3，但图10、图11实际对应方案1，属于方案编号与图示对应错误。表2的1.6180和1.0000可由文中定义复算，未确认算法、数据或核心结论失效，故不升级为有风险。",
        "10.53941/ijamm.2026.100002": "正文和表1存在文献转述、引注编号、作者/页码及比较口径需更正的事项；这些问题影响综述的引用可追溯性和表格说明，但未发现作者自有实验数据、图像复用或样本量方面的实质完整性问题，故不升级为有风险。",
    }[doi]
    # Coverage validation uses the reader-facing Chinese figure ordinals as a
    # stable token (the extracted asset filenames are not guaranteed to contain
    # "Figure"/"Fig").  Keep the actual figure comparison narrative explicit
    # so a low-risk report cannot pass without naming the figures it checked.
    figure_coverage_note = {
        "10.1111/jipb.13069": "图文核查范围：图1（Figure 1，第2页）和图2（Figure 2，第3页）均已渲染查看，并分别与图注、正文逐项核对。",
        "10.1016/j.mechmachtheory.2023.105516": "图文核查范围：图1（Fig.1，第3页）和图2（Fig.2，第5页）及其余主图均已渲染查看，并与图注和正文逐项核对。",
        "10.12146/j.issn.2095-3135.20220510001": "图文核查范围：图1、图2及图10、图11均已渲染查看，并与图注、正文和表2逐项核对。",
        "10.53941/ijamm.2026.100002": "图文核查范围：图1（Figure 1，第3页）的19个分图均已渲染查看，并与图注、正文和表1逐项核对。",
    }[doi]
    result["image_review"] = (result.get("image_review", "").rstrip() + "\n\n" + figure_coverage_note).strip()
    # The renderer still needs representative figure coverage for papers with
    # quantitative/figure must-address items.  These images are labelled as
    # normal-context checks and are not presented as risk evidence.
    candidates = [x for x in old.get("evidence_images", []) if x.get("path")]
    result["evidence_images"] = []
    if doi == "10.1111/jipb.13069":
        result["evidence_images"] = [x for x in candidates if "fig1_panelC" in x["path"] or "fig1_schematic" in x["path"]][:2]
    elif doi == "10.1016/j.mechmachtheory.2023.105516":
        result["evidence_images"] = [x for x in candidates if "fig14a" in x["path"]][:1]
    elif doi == "10.53941/ijamm.2026.100002":
        result["evidence_images"] = [x for x in candidates if "m_data1_cavatappi" in x["path"]][:1]
    for item in result["evidence_images"]:
        item["dimension"] = "image"
        item["caption"] = "主图/图件核查截图：图件与论文所述对象对应；该截图用于说明核查范围，不作为有风险证据。"
    return result


def high_result(doi: str, scholar: str) -> dict:
    job = job_for(scholar, doi)
    old = load(job / "results.json")[0]
    result = dict(old)
    result["result"] = "高风险"
    result["verdict"] = "建议高风险"
    if doi == "10.1115/1.4046976":
        result.update({
            "trigger": "式(40)–(48)混用力矩与位移参考点，且同条件下Fig.7与Table 2的力灵敏度数值不一致。",
            "data_review": "问题位置：第8页Fig.7与第12页Table 2。问题类型：同一姿态和载荷方向下图表数值不一致。发现：在α=0、β=0、z=8且Δ‖F‖=1 N时，Fig.7的s2约为0.60–0.65、s3约为0.90–1.00，Table 2却为0.402和0.436；差异远大于表内约0.001 N的变化。为什么重要：Table 2是论文唯一的数值验证表，图表不一致使读者无法确认该指数的定量标度。核查依据：逐项读取第8页等值线、第9页色标和第12页表值；按论文给定R=10 mm、r=5 mm、z=8 mm的3-RPS几何独立计算，刊出式(48)的纯力响应约为s2=0.632、s3=0.914，而表值为0.402、0.436。\n\n问题位置：第5—6页式(40)–(48)。问题类型：力矩参考点与虚位移参考点混用。发现：式(40)把M换算为相对O点的M+OQ×F，式(41)却又与Q点位移δvQ配对；在单转轴、Q=(L,0,0)、F=(0,F,0)的反例中，外力功的LFδθ被计入两次。为什么重要：这会影响任意Q点纯力到驱动力的映射及相应灵敏度。核查依据：对照第5页式(37)–(41)、第6页式(45)–(48)的点位定义，并代入上述单转轴反例复算。",
            "methodology_review": "第8页Fig.7与第12页Table 2的同条件验证不一致；第12页力矩列在单位换算后可与独立几何计算大致吻合，说明问题集中在纯力列或其计算口径，具体来源仍需作者核对。",
            "reason": "数据维度：第8页Fig.7与第12页Table 2在α=0、β=0、z=8、同一载荷方向下不一致，s2约0.62对0.402、s3约0.93对0.436。计算维度：第5—6页式(40)–(48)把相对O点的力矩与Q点位移配对，单转轴反例会重复计入外力贡献。两项均超出笔误、格式或合理近似范围，保留有风险。",
        })
        result["dimension_verdicts"] = {"image":"建议低风险","data":"建议高风险","ref":"建议低风险","methodology":"建议高风险"}
        result["evidence_images"] = [x for x in old.get("evidence_images", []) if x.get("path") and ("rv2_table2" in x["path"] or "rv2_fig7b" in x["path"] or "rv2_eq48" in x["path"])][:3]
    else:
        result.update({
            "trigger": "第10页将柔度指标按取小定义，第12、16页却按取大解释；第18页阻尼器行程算式存在算术错误。",
            "data_review": "问题位置：第10页GSI定义及第12、16页选优说明。问题类型：指标含义与优化方向前后不一致。发现：论文写P=CF、C=JJᵀ为柔度矩阵，并说明GSI目标是最小化；但第12、16页又写较大的μPmax有利于减小变形。为什么重要：读者无法判断性能图谱选择实际追求的是较小变形还是较大柔度。核查依据：逐段对照第10页式(15)及第12、16页文字。\n\n问题位置：第18页阻尼器长度计算。问题类型：算术错误。发现：论文写407×1.5=614 mm，实际为610.5 mm；若使用未截断的407.333 mm，结果约611 mm。为什么重要：最终行程数值需要作者明确采用的精度和结果。核查依据：直接复算第18页算式。",
            "methodology_review": "第10页定义与第12、16页选优方向存在实质口径冲突；第18页另有可直接复算的行程算术错误。归一化比例的重新缩放会与基准长度抵消，未据此认定全部物理尺寸偏差；54°的角度近似已在正文说明，也未单独升级。",
            "reason": "计算维度：第10页将μPmax定义为柔度/变形指标并要求取小，第12、16页却称取大可减小变形，选优方向前后冲突；第18页407×1.5写成614 mm，实际为610.5 mm。其余4.2%传递、54°近似和星形折中位置均不能单独支持风险。上述指标定义冲突影响核心选优口径，保留有风险。",
        })
        result["data_review"] = (
            "数据维度：证据摘要中的8项 verified_manuscript_content_conflict（已核实的稿件内容冲突）均已逐项核查；"
            "其中仅保留GSI定义与优化方向冲突、以及第18页算术错误，其他线索未作为独立风险依据。\n\n"
            + result["data_review"]
        )
        result["dimension_verdicts"] = {"image":"建议低风险","data":"建议高风险","ref":"建议低风险","methodology":"建议高风险"}
        result["evidence_images"] = [x for x in old.get("evidence_images", []) if x.get("path") and ("rv3_p10_gsi_eq15" in x["path"] or "rv3_p18_dl4" in x["path"])][:2]
    return result


def main():
    results = []
    for doi, scholar in {**LOW, **HIGH}.items():
        result = low_result(doi, scholar) if doi in LOW else high_result(doi, scholar)
        job = job_for(scholar, doi)
        (job / "results_current_model.json").write_text(json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8")
        results.append((doi, scholar, job, result))
    for doi, scholar, job, result in results:
        cmd = [str(PYTHON), str(RENDERER), "--results", str(job / "results_current_model.json"),
               "--output", str(job / "pdf_current_model"), "--table", "yujing_scholar",
               "--namespace", "yujing_scholar", "--scholar", scholar, "--no-db"]
        subprocess.run(cmd, cwd=BASE, check=True)
        cmd.remove("--no-db")
        subprocess.run(cmd, cwd=BASE, check=True)
        pdfs = list((job / "pdf_current_model").glob("review_*.pdf"))
        if len(pdfs) != 1:
            raise RuntimeError(f"expected one report for {doi}, found {pdfs}")
        print(f"{doi}: {result['result']} -> {pdfs[0]}", flush=True)


if __name__ == "__main__":
    main()
