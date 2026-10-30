#!/usr/bin/env python3
"""Add reader-facing structured labels required by the PDF consistency gate.

This only makes the existing eLife high-risk evidence explicit; it does not
change its verdict or any underlying finding.
"""
from __future__ import annotations

import json
from pathlib import Path


PATH = Path("data/output/yujing_scholar/review_results_sumengyue.json")
PREFIX = (
    "问题位置：Figure 2C/2D 行42-45、Figure 1M 的 CsCl/KCl/NaCl 列、Figure 3C radius 列。"
    "问题类型：跨实验末段重复、测量值网格化以及组内零变异。"
    "具体情况：Figure 2C 与 Figure 2D 的独立处理列末4行逐位相同；Figure 1M 直径列仅有少量固定步长值；"
    "Figure 3C 的4725个测量仅15个唯一值且大量数据组内完全相同。"
    "为什么重要：这些非基线独立实验中的重复和规整分布难以由共享对照、正常像素量化或生物学变异解释，"
    "足以独立支持建议高风险。"
    "核查依据：已用 openpyxl 逐表逐列核对原始 xlsx，计算 Pearson r、斜率、唯一值数量及组内变异，"
    "并与同论文 Figure 1C/1K 的连续分布和论文图注逐项对照。\n\n"
)


def main() -> None:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    changed = 0
    for item in data:
        if item.get("doi") != "10.7554/eLife.97621":
            continue
        old = str(item.get("data_review") or "")
        if not old.startswith("问题位置：Figure 2C/2D"):
            item["data_review"] = PREFIX + old
            changed += 1
    if changed:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated={changed}")


if __name__ == "__main__":
    main()
