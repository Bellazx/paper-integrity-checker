import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

from api.services.review import _source_data_audit_highrisk_review_result
from api.services.source_data_audit import audit_source_data
from modules.chinese_report_generator import _compute_overall_risk
from modules.data_checker import check_data_anomalies
from modules.source_data_patterns import detect_source_data_patterns


def _write_ratio_workbook(path: Path) -> None:
    workbook = Workbook()
    fig_s7 = workbook.active
    fig_s7.title = "FigS7"
    fig_s7["A1"] = "S7C"
    fig_s7["A2"] = "Day"
    day10 = [100.1234 + index * 7.25 for index in range(10)]
    day13 = [value * 1.04666667 for value in day10]
    day13[-1] = day13[-1] * 0.82
    fig_s7.append([10, *day10])
    fig_s7.append([13, *day13])

    fig_5 = workbook.create_sheet("Fig5")
    fig_5["A1"] = "Figure5H"
    fig_5["A2"] = "AUH/uM"
    fig_5.append([1.0, 7148.083, 9990.167, 8451.5])
    fig_5.append([0.117188, 2859.2332, 3996.0668, 3380.6])
    workbook.save(path)


def _write_exact_reuse_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Fig.7"
    basal = [27.05048, 27.18267, 23.39779, 33.85244, 23.83982, 26.90849, 21.29766, 19.24133]
    iso = [53.58603, 46.67533, 48.18494, 47.12164, 35.5969, 35.1361, 36.08544, 31.47437]
    sheet.append(["Fig.7e"])
    sheet.append(["Basal", *basal])
    sheet.append(["ISO", *iso])
    sheet.append([])
    sheet.append(["Fig.7f"])
    sheet.append(["Basal", *basal])
    sheet.append(["ISO", *iso])
    workbook.save(path)


