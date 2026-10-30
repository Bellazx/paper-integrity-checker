from __future__ import annotations

import json
import zipfile
from pathlib import Path

import fitz
import pytest

from modules import quantitative_consistency as qc
from modules.chinese_report_generator import _compute_overall_risk, _format_data_findings
from modules.data_checker import _load_data_files
from utils import delimited_source as ds
from utils.safe_data_checker import check_data_anomalies_safe


def assessment(issues):
    return _compute_overall_risk({"paper": {}, "image_duplicates": [],
                                 "data_anomalies": issues, "reference_issues": []})


def test_salt_recalculation_and_corrected_concentration():
    text = ("Two pools (3.1 wt% NaCl of aqueous solutions) where each NaCl solution contains "
            "1230 water molecules, 6Na+, and 6Cl-.")
    issues = qc.mass_fraction(text, 2)
    assert len(issues) == 1
    assert issues[0]["details"]["calculated_percent"] == pytest.approx(1.55775, abs=.00001)
    assert assessment(issues)["level"] == "高风险"
    assert not qc.mass_fraction(text.replace("3.1", "1.56"), 2)
    assert not qc.mass_fraction(text.replace("wt%", "mol%"), 2)
    assert not qc.mass_fraction(text.replace("each NaCl solution", "the hydrate"), 2)


@pytest.mark.parametrize("pairs", [3, 8, 24])
def test_salt_counts_are_computed_not_memorised(pairs):
    pct = 100 * pairs * 58.44 / (pairs * 58.44 + 1000 * 18.01528)
    text = f"{pct:.3f} wt% NaCl where each NaCl solution contains 1000 water molecules, {pairs}Na+, and {pairs}Cl-."
    assert not qc.mass_fraction(text, 1)
    assert qc.mass_fraction(text.replace(f"{pct:.3f}", f"{pct * 2:.3f}"), 1)


def test_elastic_energy_requires_parent_and_wrong_child():
    parent = "Ue = 1 2E [σ2 1 + σ2 2 + σ2 3 − 2μ(σ1σ2 + σ2σ3 + σ1σ3)] (7)"
    child = "Ue = 1 2E [σ2 1 + 2σ2 3 + 2(1 − μ)σ2 3 − 4μσ1σ3] (8)"
    assert qc.elastic_reduction("triaxial " + parent + child, 4)[0]["details"]["extra_coefficient"] == 2
    assert not qc.elastic_reduction("triaxial " + child, 4)
    assert not qc.elastic_reduction("triaxial " + parent + child.replace("+ 2σ2 3 ", ""), 4)
    assert not qc.elastic_reduction(parent + child, 4)


def test_unit_comparison_requires_same_quantity_and_unconverted_tick_values():
    table = "Gas\nconcentration\n/‰. The methane mole fraction was varied from 3.5‰ to 6.6‰."
    axis = "3.5\n4.0\n4.5\n5.0\n5.5\n6.0\n6.5\nGas concentration/%"
    assert qc.unit_conflicts([table, axis])
    assert not qc.unit_conflicts([table, axis.replace("%", "‰")])
    assert not qc.unit_conflicts([table, "0.35 0.4 0.45 0.5 0.55 0.6 0.65 Gas concentration/%"])
    assert not qc.unit_conflicts([table, axis.replace("Gas concentration", "Water conversion")])
    assert not qc.unit_conflicts([table + " Converted to percent for the figure.", axis])


def test_velocity_range_matches_table_and_tolerates_rounding():
    table = ("Table 4. Experimental results. Hydrate Saturation (%) Methane Hydrate Samples "
             "THF Hydrate Samples Vp (m/s) Vs (m/s) Vp (m/s) Vs (m/s) "
             "60 3000 1500 2600 1300 80 3400 1700 3100 1600 100 3500 1750 3800 2100 ")
    claim = "At 80–100% saturation, the THF hydrate samples exhibit higher velocities for both P waves and S waves compared to methane hydrate samples."
    assert qc.velocity_claims([table, claim])[0]["details"]["rows"][0][0] == 80
    assert not qc.velocity_claims([table, claim.replace("80–100", "100–100")])
    assert not qc.velocity_claims([table.replace("3100 1600", "3399 1699"), claim])
    assert not qc.velocity_claims([table.replace("THF Hydrate Samples", "Predicted Samples"), claim])


