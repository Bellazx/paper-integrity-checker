"""The high-risk location gate must accept source-data locations as the v3
engine writes them (worksheet name, column letter, row range), not only
page/figure/table references."""
import importlib.util
import sys
from pathlib import Path

import pytest

from api.config import REVIEW_SKILL_SCRIPTS

# The API renders reports with the deployed skill script (api.config.REVIEW_SKILL_SCRIPTS);
# test the copy that actually runs.
SCRIPT = Path(REVIEW_SKILL_SCRIPTS) / "generate_review_report.py"
if not SCRIPT.is_file():
    pytest.skip(f"deployed report script not found: {SCRIPT}", allow_module_level=True)
SPEC = importlib.util.spec_from_file_location("generate_review_report_gate", SCRIPT)
grr = importlib.util.module_from_spec(SPEC)
sys.modules["generate_review_report_gate"] = grr
SPEC.loader.exec_module(grr)


BASE = {"doi": "", "result": "高风险", "_engine": "v3", "image_review": "本模块复核结论：建议低风险。",
        "ref_review": "本模块复核结论：建议低风险。", "methodology_review": "本模块复核结论：建议低风险。", "reason": "x"}


def _info(data_review):
    return dict(BASE, data_review="本模块复核结论：建议高风险。\n" + data_review)


def test_worksheet_column_and_row_locations_are_specific():
    sheet = ("（1）问题位置：Source data.xlsx / '1012 DSCLS analysis' 工作表 / ndcc 分组 / O 列 current_flow_betweenness\n"
             "问题：O 与 P 两列 323 行逐位相同\n影响：两个指标不能同时成立")
    grr._validate_explainable_highrisk_detail(_info(sheet))
    rows = "（1）问题位置：Source data.xlsx，表头第 4 行，数据第 5–327 行\n问题：两列相同\n影响：归属错误"
    grr._validate_explainable_highrisk_detail(_info(rows))


def test_label_only_location_is_still_rejected():
    with pytest.raises(ValueError, match="no specific page/figure/table"):
        grr._validate_explainable_highrisk_detail(_info("（1）问题位置：补充材料\n问题：两列相同\n影响：归属错误"))