class SourceDataPatternIntegrationTests(unittest.TestCase):
    def test_wide_table_channel_scan_is_linear(self):
        width = 1200
        grid = [[f"signal_{column}" for column in range(width)]]
        for row in range(40):
            grid.append([column + row / 1000 for column in range(width)])

        started = time.monotonic()
        result = detect_source_data_patterns("wide.csv", "Fig.5B", grid)
        elapsed = time.monotonic() - started

        self.assertEqual([], result["same_sheet_channel_duplicate"])
        self.assertLess(elapsed, 2.5)

    def test_matrix_fastpath_carries_structured_excel_evidence_regions(self):
        audit = {
            "version": "2",
            "high_findings": [{"test": "rectangular_matrix_reuse"}],
            "rectangular_matrix_reuse": [{
                "severity": "high",
                "location": "source.xlsx / Fig2 R2:R4 C2:C3 vs source.xlsx / Fig3 R8:R10 C4:C5",
                "description": "两个3x2矩阵逐格完全相同",
                "n_values": 6,
                "sample": [1, 2, 3],
                "left": {
                    "file": "source.xlsx", "sheet": "Fig2",
                    "row_range": [2, 4], "col_range": [2, 3],
                },
                "right": {
                    "file": "source.xlsx", "sheet": "Fig3",
                    "row_range": [8, 10], "col_range": [4, 5],
                },
            }],
        }
        review = _source_data_audit_highrisk_review_result(
            "10.0000/test-matrix",
            {"deterministic_findings": {"source_data_audit": audit}},
        )

        self.assertEqual(1, len(review["excel_evidence_regions"]))
        regions = review["excel_evidence_regions"][0]["regions"]
        self.assertEqual([2, 4], regions[0]["row_range"])
        self.assertEqual("Fig3", regions[1]["sheet"])

    def test_near_modified_matrix_fastpath_carries_excel_evidence_regions(self):
        audit = {
            "version": "3",
            "high_findings": [{"test": "near_modified_matrix_reuse"}],
            "near_modified_matrix_reuse": [{
                "severity": "high",
                "location": "source.xlsx / EDFig2 R56:R65 C6:C8 vs source.xlsx / SuppFig3 R33:R42 C4:C6",
                "description": "两个10x3矩阵的前两位小数一致，但后续小数位不同",
                "n_values": 30,
                "matching_prefix_count": 30,
                "changed_count": 30,
                "sample_pairs": [{"left": 2.734738, "right": 2.733577}],
                "left": {
                    "file": "source.xlsx", "sheet": "EDFig2",
                    "row_range": [56, 65], "col_range": [6, 8],
                },
                "right": {
                    "file": "source.xlsx", "sheet": "SuppFig3",
                    "row_range": [33, 42], "col_range": [4, 6],
                },
            }],
        }

        review = _source_data_audit_highrisk_review_result(
            "10.0000/test-near-modified",
            {"deterministic_findings": {"source_data_audit": audit}},
        )

        self.assertIn("30/30", review["reason"])
        self.assertIn("2.734738", review["data_review"])
        self.assertEqual(1, len(review["excel_evidence_regions"]))
        regions = review["excel_evidence_regions"][0]["regions"]
        self.assertEqual([56, 65], regions[0]["row_range"])
        self.assertEqual("SuppFig3", regions[1]["sheet"])

    def test_near_modified_summary_aggregates_separate_group_series(self):
        audit = {
            "version": "4",
            "high_findings": [{"test": "near_modified_matrix_reuse"}],
            "near_modified_matrix_reuse": [
                {
                    "severity": "high", "location": "Fig.2j 10x3 vs Fig.3e 10x3",
                    "description": "30个对应值尾数不同", "n_values": 30,
                    "matching_prefix_count": 30, "changed_count": 30,
                },
                {
                    "severity": "high", "location": "Fig.2j Vector vs Fig.3e Vector",
                    "description": "11个对应值尾数不同", "n_values": 11,
                    "matching_prefix_count": 11, "changed_count": 11,
                },
            ],
        }

        review = _source_data_audit_highrisk_review_result(
            "10.0000/test-near-modified-aggregate",
            {"deterministic_findings": {"source_data_audit": audit}},
        )

        self.assertIn("41/41", review["reason"])

    def test_digit_edit_fastpath_is_reader_facing_and_carries_excel_regions(self):
        audit = {
            "version": "6",
            "high_findings": [{"test": "digit_edit_matrix_reuse"}],
            "digit_edit_matrix_reuse": [{
                "severity": "high",
                "location": "source.xlsx / Fig1 R2:R6 C2:C4 vs source.xlsx / Fig2 R2:R6 C2:C4",
                "description": "15/15个数值仅相差一次数字编辑",
                "n_values": 15,
                "matched_count": 15,
                "changed_count": 15,
                "operation_counts": {"substitute": 9, "insert": 3, "delete": 3},
                "position_counts": {
                    "integer": 3, "decimal_first": 3,
                    "decimal_middle": 6, "decimal_last": 3,
                },
                "edit_count_distribution": {"1": 15, "2": 0},
                "max_edits_per_value": 2,
                "sample_pairs": [{
                    "left": "5.123456", "right": "8.123456",
                    "operation": "substitute", "position": "integer", "edit_count": 1,
                    "operations": ["substitute"], "positions": ["integer"],
                }],
                "left": {
                    "file": "source.xlsx", "sheet": "Fig1",
                    "row_range": [2, 6], "col_range": [2, 4],
                },
                "right": {
                    "file": "source.xlsx", "sheet": "Fig2",
                    "row_range": [2, 6], "col_range": [2, 4],
                },
            }],
        }

        review = _source_data_audit_highrisk_review_result(
            "10.0000/test-digit-edit",
            {"deterministic_findings": {"source_data_audit": audit}},
        )

        self.assertIn("替换/整数位，1次编辑", review["data_review"])
        self.assertIn("15/15", review["reason"])
        self.assertNotIn("substitute", review["data_review"])
        self.assertEqual(1, len(review["excel_evidence_regions"]))

    def test_zip_workbook_fixed_ratios_drive_initial_high_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook_path = root / "source.xlsx"
            archive_path = root / "supplementary.zip"
            _write_ratio_workbook(workbook_path)
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.write(workbook_path, arcname="Source data for creating all graphs.xlsx")
            workbook_path.unlink()

            audit = audit_source_data(root, doi="10.0000/test-ratio")
            ratio_findings = audit["fixed_ratio_series"]
            self.assertEqual(2, len(ratio_findings))
            self.assertTrue(all(item.get("details") for item in ratio_findings))
            self.assertTrue(any(item["matched_count"] == 9 and item["total_compared"] == 10 for item in ratio_findings))
            self.assertTrue(any(abs(item["ratio"] - 0.4) < 1e-8 for item in ratio_findings))

            anomalies = check_data_anomalies(root)
            pattern_anomalies = [item for item in anomalies if item.get("test") == "fixed_ratio_series"]
            self.assertEqual(2, len(pattern_anomalies))
            overall = _compute_overall_risk({
                "image_duplicates": [],
                "image_splicing": [],
                "data_anomalies": anomalies,
                "reference_issues": [],
            })
            self.assertEqual("高风险", overall["level"])
            self.assertIn("data", overall["high_dimensions"])

            review = _source_data_audit_highrisk_review_result(
                "10.0000/test-ratio",
                {"deterministic_findings": {"source_data_audit": audit}},
            )
            self.assertIsNotNone(review)
            self.assertEqual("高风险", review["result"])
            self.assertIn("固定比例", review["data_review"])
            self.assertIn("9/10", review["data_review"])
            self.assertIn("3/3", review["data_review"])

    def test_same_sheet_different_panels_exact_series_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_exact_reuse_workbook(root / "source.xlsx")

            audit = audit_source_data(root, doi="10.0000/test-reuse")
            findings = audit["same_sheet_exact_series_reuse"]
            self.assertEqual(2, len(findings))
            self.assertTrue(all(item.get("details") for item in findings))
            self.assertTrue(all(item["n_values"] == 8 for item in findings))
            locations = "\n".join(item["location"] for item in findings)
            self.assertIn("Fig.7e", locations)
            self.assertIn("Fig.7f", locations)

            review = _source_data_audit_highrisk_review_result(
                "10.0000/test-reuse",
                {"deterministic_findings": {"source_data_audit": audit}},
            )
            self.assertIsNotNone(review)
            self.assertEqual("高风险", review["result"])
            self.assertIn("精确复用", review["reason"])


if __name__ == "__main__":
    unittest.main()
