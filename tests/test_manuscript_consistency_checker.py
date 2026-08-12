from pathlib import Path

import fitz

from modules.chinese_report_generator import _compute_overall_risk
from modules.manuscript_consistency_checker import (
    _check_benchmark_summary_claims,
    _check_model_comparison_claims,
    _check_scenario_consistency,
    check_manuscript_consistency,
)


def _write_test_pdf(path: Path, *, broken_formula: bool = True) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    formula = (
        "-Pdown < Pdg(t+1) - Pdg(t+1) < Pup"
        if broken_formula else
        "-Pdown < Pdg(t+1) - Pdg(t) < Pup"
    )
    page.insert_text((70, 80), formula)
    page.insert_text((275, 80), "(3)")

    page.insert_text((90, 130), "TABLE II. CONFIGURATION RESULTS")
    page.insert_text((55, 155), "Parameter")
    page.insert_text((125, 155), "SES")
    page.insert_text((175, 155), "C")
    page.insert_text((215, 155), "SOCmin")
    page.insert_text((270, 155), "f")
    page.insert_text((60, 175), "Value")
    page.insert_text((123, 175), "kWh")
    page.insert_text((165, 175), "48.09 M¥")
    page.insert_text((220, 175), "0")
    page.insert_text((260, 175), "0.37")
    document.save(path)
    document.close()


def test_broken_constraint_and_unit_only_result_cell_are_high_risk(tmp_path: Path):
    pdf = tmp_path / "broken.pdf"
    _write_test_pdf(pdf)

    issues = check_manuscript_consistency(str(pdf))
    issue_types = {issue["test"] for issue in issues}

    assert "self_cancelling_constraint" in issue_types
    assert "table_numeric_value_missing" in issue_types
    assert all(issue["severity"] == "high" for issue in issues)

    overall = _compute_overall_risk({
        "paper": {"total_references": 0},
        "image_duplicates": [],
        "image_splicing": [],
        "data_anomalies": issues,
        "reference_issues": [],
    })
    assert overall["level"] == "高风险"


def test_valid_adjacent_time_constraint_is_not_flagged(tmp_path: Path):
    pdf = tmp_path / "valid.pdf"
    _write_test_pdf(pdf, broken_formula=False)

    issues = check_manuscript_consistency(str(pdf))

    assert not any(issue["test"] == "self_cancelling_constraint" for issue in issues)


def test_reversed_season_caption_and_prose_order_is_reported(tmp_path: Path):
    pdf = tmp_path / "plain.pdf"
    document = fitz.open()
    document.new_page()
    document.save(pdf)
    document.close()
    text = """Fig. 5. Typical summer daily power curve.
Fig. 6. Typical winter daily power curve.
Typical daily power curves are shown in Fig. 5 and Fig. 6.
On a typical winter day, PV output is high. On a typical summer day, load is high.
"""

    issues = check_manuscript_consistency(str(pdf), text)

    mismatch = [issue for issue in issues if issue["test"] == "figure_condition_order_mismatch"]
    assert mismatch and mismatch[0]["severity"] == "medium"


def _claim_tables(*, inconsistent: bool) -> tuple[list[dict], str]:
    table_one = {
        "page": 4,
        "name": "Table I",
        "rows": [
            ["DataSets", "Models", "Horizon 10"],
            [None, None, "MAE RMSE WMAPE"],
            ["A", "Baseline\nours", "0.7000 1.0100 0.4500\n0.6800 1.0000 0.4100"],
            ["B", "Baseline\nours", "0.7200 1.0200 0.5000\n0.7000 1.0700 0.4550"],
        ],
    }
    table_two = {
        "page": 5,
        "name": "Table II",
        "rows": [
            ["Settings", "Scens", "ML with 50 robots and 1000 tasks", None,
             "MM with 20 robots and 400 tasks", None, "MS with 30 robots and 200 tasks", None],
            [None, "Horizon", "classic", "ours", "classic", "ours", "classic", "ours"],
            [None, None, "-", "5 10 15", "-", "5 10 15", "-", "5 10 15"],
            ["Metrics", "ERR(%)", "40", "30 25 22", "36", "27 22 18", "32", "22 18 14"],
            [None, "MPT(mins)", "6", "5 4 3.3", "4", "3 2.4 2", "5", "3.5 2.8 2.2"],
            [None, "MTR(%)", "-", "0.2 0.8 1.1", "-", "0.1 0.5 0.6", "-", "0.2 0.4 1.25"],
        ],
    }
    mm_tasks, ms_tasks = ((200, 400) if inconsistent else (400, 200))
    table_four = {
        "page": 5,
        "name": "Table IV",
        "rows": [
            ["Maps", "nodes edges sectors tasks"],
            ["large map ML\nmiddle map MM\nsmall map MS",
             f"100 200 5 1000\n80 150 4 {mm_tasks}\n40 70 2 {ms_tasks}"],
        ],
    }
    rmse_claim = "5.3" if inconsistent else "0.99"
    range_claim, mtr_claim, prose_size = (
        ("50-60", "1", "smallest") if inconsistent else ("40-60", "1.3", "middle")
    )
    context = (
        f"At horizon 10, our method shows gains of up to 2.86%, {rmse_claim}%, and 9.0%. "
        f"MTR remains at or below about {mtr_claim}% while achieving {range_claim}% improvements in ERR and MPT. "
        f"The {prose_size} map MM is used for the main analysis."
    )
    return [table_one, table_two, table_four], context


def test_quantitative_claims_and_cross_table_scenarios_are_reported():
    tables, context = _claim_tables(inconsistent=True)
    issues = []
    issues.extend(_check_model_comparison_claims(tables[0], context))
    issues.extend(_check_benchmark_summary_claims(tables[1], context))
    issues.extend(_check_scenario_consistency(tables, {5: context}))
    by_type = {issue["test"]: issue for issue in issues}

    assert by_type["quantitative_claim_mismatch"]["severity"] == "high"
    assert by_type["scenario_definition_conflict"]["severity"] == "high"
    summary = by_type["quantitative_summary_mismatch"]
    discrepancy_types = {item["type"] for item in summary["details"]["discrepancies"]}
    assert discrepancy_types == {"improvement_range", "upper_bound"}


def test_supported_quantitative_claims_and_consistent_scenarios_are_not_reported():
    tables, context = _claim_tables(inconsistent=False)
    issues = []
    issues.extend(_check_model_comparison_claims(tables[0], context))
    issues.extend(_check_benchmark_summary_claims(tables[1], context))
    issues.extend(_check_scenario_consistency(tables, {5: context}))
    issue_types = {issue["test"] for issue in issues}

    assert "quantitative_claim_mismatch" not in issue_types
    assert "quantitative_summary_mismatch" not in issue_types
    assert "scenario_definition_conflict" not in issue_types
