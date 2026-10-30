#!/usr/bin/env python3
"""Downgrade five GPT-rechecked papers and publish concise, gentle reports."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND = BASE / "data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1"


def load_apply_module():
    source = BASE / "scripts/apply_scholar_hugaowei_fanlingzhong_wangdandan_round.py"
    spec = importlib.util.spec_from_file_location("scholar_three_apply", source)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {source}")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    return wrapper.load_module()


LOW = {key: "建议低风险" for key in ("image", "data", "ref", "methodology")}


CASES = {
    1003018: {
        "doi": "10.1016/j.jngse.2021.103844",
        "result": "低风险",
        "verdict": "建议低风险",
        "trigger": "GPT/Codex 独立版面复核后降级",
        "dimension_verdicts": LOW,
        "image_review": "图像核查：8张随文原始图未发现重复使用或明显拼接。",
        "data_review": (
            "建议核对：论文第6页 Table 2 有两组相邻记录数值相同，分别为 "
            "74%/1328/2722 和 66%/1172/2548。结合表格版面看，更像整理表格时的重复录入；"
            "现有材料不足以据此判断实验结果存在实质问题，建议作者回查原始记录后更正。"
        ),
        "ref_review": (
            "建议核对：第7页 Murphy、Reischer 与 Hsu（1993）条目所附 DOI "
            "10.1109/CCNC.2006.1593258 指向一篇电视接收机论文；对应砂体声波研究的 DOI "
            "应为 10.1190/1.1443408。该情况更符合参考文献链接粘贴错误。"
        ),
        "methodology_review": "研究设计、图表趋势和主要结论未发现需要上调风险的其他问题。",
        "evidence_images": [
            {
                "dimension": "data",
                "path": str(BASE / "data/output/review_v2/10.1016_j.jngse.2021.103844_evidence_imgs/data_table2_duprows.png"),
                "caption": "Table 2 中两组相邻记录数值相同，建议回查原始记录，确认是否为重复录入。",
            },
            {
                "dimension": "ref",
                "path": str(BASE / "data/output/review_v2/10.1016_j.jngse.2021.103844_evidence_imgs/ref_murphy_doi_clear.png"),
                "caption": "Murphy（1993）条目所附 DOI 指向其他论文，属于可直接更正的引用链接问题。",
            },
        ],
        "reason": (
            "复核确认两处可直接更正的文稿问题：Table 2 局部记录疑似重复录入，以及一条参考文献 DOI "
            "粘贴错误。两项均值得在汇报中提示作者核对，但目前未发现其影响论文主要数据趋势或核心结论，"
            "综合建议低风险。"
        ),
    },
    1003022: {
        "doi": "10.1016/j.marpetgeo.2023.106169",
        "result": "低风险",
        "verdict": "建议低风险",
        "trigger": "GPT/Codex 独立版面复核后降级",
        "dimension_verdicts": LOW,
        "image_review": (
            "图像核查：初检提示的两处拼接疑点经原图和版面复核均未成立，未发现图像重复或明显拼接。"
        ),
        "data_review": "可读取的图表数值、样本数量和正文趋势未发现实质矛盾。",
        "ref_review": (
            "建议核对：第10页 Murphy（1993）和 Cranston（1994）两条文献的 DOI 均链接到无关论文。"
            "对应 DOI 应分别为 10.1190/1.1443408 和 10.37570/bgsd-1995-41-11。"
            "两条著录的作者、题名和期刊信息本身清楚，情况更符合整理参考文献时误粘贴 DOI。"
        ),
        "methodology_review": "仪器、实验轮次和研究时间线未发现需要上调风险的问题。",
        "evidence_images": [
            {
                "dimension": "ref",
                "path": str(BASE / "data/output/review_v2/evidence_1003022/ref_doi_mismatch.png"),
                "caption": "两条参考文献的著录内容与所附 DOI 不对应，建议按正确 DOI 直接更正。",
            }
        ],
        "reason": (
            "复核确认两条参考文献的 DOI 存在粘贴错误，读者按链接会进入无关论文。该问题应在汇报中保留并"
            "建议作者更正，但未发现论文数据、图像或方法存在相应异常，综合建议低风险。"
        ),
    },
    1003023: {
        "doi": "10.1016/j.jgsce.2025.205818",
        "result": "低风险",
        "verdict": "建议低风险",
        "trigger": "GPT/Codex 独立版面复核后降级",
        "dimension_verdicts": LOW,
        "image_review": "图像核查：9张投稿图未发现重复使用或明显拼接。",
        "data_review": (
            "建议核对两处文稿细节：第一，补充材料 Table S1 的 2911 cm⁻¹ 峰高写为 64174；结合峰面积、"
            "半高宽和 Fig.2(b) 约6600的峰顶判断，更可能是 6417.4 的小数点遗漏。第二，Fig.S1(a)/(b) 的"
            "平台高低次序与 Table S2 的 Replicate 1/2 相反，更像两次重复实验的编号或标签互换。"
            "两处均可通过回查原始记录直接确认。"
        ),
        "ref_review": "抽查的参考文献题名、期刊、年份和 DOI 未发现实质不一致。",
        "methodology_review": (
            "正文报告的峰面积比 1.6188 与 Table S1 复算值 1.9026 不完全一致，建议统一计算口径；"
            "两者均低于文中比较的理论值 3:1，未改变该处定性判断。"
        ),
        "evidence_images": [
            {
                "dimension": "data",
                "path": str(BASE / "data/output/review_v2/evidence_1003023/final_tableS1_recompute.png"),
                "caption": "Table S1 的2911 cm⁻¹峰高疑似遗漏小数点；改为6417.4后可与峰面积和谱图对应。",
            },
            {
                "dimension": "data",
                "path": str(BASE / "data/output/review_v2/evidence_1003023/final_figS1_plateau.png"),
                "caption": "Fig.S1两次重复实验的平台顺序与Table S2标签相反，建议核对Replicate 1/2编号。",
            },
        ],
        "reason": (
            "复核后更倾向于小数点遗漏、重复实验标签互换及计算口径未统一，均属于可核对、可更正的文稿问题。"
            "现有证据不足以认定数据本身存在实质异常，综合建议低风险。"
        ),
    },
    1003029: {
        "doi": "10.1523/JNEUROSCI.1510-24.2024",
        "result": "低风险",
        "verdict": "建议低风险",
        "trigger": "GPT/Codex 独立版面复核后降级",
        "dimension_verdicts": LOW,
        "image_review": "图像核查：21个原始图文件未发现实验图像重复或明显拼接。",
        "data_review": (
            "建议核对：正文第6页将 SLF II 写成“均值最大”，而图2b和扩展图显示其特点实际是方差最大；"
            "这更像 mean/variance 的文字误写。方法称排除 FX，但结果图仍显示 FX，建议作者说明该束是否仅"
            "用于展示。图表数值本身可以相互对应。"
        ),
        "ref_review": "初检参考文献告警均由双栏断行或模糊匹配造成，逐条复核后未发现真实引用错配。",
        "methodology_review": (
            "结果段写 top 5%，但方法、图注及每端130个基因的计数均支持0.83%，更像局部文字沿用错误。"
            "此前“72束与64束矛盾”的判断不成立：72束在排除4类双侧束后正好剩64束。"
        ),
        "evidence_images": [
            {
                "dimension": "data",
                "path": str(ROUND / "results_judge/evidence/batch_1003029/1_fig2b_gc2_rc_panel.png"),
                "caption": "SLF II 在该面板中是方差较大，并非均值最大，建议更正正文措辞。",
            },
            {
                "dimension": "data",
                "path": str(ROUND / "results_judge/evidence/batch_1003029/3_fx_column_and_fig2b_fx_point.png"),
                "caption": "方法称排除FX，但结果图仍显示FX；建议作者补充说明展示与分析口径。",
            },
        ],
        "reason": (
            "复核确认几处局部文字和分析口径表述需要更正，但最主要的纤维束数量矛盾属于流程误读。"
            "现有图表和源数据能够相互对应，未见足以支持高风险的证据，综合建议低风险。"
        ),
    },
    1003044: {
        "doi": "10.31035/cg2025183",
        "result": "低风险",
        "verdict": "建议低风险",
        "trigger": "GPT/Codex 独立版面复核后降级",
        "dimension_verdicts": LOW,
        "image_review": (
            "图像核查：初检的4组相似图均为同一模拟体系和渲染模板造成，未发现图像重复或明显拼接。"
        ),
        "data_review": (
            "建议核对：3.4节有一段把上、下界面的变化方向写反；同节前后文字、图14、图15和结论均一致"
            "支持“上界面增厚、下界面基本稳定”，因此更像该段界面名称对调。摘要0.3 V/nm与正文"
            "0.5 V/nm的平台表述也建议统一，但相关误差区间重叠，不宜据此作过度判断。"
        ),
        "ref_review": "参考文献抽查未发现真实错配；初检的609条统计是双栏断行造成的解析伪影。",
        "methodology_review": (
            "建议核对：摘要写时间步长2 fs，方法写1 fs，属于需要统一的参数表述。此前对模拟次数的"
            "高风险判断不成立：30次可解释为6个场强×5次运行，三次主运行另有两次补充运行。"
        ),
        "evidence_images": [
            {
                "dimension": "data",
                "path": str(BASE / "data/tmp/b1003044/ev_text_34.png"),
                "caption": "3.4节同页前后段落对上下界面的称呼相反，更像局部名称对调。",
            },
            {
                "dimension": "methodology",
                "path": str(BASE / "data/tmp/b1003044/ev_timestep.png"),
                "caption": "摘要与方法部分的时间步长分别写为2 fs和1 fs，建议统一。",
            },
        ],
        "reason": (
            "复核确认界面名称和时间步长存在局部表述不一致，但模拟次数可由实验层级完整解释，图表与全文"
            "主要趋势也一致。问题更符合文稿整理或复制修改时未同步更正，综合建议低风险。"
        ),
    },
}


SUMMARY = """这批材料中，前期高风险候选已进一步结合原始PDF版面、图表和补充材料进行独立复核。现阶段保留高风险3篇；另有5篇发现了需要作者核对的复制、粘贴、标签或局部表述问题，但现有证据不足以认定为高风险。这些问题不等同于数据造假，建议作为文稿修改事项在汇报中保留。

