#!/usr/bin/env python3
"""Build the concise, screenshot-backed final result for one high-risk paper."""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND = BASE / "data/yujing_4050_sep30_comm_highrisk_round_1"
EVIDENCE_DIR = BASE / "data/output/yujing_4050_202607/review_evidence"
OUT = ROUND / "revised_results/batch_1110003.json"


def main():
    result = {
        "doi": "10.1073/pnas.2535342123",
        "result": "高风险",
        "trigger": "4050_sep30_comm_highrisk_round1 简明版复核报告",
        "image_review": (
            "检查正文和补充材料共27张图，未看到同一图在不同位置重复，也未看到明显拼接边界。"
            "图像不是本次高风险结论的依据。"
        ),
        "data_review": (
            "核心问题：\n"
            "问题位置：三份源数据表——sd01.xlsx 第6–8行、sd02.xlsx 第44–45行和第47–48行、"
            "sd03.xlsx 第9–10行和第33–34行。\n"
            "问题类型：不同通路行重复使用同一批结果。\n"
            "具体情况：sd01中三条不同的rRNA通路都显示 BgRatio=176/8691、Count=118，且118个命中基因名单逐字相同；"
            "sd02中两对不同通路分别重复了22个和23个基因，BgRatio分别为65/8691和75/8691；"
            "sd03中两对不同通路分别重复了47个和17个基因，BgRatio分别为90/8691和33/8691。"
            "证据截图直接标出了这些行。\n"
            "需关注原因：通路名称和通路定义不同，却同时使用完全相同的基因名单、背景比例和命中数，"
            "会使富集分析结果无法作为彼此独立的结果来解释。\n"
            "核查依据：逐行读取三个Excel中的通路ID、通路名称、BgRatio、Count和geneID，并对基因名单逐字比对。\n"
            "图文核查：已核对Fig.1–Fig.8及补充图对应的图注、趋势和可用源数据，未发现另有图文矛盾。\n"
            "其他告警：4组跨行重复/整数偏移类告警（sd01 17组、sd02 10组、sd03 10组、sd05 100组）已逐组核查；"
            "sd01–sd03与上述通路结果重复相符，sd05中相邻通路的相关系数和通路名称并不相同，未另发现独立问题。"
        ),
        "ref_review": (
            "检查正文列出的45条参考文献，未发现完整著录与 DOI、题名、作者或期刊明显不一致的条目。"
            "参考文献不是本次高风险结论的依据。"
        ),
        "methodology_review": (
            "核对了研究方法、伦理声明、图注和样本量：动物实验伦理编号为2021-183，Fig.1H声明n=3，"
            "Fig.4A的三组重复与源数据一致，未发现方法学或样本量自相矛盾。"
        ),
        "verdict": "建议高风险",
        "reason": (
            "简单结论：高风险来自源数据表，而不是图片或参考文献。三份Reactome富集结果表中，"
            "多条不同通路重复使用完全相同的基因名单和背景计数；例如sd01第6–8行三条通路均为"
            "BgRatio=176/8691、Count=118、118个基因完全相同，sd02和sd03也有同类重复。"
            "这会影响对通路富集结果的独立解释，建议作者核对并更正源数据。"
        ),
        "dimension_verdicts": {
            "image": "建议低风险",
            "data": "建议高风险",
            "ref": "建议低风险",
            "methodology": "建议低风险",
        },
        "evidence_images": [
            {
                "dimension": "data",
                "path": str(EVIDENCE_DIR / "reactome_duplicate_sd01.png"),
                "caption": "证据截图1：sd01第6–8行，三条不同通路的BgRatio、Count和geneID摘要校验值完全相同。",
            },
            {
                "dimension": "data",
                "path": str(EVIDENCE_DIR / "reactome_duplicate_sd02_sd03.png"),
                "caption": "证据截图2：sd02和sd03中多对不同通路重复相同的基因名单、背景比例和命中数。",
            },
        ],
        "_input_dir": str(BASE / "data/input/9月-八刊 - 通讯/10.1073_pnas.2535342123"),
        "_report_json": str(BASE / "data/output/yujing_4050_202607/init/10.1073_pnas.2535342123/report.json"),
        "_evidence_path": str(BASE / "data/output/review_v2/10.1073_pnas.2535342123_evidence.json"),
    }
    payload = json.dumps([result], ensure_ascii=False, indent=2)
    for destination in (
        OUT,
        ROUND / "results_judge/batch_1110003.json",
        ROUND / "apply_chunks/batch_1110003.json",
    ):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
