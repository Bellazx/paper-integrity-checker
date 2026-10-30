import math

from modules.chinese_report_generator import (
    _compute_dimension_risk,
    _compute_overall_risk,
    _normalize_analysis_consistency,
)
from modules.citation_content_checker import _entity_type_conflicts
from modules.source_data_patterns import detect_source_data_patterns
from modules.data_checker import _suppress_redundant_transform_findings
from modules.supplementary_consistency_checker import (
    _check_capacity_share_consistency,
    _check_sign_polarity_blocks,
)
from utils.db import _select_initial_report_url


def _findings(*, data=None, references=None):
    return {
        "paper": {"total_references": 6},
        "image_duplicates": [],
        "image_splicing": [],
        "data_anomalies": data or [],
        "reference_issues": references or [],
    }


def test_log2fc_copied_into_fc_is_a_decisive_data_finding():
    grid = [["gene", "avg_log2FC", "FC"]]
    grid.extend([[f"G{i}", -2 + i / 20, -2 + i / 20] for i in range(30)])

    result = detect_source_data_patterns("table_s5.xlsx", "Sheet1", grid)
    issues = result["semantic_transform_conflicts"]

    assert len(issues) == 1
    assert issues[0]["details"]["row_count"] == 30
    assert issues[0]["details"]["negative_fc_count"] > 0
    assert _compute_dimension_risk(issues)["score"] >= 61
    overall = _compute_overall_risk(_findings(data=issues))
    assert overall["level"] == "高风险"
    assert overall["high_dimensions"] == {"data"}


def test_same_repeat_different_channels_are_a_decisive_finding():
    channel_a = "normalized fluorescence TSPAN4-GFP"
    channel_b = "normalized fluorescence mCherry-NT-Lys"
    values = [9.0] + [0.10 + i * 0.0137 for i in range(33)]
    grid = [
        [None] * 45,
        ["Repeat 1"] + [None] * 44,
        [None, channel_a] + [None] * 19 + [channel_b] + [None] * 23,
        ["time point"] + list(range(1, 21)) + [None] * 1 + list(range(1, 21)) + [None] * 3,
    ]
    # The checker compares aligned numeric cells; put the same measurement vector
    # in two distinct, widely separated channel blocks.
    for value in values:
        grid.append([None] + [value] * 20 + [None] + [value] * 20 + [None] * 3)

    result = detect_source_data_patterns("source.xlsx", "fig3e", grid)
    issues = result["same_sheet_channel_duplicate"]

    assert issues
    assert issues[0]["severity"] == "high"
    assert issues[0]["details"]["n_values"] >= 15
    assert issues[0]["details"]["channel_a"] != issues[0]["details"]["channel_b"]
    overall = _compute_overall_risk(_findings(data=issues))
    assert overall["level"] == "高风险"


def test_same_repeat_axis_columns_are_not_channel_duplicates():
    grid = [
        [None] * 12,
        ["Repeat 1"] + [None] * 11,
        [None, "time A"] + [None] * 4 + ["time B"] + [None] * 5,
        ["time"] + list(range(1, 6)) + [None] + list(range(1, 6)) + [None] * 5,
    ]
    for i in range(20):
        grid.append([None] + [float(i)] * 5 + [None] + [float(i)] * 5 + [None] * 5)

    result = detect_source_data_patterns("source.xlsx", "fig3e", grid)
    assert result["same_sheet_channel_duplicate"] == []


def test_main_text_share_disagrees_with_one_supplementary_area():
    main = (
        "The total technical potential is 3.37 GW at a rated power density of 200 W/m2. "
        "The total 3.37 GW is distributed across Egypt (3.48%), Sudan (0.45%), "
        "and Chad (1.00%)."
    )
    supplement = """Table S3. Country area (m2)
Egypt 3.75E+04
Sudan 75825
Chad 168500
Kenya 1000
Niger 2000
"""

    issues = _check_capacity_share_consistency(main, supplement, "supplement.pdf")

    assert len(issues) == 1
    assert issues[0]["details"]["entity"] == "Egypt"
    assert math.isclose(issues[0]["details"]["expected_area_m2"], 586380)