def test_weighted_matrix_geometry_and_corrected_result():
    doc = fitz.open()
    page = doc.new_page()
    # The rule consumes geometry for matrix cells and final-weight column, plus
    # explicit semantic anchors. No known paper's numbers are needed.
    page.insert_text((50, 120), "R=")
    for y, values in [(105, [.1, .2, .3]), (125, [.4, .5, .6])]:
        for x, v in zip((100, 150, 200), values):
            page.insert_text((x, y), str(v))
    page.insert_text((350, 100), "最终权重", fontname="china-s", fontsize=10)
    page.insert_text((360, 120), "0.4")
    page.insert_text((360, 140), "0.6")
    pages = ["隶属度矩阵 最终权重 评判结果矩阵B = ［0.40，0.50，0.60］"]
    issues = qc.weighted_evaluation(doc, pages)
    assert issues[0]["details"]["calculated"] == pytest.approx([.28, .38, .48])
    assert not qc.weighted_evaluation(doc, [pages[0].replace("0.40，0.50，0.60", "0.280，0.380，0.480")])
    doc.close()


def test_txt_tsv_and_zip_loading_and_non_table_warning(tmp_path):
    text = "group\tmeasurement\nA\t1.25\nB\t2.78\n"
    (tmp_path / "table.txt").write_text(text)
    (tmp_path / "table.tsv").write_text(text + "C\t3.34\n")
    (tmp_path / "readme.txt").write_text("Supplementary information\nSee the paper.\n")
    (tmp_path / "doi.txt").write_text("10.1000/example")
    with zipfile.ZipFile(tmp_path / "tables.zip", "w") as archive:
        archive.writestr("sub/table.tsv", text)
    loaded, failed, _ = _load_data_files(tmp_path)
    assert {"table.txt", "table.tsv", "tables.zip/sub/table.tsv"} <= set(loaded)
    assert loaded["table.txt"]["Sheet1"]["measurement"].tolist() == [1.25, 2.78]
    assert any("readme.txt" in f for f in failed)
    assert not any("doi.txt" in f for f in failed)


def test_txt_utf16_and_invalid_duplicate_headers(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("组别\t测量\n甲\t1.2\n乙\t4.3\n", encoding="utf-16")
    frame, warnings = ds.read_screen_table(path)
    assert frame["测量"].tolist() == [1.2, 4.3]
    assert not warnings
    path.write_text("value\tvalue\n1\t2\n")
    with pytest.raises(ValueError, match="列名"):
        ds.read_screen_table(path)


def test_large_txt_read_is_bounded_and_discloses_partial_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "MAX_SCREEN_ROWS", 2)
    monkeypatch.setattr(ds, "MAX_SCREEN_COLUMNS", 2)
    path = tmp_path / "source.txt"
    path.write_text("a\tb\tc\n1\t2\t3\n4\t5\t6\n7\t8\t9\n")
    frame, warnings = ds.read_screen_table(path)
    assert frame.shape == (2, 2)
    assert "不能视为全表核验" in warnings[0]


def _protein_table(path, count, *, contaminant=0):
    lines = ["Protein IDs\tIntensity\tReverse\tPotential contaminant\tOnly identified by site"]
    lines += [f"P{i}\t1\t\t\t" for i in range(count)]
    lines += [f"C{i}\t100\t\t+\t" for i in range(contaminant)]
    path.write_text("\n".join(lines) + "\n")


def test_protein_counts_need_two_positive_controls_and_read_all_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "MAX_SCREEN_ROWS", 2)
    for i, n in [(1, 10), (2, 20), (3, 30)]:
        _protein_table(tmp_path / f"Supplemental Table S{i}.txt", n, contaminant=3)
    pages = [f"In total, we quantified {n} proteins and 60 peptides (Supplementary Tables S{i} and S9)."
             for i, n in [(1, 15), (2, 20), (3, 30)]]
    issues, warnings = qc.protein_claims(pages, tmp_path)
    assert not warnings
    assert len(issues) == 1
    assert issues[0]["details"]["comparison"]["count"] == 10
    assert not qc.protein_claims(pages[:2], tmp_path)[0]
    assert not qc.protein_claims([s.replace("15 proteins", "10 proteins") for s in pages], tmp_path)[0]


def test_read_failures_survive_both_worker_paths(tmp_path, monkeypatch):
    (tmp_path / "unreadable.txt").write_text("not a table\n")
    from utils import safe_data_checker as safe
    monkeypatch.setattr(safe, "ISOLATION_FILE_COUNT", 100)
    issues, warnings = check_data_anomalies_safe(tmp_path)
    assert not issues and any("unreadable.txt" in w for w in warnings)
    monkeypatch.setattr(safe, "ISOLATION_FILE_COUNT", 1)
    issues, warnings = check_data_anomalies_safe(tmp_path)
    assert not issues and any("unreadable.txt" in w for w in warnings)


def test_decisive_finding_not_hidden_by_weak_alert_volume():
    issue = qc.finding("molecular_mass_fraction_mismatch", 2, "盐浓度计算差异：1.56%与3.1%不符。")
    others = [{"test": "decimal_uniformity", "severity": "high", "location": "data",
               "description": "weak signal", "details": {}} for _ in range(150)]
    assert "盐浓度计算差异" in _format_data_findings([*others, issue])
    assert assessment([{**issue, "severity": "low"}])["level"] == "低风险"


