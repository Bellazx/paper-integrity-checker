#!/usr/bin/env python3
"""Repair the contradictory dimension verdict in one 4050 review result."""

from __future__ import annotations

import json
from pathlib import Path


RESULT_PATH = Path(
    "/home/sjtu/paper-integrity-checker/"
    "data/yujing_4050_202607_round_1/results_judge/batch_501063.json"
)
DOI = "10.1016/j.cmet.2021.09.008"

STRUCTURED_PREFIX = """问题位置：mmc3-mmc6 的38张rMATS工作表，代表性位置为mmc6 / Table S6N / 第2-5行Cfh剪接事件。

问题类型：IJC/SJC原始计数字段出现物理上不可能的大数值，并与同行IncLevel结果不自洽。

具体情况：109630个单元格以数值类型存储1×10^17至1×10^35的值；以Table S6N第2行Cfh为例，按IJC/SJC反算PSI为6.76e-5，而表中IncLevel1为0.403。正常字符串格式的Sntg2行则可逐位复算一致。

需关注原因：异常覆盖论文Fig 5/6所依赖的RNA-seq alternative splicing源数据，使公开材料无法支持读者独立核验junction read count。

核查依据：直接读取mmc3-mmc6的xlsx/XML底层单元格，统计字段类型与数值范围，并按rMATS长度归一化公式逐行反算PSI。"""


def main() -> None:
    results = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    matches = [item for item in results if item.get("doi") == DOI]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {DOI} result, found {len(matches)}")

    item = matches[0]
    item["dimension_verdicts"] = {
        "image": "建议低风险",
        "data": "建议高风险",
        "ref": "建议低风险",
        "methodology": "建议低风险",
    }
    if not str(item.get("data_review") or "").startswith("问题位置："):
        item["data_review"] = STRUCTURED_PREFIX + "\n\n" + item["data_review"]

    RESULT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
