from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "project_generate_review_report",
    Path(__file__).parents[1] / "scripts" / "generate_review_report.py",
)
assert _SPEC and _SPEC.loader
report_entrypoint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_entrypoint)
sys.path.insert(0, "/home/library/.claude/skills/paper-batch-review/scripts")
import coverage_validator


class _FakeRenderer:
    def _coverage_check(self, info):
        return {**info, "_coverage_error": "coverage timed out after 240s"}


class _GapRenderer:
    def _coverage_check(self, info):
        return {**info, "_coverage_gaps": ["未说明小数精度不一致类别（共1项）"]}


def test_feature_matrix_duplicate_coverage_accepts_specific_chinese_but_not_generic_text():
    from modules.review_coverage import validate
    evidence = {"high_findings": [{"dim": "data", "test": "feature_matrix_duplicate_columns"}],
                "deterministic_findings": {}, "main_figures": []}
    info = {"result": "高风险", "data_review": "数据均正常。"}
    _, gaps = validate(info, evidence)
    assert any("feature_matrix_duplicate_columns" in gap for gap in gaps)
    info["data_review"] = "特征矩阵重复列位于 FS21 的 B/F 列，均为 Magnetic field 自变量，属正常共享横轴。"
    _, gaps = validate(info, evidence)
    assert not any("feature_matrix_duplicate_columns" in gap for gap in gaps)


def test_resource_coverage_timeout_is_soft_warning(monkeypatch):
    monkeypatch.setenv("REVIEW_COVERAGE_FAILURE_POLICY", "warn")
    renderer = _FakeRenderer()
    report_entrypoint._patch_coverage_entrypoint(renderer)

    result = renderer._coverage_check({"doi": "10.1/example"})

    assert "_coverage_error" not in result
    assert result["_coverage_status"] == "resource_warning"
    assert "timed out" in result["_coverage_warning"]


def test_strict_policy_keeps_nonfatal_handling_opt_out(monkeypatch):
    monkeypatch.setenv("REVIEW_COVERAGE_FAILURE_POLICY", "strict")
    renderer = _FakeRenderer()
    report_entrypoint._patch_coverage_entrypoint(renderer)

    result = renderer._coverage_check({"doi": "10.1/example"})

    assert result["_coverage_error"] == "coverage timed out after 240s"


def test_semantic_coverage_gaps_remain_blocking_even_if_legacy_override_is_set(monkeypatch):
    monkeypatch.setenv("REVIEW_COVERAGE_GAP_POLICY", "warn")
    renderer = _GapRenderer()
    report_entrypoint._patch_coverage_entrypoint(renderer)

    result = renderer._coverage_check({"doi": "10.1/example"})

    assert result["_coverage_gaps"] == ["未说明小数精度不一致类别（共1项）"]
    assert result.get("_coverage_status") != "warning"


def test_highrisk_report_must_not_invent_omitted_rowwise_acknowledgement():
    info = {
        "doi": "10.7554/eLife.97621",
        "result": "高风险",
        "verdict": "建议高风险",
        "image_review": "已核查图像，未见独立问题。",
        "data_review": "已核查数据并确认存在高风险异常。",
        "ref_review": "已核查参考文献，未见独立问题。",
        "methodology_review": "已核查研究设计和统计事实。",
        "reason": "数据异常支持高风险结论。",
        "dimension_verdicts": {
            "image": "建议低风险", "data": "建议高风险",
            "ref": "建议低风险", "methodology": "建议低风险",
        },
    }
    evidence = {
        "high_findings": [{
            "dim": "data", "test": "rowwise_value_reuse",
            "location": "elife-97621-fig4-data3-v1.xlsx / Figure 4I",
        }],
        "deterministic_findings": {},
        "main_figures": [],
    }
    from api.services.review_validation import validate
    checked, gaps = validate(info, evidence)
    assert any("rowwise_value_reuse" in gap for gap in gaps)
    assert checked['data_review'] == info['data_review']


def test_grouped_duplicate_coverage_accepts_explicit_sheet_range():
    from modules.review_coverage import validate

    info = {
        "result": "高风险",
        "verdict": "建议高风险",
        "image_review": "已核对图像重复线索，未见独立异常。",
        "data_review": "Supplementary data 中 ST13 至 ST16 的重复列整类已核查；这些稀疏指示列的重叠属可解释的正常结构，不构成独立测量值复用。",
        "ref_review": "已核对参考文献著录，未见实质不一致。",
        "methodology_review": "已核对研究设计、样本数和统计口径。",
        "reason": "综合判定依据为已确认的数据问题。",
        "dimension_verdicts": {
            "image": "建议低风险", "data": "建议高风险",
            "ref": "建议低风险", "methodology": "建议低风险",
        },
    }
    evidence = {
        "deterministic_findings": {
            "duplicate_column_pairs": [
                {"file": "MOESM3.xlsx", "sheet": "ST14", "col_a_header": "a", "col_b_header": "b"},
                {"file": "MOESM3.xlsx", "sheet": "ST15", "col_a_header": "c", "col_b_header": "d"},
            ]
        },
        "high_findings": [],
        "main_figures": [],
    }
    _, gaps = validate(info, evidence)
    assert not any("未说明重复列对" in gap for gap in gaps)


def test_sheet_range_match_requires_matching_prefix_and_bounds():
    from modules.review_coverage import _mentions_sheet_token

    assert _mentions_sheet_token("ST13 至 ST16 的重复列已核查", "st14")
    assert not _mentions_sheet_token("ST13 至 ST16 的重复列已核查", "st17")
    assert not _mentions_sheet_token("ST13 至 ST16 的重复列已核查", "supplementary14")
