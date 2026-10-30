from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from openpyxl import Workbook

from api.services.source_data_audit import _digit_edit, _single_digit_edit, audit_source_data
from api.services import source_data_audit


FIXTURE_ROOT = Path("/home/sjtu/paper-integrity-checker/data/input/scholar/0705gengtongxue")


class SourceDataAuditRegressionTests(unittest.TestCase):
    def test_byte_identical_inputs_share_only_deterministic_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            left = root / "left"
            right = root / "right"
            cache = root / "cache"
            left.mkdir()
            right.mkdir()
            (left / "source.csv").write_text("group,value\nA,1\nB,2\n", encoding="utf-8")
            (right / "source.csv").write_text("group,value\nA,1\nB,2\n", encoding="utf-8")
            calls = 0
            calls_lock = threading.Lock()

            def slow_audit(input_dir, doi=""):
                nonlocal calls
                with calls_lock:
                    calls += 1
                time.sleep(0.05)
                return {
                    "version": source_data_audit.AUDIT_VERSION,
                    "doi": doi,
                    "input_dir": str(input_dir),
                    "high_findings": [],
                }

            with mock.patch.object(source_data_audit, "_AUDIT_CACHE_DIR", cache), \
                 mock.patch.object(source_data_audit, "_audit_source_data_uncached", side_effect=slow_audit):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(audit_source_data, left, "10.1/left")
                    second = pool.submit(audit_source_data, right, "10.1/right")
                    results = [first.result(), second.result()]

            self.assertEqual(1, calls)
            self.assertEqual({"10.1/left", "10.1/right"}, {item["doi"] for item in results})
            self.assertEqual({str(left), str(right)}, {item["input_dir"] for item in results})

    def test_classifies_single_digit_edits_by_operation_and_position(self):
        cases = (
            (5.123456, 8.123456, "substitute", "integer"),
            (5.123456, 5.923456, "substitute", "decimal_first"),
            (5.123456, 5.193456, "substitute", "decimal_middle"),
            (5.123456, 5.123459, "substitute", "decimal_last"),
            (5.123456, 5.1237456, "insert", "decimal_middle"),
            (5.123456, 5.12346, "delete", "decimal_middle"),
        )
        for left, right, operation, position in cases:
            with self.subTest(left=left, right=right):
                edit = _single_digit_edit(left, right)
                self.assertIsNotNone(edit)
                self.assertEqual(operation, edit["operation"])
                self.assertEqual(position, edit["position"])

    def test_classifies_two_digit_edits(self):
        cases = (
            (5.123456, 5.12783456, ["insert", "insert"]),
            (5.123456, 5.1256, ["delete", "delete"]),
            (5.123456, 5.193496, ["substitute", "substitute"]),
            (5.123456, 5.1983456, ["substitute", "insert"]),
        )
        for left, right, operations in cases:
            with self.subTest(left=left, right=right):
                edit = _digit_edit(left, right)
                self.assertIsNotNone(edit)
                self.assertEqual(2, edit["edit_count"])
                self.assertCountEqual(operations, edit["operations"])

        self.assertIsNone(_digit_edit(5.123456, 8.923856))

    def test_detects_matrix_reuse_with_mixed_single_digit_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "Fig1"
            left.append(["Fig.1a"])
            right = workbook.create_sheet("Fig2")
            right.append(["Fig.2a"])
            original = [
                [5.123456, 6.234567, 7.345678],
                [5.223456, 6.334567, 7.445678],
                [5.323456, 6.434567, 7.545678],
                [5.423456, 6.534567, 7.645678],
                [5.523456, 6.634567, 7.745678],
            ]
            operations = (
                lambda token: "8" + token[1:],
                lambda token: token[:3] + "9" + token[4:],
                lambda token: token[:-1] + ("9" if token[-1] != "9" else "8"),
                lambda token: token[:5] + "7" + token[5:],
                lambda token: token[:5] + token[6:],
            )
            for row_index, row in enumerate(original, start=1):
                changed = []
                for column_index, value in enumerate(row):
                    token = f"{value:.6f}"
                    changed.append(float(operations[(row_index + column_index - 1) % len(operations)](token)))
                left.append([row_index, *row])
                right.append([row_index, *changed])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/digit-edits")

            findings = result["digit_edit_matrix_reuse"]
            self.assertEqual(1, len(findings))
            finding = findings[0]
            self.assertEqual(15, finding["matched_count"])
            self.assertEqual(15, finding["changed_count"])
            self.assertGreater(finding["operation_counts"]["substitute"], 0)
            self.assertGreater(finding["operation_counts"]["insert"], 0)
            self.assertGreater(finding["operation_counts"]["delete"], 0)
            self.assertTrue(any(
                item["test"] == "digit_edit_matrix_reuse"
                for item in result["high_findings"]
            ))

    def test_detects_matrix_reuse_with_two_digit_insertions_and_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "Fig1"
            left.append(["Fig.1a"])
            right = workbook.create_sheet("Fig2")
            right.append(["Fig.2a"])
            original = [
                [5.123456, 6.234567, 7.345678],
                [5.223456, 6.334567, 7.445678],
                [5.323456, 6.434567, 7.545678],
                [5.423456, 6.534567, 7.645678],
                [5.523456, 6.634567, 7.745678],
            ]
            operations = (
                lambda token: token[:5] + "78" + token[5:],
                lambda token: token[:5] + token[7:],
                lambda token: token[:3] + "9" + token[4:5] + "8" + token[6:],
            )
            for row_index, row in enumerate(original, start=1):
                changed = []
                for column_index, value in enumerate(row):
                    token = f"{value:.6f}"
                    changed.append(float(operations[(row_index + column_index - 1) % 3](token)))
                left.append([row_index, *row])
                right.append([row_index, *changed])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/two-digit-edits")

            findings = result["digit_edit_matrix_reuse"]
            self.assertEqual(1, len(findings))
            finding = findings[0]
            self.assertEqual(15, finding["matched_count"])
            self.assertEqual(15, finding["edit_count_distribution"]["2"])
            self.assertGreater(finding["operation_counts"]["insert"], 0)
            self.assertGreater(finding["operation_counts"]["delete"], 0)

    def test_independent_nearby_values_with_multiple_digit_changes_are_not_digit_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "Fig1"
            left.append(["Fig.1a"])
            right = workbook.create_sheet("Fig2")
            right.append(["Fig.2a"])
            for row in range(5):
                left.append([row, 10.123456 + row, 20.234567 + row, 30.345678 + row])
                right.append([row, 11.987654 + row, 21.876543 + row, 31.765432 + row])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/independent")

            self.assertEqual([], result["digit_edit_matrix_reuse"])
    def test_detects_cross_figure_reuse_with_rewritten_later_decimals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "EDFig2"
            left.append(["EDFig 2j"])
            for row, base in enumerate((5.341093, 6.784632, 4.231120, 5.420348, 3.897482), start=1):
                left.append([row, base, base + 1.11, base + 2.22])
            right = workbook.create_sheet("SuppFig3")
            right.append(["SuppFig 3e"])
            for row, base in enumerate((5.340439, 6.782191, 4.231678, 5.420531, 3.892166), start=1):
                right.append([row, base, base + 1.1102, base + 2.2201])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/near-reuse")

            findings = result["near_modified_matrix_reuse"]
            self.assertEqual(1, len(findings))
            finding = findings[0]
            self.assertEqual(15, finding["n_values"])
            self.assertEqual(15, finding["matching_prefix_count"])
            self.assertIn("Extended Data Fig.2j", finding["location"])
            self.assertIn("Supplementary Fig.3e", finding["location"])
            self.assertTrue(any(
                item["test"] == "near_modified_matrix_reuse"
                for item in result["high_findings"]
            ))

    def test_ignores_near_zero_values_that_all_truncate_to_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "Fig1"
            left.append(["Fig.1a"])
            right = workbook.create_sheet("Fig2")
            right.append(["Fig.2a"])
            for row in range(5):
                left.append([row, 0.001 + row / 10000, 0.002 + row / 10000, 0.003 + row / 10000])
                right.append([row, 0.004 + row / 10000, 0.005 + row / 10000, 0.006 + row / 10000])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/near-zero")

            self.assertEqual([], result["near_modified_matrix_reuse"])

    def test_detects_matching_single_group_series_with_rewritten_tail_digits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "EDFig2"
            left.append(["EDFig 2j"])
            left.append(["sample", "Vector"])
            right = workbook.create_sheet("SuppFig3")
            right.append(["SuppFig 3e"])
            right.append(["sample", "Vector"])
            values = [5.341093, 6.784632, 4.231120, 5.420348, 3.897482,
                      6.004731, 7.544457, 4.512391, 5.112135, 4.943782, 5.763285]
            changed = [5.340439, 6.782191, 4.231678, 5.420531, 3.892166,
                       6.003421, 7.543827, 4.512256, 5.112107, 4.943280, 5.764059]
            for row, (original, rewritten) in enumerate(zip(values, changed), start=1):
                left.append([row, original])
                right.append([row, rewritten])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/near-series")

            findings = result["near_modified_matrix_reuse"]
            self.assertEqual(1, len(findings))
            self.assertEqual(11, findings[0]["n_values"])
            self.assertIn("Vector", findings[0]["left"]["column_context"])

    def test_single_group_series_does_not_borrow_neighboring_group_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.xlsx"
            workbook = Workbook()
            left = workbook.active
            left.title = "Fig1"
            left.append(["Fig.1a"])
            left.append(["sample", "Treatment A", "Shared neighbor"])
            right = workbook.create_sheet("Fig2")
            right.append(["Fig.2a"])
            right.append(["sample", "Treatment B", "Shared neighbor"])
            for row in range(1, 12):
                original = 5 + row / 10 + 0.001234
                rewritten = 5 + row / 10 + 0.001876
                left.append([row, original, row * 20])
                right.append([row, rewritten, row * 30])
            workbook.save(path)

            result = audit_source_data(tmp, doi="10.0000/neighbor-context")

            self.assertEqual([], result["near_modified_matrix_reuse"])

    def test_detects_s41586_022_matrix_reuse_and_n_mismatch(self):
        result = audit_source_data(FIXTURE_ROOT / "10.1038_s41586-022-04888-7", doi="10.1038/s41586-022-04888-7")

        matrix_locations = "\n".join(item["location"] for item in result["rectangular_matrix_reuse"])
        self.assertIn("41586_2022_4888_MOESM4_ESM.xlsx / Fig. 2d", matrix_locations)
        self.assertIn("41586_2022_4888_MOESM7_ESM.xlsx / Extended Data Fig. 3d", matrix_locations)
        self.assertTrue(any(item["n_values"] >= 15 for item in result["rectangular_matrix_reuse"]))

        n_locations = "\n".join(item["location"] for item in result["figure_n_mismatch"])
        self.assertIn("41586_2022_4888_MOESM5_ESM.xlsx / Fig. 3e / Vehicle", n_locations)
        self.assertIn("41586_2022_4888_MOESM5_ESM.xlsx / Fig. 3e / TMX", n_locations)
        self.assertTrue(any(item["declared_n"] == 3 and item["source_n"] == 4 for item in result["figure_n_mismatch"]))


    def test_detects_s41586_025_extended_data_rtqpcr_reuse(self):
        result = audit_source_data(FIXTURE_ROOT / "10.1038_s41586-025-09574-y", doi="10.1038/s41586-025-09574-y")

        locations = "\n".join(item["location"] for item in result["rectangular_matrix_reuse"])
        self.assertIn("41586_2025_9574_MOESM12_ESM.xlsx / Extended data fig.2 b", locations)
        self.assertIn("41586_2025_9574_MOESM19_ESM.xlsx / Extended data fig.11 b", locations)
        self.assertGreaterEqual(sum(1 for item in result["rectangular_matrix_reuse"] if item["n_values"] >= 21), 2)
        self.assertTrue(result["high_findings"])


if __name__ == "__main__":
    unittest.main()