def test_isolated_consecutive_sign_reversal_is_decisive():
    rows = [f"sample{i} -1.1 -2.2 -3.3" for i in range(50)]
    rows[15:20] = [f"weightlifting{i} 4.16 4.80 5.56" for i in range(5)]
    page = "Table S2. Signal change measurement\n" + "\n".join(rows)

    issues = _check_sign_polarity_blocks([page], "measurements.pdf")

    assert len(issues) == 1
    assert issues[0]["details"]["total_values"] == 150
    assert issues[0]["details"]["dominant_count"] == 135
    assert issues[0]["details"]["block_values"] == 15


def test_systematic_subtype_entity_switch_is_a_decisive_reference_finding():
    citing = (
        "AD3 LAMs are inflammatory. AD3 macrophages persist. "
        "AD5 LAMs handle lipid. AD5 macrophages are protective."
    )
    cited = (
        "AD3 adipocytes are stressed. AD3 fat cells show lower turnover. "
        "AD5 adipocytes synthesize lipid. AD5 fat cells decrease in obesity."
    )

    conflicts = _entity_type_conflicts(citing, cited)
    issue = {
        "issue_type": "citation_entity_type_conflict",
        "severity": "high",
        "details": {"conflicts": conflicts},
    }

    assert {item["token"] for item in conflicts} == {"AD3", "AD5"}
    assert _compute_dimension_risk([issue])["score"] >= 61
    overall = _compute_overall_risk(_findings(references=[issue]))
    assert overall["level"] == "高风险"
    assert overall["score"] >= 61
    assert overall["high_dimensions"] == {"reference"}


def test_report_narrative_uses_deterministic_dimension_levels_and_issue_count():
    data_issue = {
        "test": "cross_document_quantitative_mismatch",
        "severity": "high",
    }
    findings = _findings(data=[data_issue])
    findings["summary"] = {"total_issues": 5}
    html = (
        "检测范围覆盖图像、数据及参考文献，共发现7处问题。"
        "图像问题维度为中等风险，数据分析维度为低风险。"
    )

    normalized = _normalize_analysis_consistency(findings, html)

    assert "共发现5处问题" in normalized
    assert "图像问题维度为低风险" in normalized
    assert "数据分析维度为高风险" in normalized
    assert "中等风险" not in normalized


def test_semantic_transform_replaces_generic_duplicate_column_alerts():
    semantic = {
        "test": "semantic_transform_conflict",
        "location": "table_s5.xlsx / Sheet1 / rows 3-975",
        "severity": "high",
        "details": {"left_header": "avg_log2FC", "right_header": "FC"},
    }
    duplicate = {
        "test": "feature_matrix_duplicate_columns",
        "location": "table_s5.xlsx / Sheet1",
        "severity": "high",
        "details": {"duplicate_column_groups": [["avg_log2FC", "FC"]]},
    }
    correlated = {
        "test": "feature_matrix_near_identical_columns",
        "location": "table_s5.xlsx / Sheet1",
        "severity": "medium",
        "details": {"pairs": [{"col_a": "avg_log2FC", "col_b": "FC"}]},
    }

    assert _suppress_redundant_transform_findings([semantic, duplicate, correlated]) == [semantic]


def test_review_report_link_does_not_promote_a_low_initial_result():
    generated = "http://example.test/initial.pdf"
    reviewed = "http://example.test/review.pdf"

    assert _select_initial_report_url(generated, "低风险", "高风险", reviewed) == generated
    assert _select_initial_report_url(generated, "高风险", "高风险", reviewed) == reviewed
    assert _select_initial_report_url(generated, "高风险", "低风险", reviewed) == generated