def test_report_explanation_failure_does_not_erase_quantitative_evidence(monkeypatch):
    from modules import chinese_report_generator as report
    issue = qc.finding("molecular_mass_fraction_mismatch", 2, "1230个水分子和6对NaCl应为1.56%，标注3.1%。",
                       source_pdf="paper.pdf")
    findings = {"paper": {}, "image_duplicates": [], "data_anomalies": [issue], "reference_issues": []}
    monkeypatch.setattr(report, "chat", lambda *a, **k: "broken response")
    monkeypatch.setattr(report, "_parse_combined_report_response", lambda s: ({"authors_full": [], "affiliations": []}, "<p>不完整说明</p>"))
    _, html = report._generate_analysis_html(findings, "")
    assert "1.56%" in html
    assert not report._analysis_explanation_gaps(html, "高风险", {"data"})
    assert not findings.get("_explainability_downgraded")
    assert assessment([issue])["level"] == "高风险"


def test_shared_cli_pipeline_persists_new_findings_and_coverage(tmp_path, monkeypatch):
    import main
    from core import pipeline
    from utils import pdf_utils, pdf_table_extractor, docx_extractor, safe_data_checker
    root = tmp_path / "input"
    root.mkdir()
    doc = fitz.open()
    pg = doc.new_page()
    pg.insert_textbox(fitz.Rect(40, 40, 500, 500),
                      "Table 1. Methods.\n3.1 wt% NaCl solution, where each NaCl solution contains "
                      "1230 water molecules, 6Na+, and 6Cl-.")
    doc.save(root / "paper.pdf")
    doc.close()
    monkeypatch.setenv("PAPER_CLEAN_JUNK", "0")
    monkeypatch.setattr(pipeline, "extract_metadata", lambda p: {"title": "Experimental methods", "journal": "Example"})
    monkeypatch.setattr(pipeline, "extract_images", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "check_image_duplicates", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "check_splicing", lambda *a, **k: [])
    monkeypatch.setattr(pdf_utils, "extract_standalone_images", lambda *a, **k: [])
    monkeypatch.setattr(docx_extractor, "extract_docx_images_in_dir", lambda *a, **k: [])
    monkeypatch.setattr(pdf_table_extractor, "extract_tables_from_pdf", lambda *a, **k: [])
    monkeypatch.setattr(safe_data_checker, "check_data_anomalies_safe", lambda *a: ([], ["test incomplete source coverage"]))
    monkeypatch.setattr(pipeline, "generate_chinese_pdf", lambda *a: ("test.pdf", {}))
    monkeypatch.setattr(pipeline, "screen_manuscript", lambda *a: ([], [], {"status": "complete"}))
    # main._analyze_one is also the API detection subprocess's dispatch path.
    result = main._analyze_one(str(root), str(tmp_path / "output"), skip_refs=True,
                               chinese_reports_dir=str(tmp_path / "pdf"))
    saved = json.loads((tmp_path / "output/report.json").read_text())
    assert result["initial_risk_level"] == saved["initial_risk_level"] == "高风险"
    assert any(x["test"] == "molecular_mass_fraction_mismatch" for x in saved["data_anomalies"])
    assert "test incomplete source coverage" in saved["analysis_warnings"]
    assert any("未提取到结构化" in x for x in saved["analysis_warnings"])


# Optional integration cases use original input files only. They never read
# archived review decisions, regenerate reports, or write to a database.
ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("胡高伟/10.1016_j.fuel.2021.121866", "molecular_mass_fraction_mismatch"),
    ("胡高伟/10.1016_j.oceaneng.2025.123854", "elastic_energy_reduction_mismatch"),
    ("胡高伟/10.19657_j.geoscience.1000－8527.2021.173", "weighted_evaluation_mismatch"),
    ("胡高伟/10.31035_cg2022015", "same_quantity_unit_conflict"),
    ("胡高伟/10.3390_jmse12122239", "velocity_range_claim_mismatch"),
    ("王丹丹/10.1093_jmcb_mjac004", "source_protein_count_mismatch"),
]


@pytest.mark.parametrize("folder,test_name", CASES)
def test_original_six_papers(folder, test_name):
    root = ROOT / "data/input/scholar" / folder
    pdfs = list(root.glob("*.pdf"))
    if len(pdfs) != 1:
        pytest.skip("original input fixture unavailable")
    issues, warnings = qc.check_quantitative_consistency(str(pdfs[0]), root)
    matching = [x for x in issues if x["test"] == test_name]
    assert matching
    assert not warnings
    assert assessment(matching)["level"] == "高风险"
