import json
from pathlib import Path

from api.services.review import _augment_evidence_with_cif_findings
from modules.data_checker import check_data_with_validation


VALID_CIF = """data_demo
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_cell_volume 1000
_space_group_name_H-M_alt 'P 1'
_chemical_formula_sum 'C2 H6 O'
_chemical_formula_weight 46.068
_cell_formula_units_Z 4
_exptl_crystal_density_diffrn 0.306
_refine_ls_R_factor_gt 0.035
_refine_ls_wR_factor_ref 0.091
_refine_ls_goodness_of_fit_ref 1.05
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
_atom_site_U_iso_or_equiv
C1 C 0.1 0.2 0.3 1.0 0.02
C2 C 0.2 0.3 0.4 1.0 0.03
"""


ANOMALOUS_CIF = """data_bad_structure
_cell_length_a 10
_cell_length_b 10
_cell_length_c 10
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
_cell_volume 800
_space_group_name_H-M_alt 'P 1'
_chemical_formula_sum 'C2 H6 O'
_chemical_formula_weight 100
_refine_ls_R_factor_gt 0.65
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
_atom_site_occupancy
_atom_site_U_iso_or_equiv
C1 C 0.1 0.2 0.3 1.2 -0.01
C2 C 0.1 0.2 0.3 1.0 0.02
"""


def test_valid_cif_is_checked_without_false_positive(tmp_path: Path):
    (tmp_path / "valid.cif").write_text(VALID_CIF, encoding="utf-8")

    anomalies, failed = check_data_with_validation(tmp_path)

    assert anomalies == []
    assert failed == []


def test_invalid_cif_is_reported_as_failed_input(tmp_path: Path):
    (tmp_path / "broken.cif").write_text("data_broken\n_tag 'unterminated\n", encoding="utf-8")

    anomalies, failed = check_data_with_validation(tmp_path)

    assert [item["test"] for item in anomalies] == ["cif_read_error"]
    assert failed == ["broken.cif"]


def test_cif_structural_inconsistencies_are_detected(tmp_path: Path):
    (tmp_path / "anomalous.cif").write_text(ANOMALOUS_CIF, encoding="utf-8")

    anomalies, failed = check_data_with_validation(tmp_path)
    tests = {item["test"] for item in anomalies}

    assert failed == []
    assert {
        "cif_cell_volume_mismatch",
        "cif_duplicate_fractional_coordinates",
        "cif_invalid_occupancy",
        "cif_negative_displacement_parameter",
        "cif_formula_weight_mismatch",
        "cif_poor_refinement_quality",
    }.issubset(tests)


def test_cif_findings_are_added_to_review_evidence(tmp_path: Path):
    input_dir = tmp_path / "paper"
    input_dir.mkdir()
    (input_dir / "structure.cif").write_text(ANOMALOUS_CIF, encoding="utf-8")
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "data_anomalies": [{
            "test": "cif_invalid_occupancy",
            "location": "structure.cif / data_bad_structure",
            "severity": "high",
            "details": {"invalid_site_count": 1},
            "description": "invalid occupancy",
        }],
    }), encoding="utf-8")
    evidence_path = tmp_path / "review_evidence.json"
    evidence_path.write_text(json.dumps({
        "high_findings": [],
        "deterministic_findings": {},
        "coverage_manifest": {"must_address": [], "counts": {}},
    }), encoding="utf-8")

    _augment_evidence_with_cif_findings(evidence_path, str(report_path), str(input_dir))

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    cif_review = evidence["deterministic_findings"]["cif_review"]
    assert cif_review["files"] == ["structure.cif"]
    assert cif_review["findings"][0]["test"] == "cif_invalid_occupancy"
    assert evidence["high_findings"][0]["source"] == "cif_checker"
    assert evidence["coverage_manifest"]["must_address"] == ["high::data::0"]
    assert evidence["coverage_manifest"]["counts"]["cif_high"] == 1