学者：胡高伟

1. DOI：10.1016/j.jngse.2021.103844
Table 2有两组相邻记录数值相同，另有一条参考文献DOI粘贴到无关论文。更像表格重复录入和引用链接整理错误，建议作者回查原始记录并更正。

2. DOI：10.1016/j.marpetgeo.2023.106169
两条参考文献的DOI链接到无关论文，但作者、题名和期刊著录清楚，更像整理参考文献时误粘贴链接，建议直接更正。

3. DOI：10.1016/j.jgsce.2025.205818
补充表一个峰高疑似遗漏小数点，两次重复实验的图表编号可能互换，另有一处计算口径未统一。相关定性结论未因此改变，建议作者核对原始记录后修订。

4. DOI：10.31035/cg2025183
正文有一段将上、下界面名称写反，摘要与方法的时间步长分别写为2 fs和1 fs。全文其他图文对主要趋势的表述一致，更像局部文字修改时未同步更新。

学者：樊令仲

1. DOI：10.1523/JNEUROSCI.1510-24.2024
正文将“方差最大”写成“均值最大”，基因筛选比例一处写成5%而方法和图注均为0.83%，另需说明FX在方法与结果图中的展示口径。图表数值本身可以对应，属于建议作者统一表述的文稿问题。
"""


def load_item(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data[0] if isinstance(data, list) else data


def main() -> int:
    module = load_apply_module()
    backup = ROUND / "results_judge_before_gpt_gentle_downgrade"
    backup.mkdir(parents=True, exist_ok=True)
    for batch_num, result in CASES.items():
        result["include_low_risk_evidence_images"] = True
        judge_path = ROUND / f"results_judge/batch_{batch_num}.json"
        if not (backup / judge_path.name).exists():
            shutil.copy2(judge_path, backup / judge_path.name)
        judge_path.write_text(
            json.dumps([result], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        paper = load_item(ROUND / f"batches/batch_{batch_num}.json")
        pdf = module.render(paper, result, batch_num)
        module.update_db(paper, "低风险")
        print(f"{batch_num} {result['doi']} -> low risk; {pdf}")
    (ROUND / "leader_summary_gpt_recheck.md").write_text(SUMMARY, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
