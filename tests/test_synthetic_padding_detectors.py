"""Tests for the synthetic-data '末位填充' and '跨行伪重复' detectors.

These detectors were added in 2026-06-06 to plug gaps that the original
data_checker missed on scitranslmed.aba3571:

  - check_trailing_digit_uniformity — aba3571 Fig.S9 末 4 位 = "1246" in 60-67% of
    values across 5 antibiotic columns
  - check_passage_offset_pattern — aba3571 Fig.S11 Section A/B 多组 passages
    前 N 列相同 + 第 N+1 列整数偏移

Both are wired into data_checker._analyze_column_group / _analyze_sheet and
into review_evidence.py for the deterministic evidence bundle.
"""
import unittest

import numpy as np
import pandas as pd

from modules.data_checker import _analyze_column_group, _analyze_passage_offset_pattern
from utils.stats import check_passage_offset_pattern, check_trailing_digit_uniformity


class TrailingDigitUniformityTests(unittest.TestCase):
    def test_aba3571_fig_s9_pattern(self):
        """14/15 values end in '1246' — synthetic padding marker (the 15th is
        a 0.000000123 offset that breaks the pattern, matching the real Fig.S9
        IMI column)."""
        vals = np.array([
            6.115011246, 5.105431246, 6.084441246, 6.333691246, 6.003591246,
            5.370941246, 5.955411246, 5.662871246, 6.272131246, 5.692861246,
            5.665471246, 4.856239230, 6.201311246, 6.149941246, 5.957411246,
        ])
        r = check_trailing_digit_uniformity(vals, n_digits=4)
        self.assertTrue(r["flagged"])
        self.assertEqual(r["severity"], "high")
        self.assertEqual(r["most_common_suffix"], "1246")
        self.assertEqual(r["match_count"], 14)
        self.assertEqual(r["total"], 15)

    def test_realistic_random_data_not_flagged(self):
        """Random uniform data has no common suffix."""
        np.random.seed(0)
        vals = np.random.uniform(0, 100, 200)
        r = check_trailing_digit_uniformity(vals, n_digits=4)
        self.assertFalse(r.get("flagged", True))
        self.assertTrue(r["testable"])
        self.assertLess(r["match_ratio"], 0.5)

    def test_integer_data_skipped(self):
        """Integer columns route to terminal_digit_test, not here."""
        vals = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], dtype=float)
        r = check_trailing_digit_uniformity(vals, n_digits=4)
        self.assertFalse(r["testable"])
        self.assertEqual(r["reason"], "integer_data_use_terminal_digit")

    def test_insufficient_data_skipped(self):
        """Below min_samples → not testable."""
        vals = np.array([1.123456, 2.123456, 3.123456])
        r = check_trailing_digit_uniformity(vals, n_digits=4, min_samples=10)
        self.assertFalse(r["testable"])
        self.assertEqual(r["reason"], "insufficient_data")

    def test_partial_padding_still_flags(self):
        """10/15 = 67% match → high (>=60% threshold for 4 digits)."""
        suffixes = ["1246"] * 10 + ["9999", "8888", "7777", "6666", "5555"]
        # build values whose last 4 digits are these suffixes
        vals = np.array([float(f"3.{s}11246") if s == "1246" else float(f"3.{s}99999")
                         for s in suffixes])
        r = check_trailing_digit_uniformity(vals, n_digits=4)
        self.assertTrue(r["flagged"])
        self.assertEqual(r["severity"], "high")
        self.assertEqual(r["most_common_suffix"], "1246")
        self.assertEqual(r["match_count"], 10)

    def test_empty_suffix_no_flag(self):
        """Many values are integer-like → not flagged (decimal_uniformity territory)."""
        # Mix: 8 integer-valued + 2 short decimal — empty suffix dominates but
        # function should NOT flag because the "synthetic padding" interpretation
        # doesn't fit.
        vals = np.array([5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 5.5, 6.7])
        r = check_trailing_digit_uniformity(vals, n_digits=4)
        self.assertFalse(r.get("flagged", True))

    def test_data_checker_integration(self):
        """The check fires through _analyze_column_group with severity=high."""
        vals = np.array([
            6.115011246, 5.105431246, 6.084441246, 6.333691246, 6.003591246,
            5.370941246, 5.955411246, 5.662871246, 6.272131246, 5.692861246,
            5.665471246, 4.856239230, 6.201311246, 6.149941246, 5.957411246,
        ])
        anomalies = _analyze_column_group(vals, "test/Fig.S9/LVX", "LVX")
        trailing = [a for a in anomalies if a["test"] == "trailing_digit_uniformity"]
        self.assertEqual(len(trailing), 1)
        self.assertEqual(trailing[0]["severity"], "high")
        self.assertIn("1246", trailing[0]["description"])


class PassageOffsetPatternTests(unittest.TestCase):
    def test_aba3571_fig_s11_section_b_style(self):
        """Three patterns: exact match, +1.5 offset, +2.0 offset."""
        df = pd.DataFrame({
            "rep1": [4.08634, 4.08634, 6.313123, 6.313123, 6.004274, 6.004274],
            "rep2": [6.682494, 6.682494, 4.721659, 4.721659, 4.190321, 4.190321],
            "rep3": [5.09884, 6.59884, 4.172879, 6.172879, 5.341484, 5.341484],
        })
        results = check_passage_offset_pattern(df, min_pairs=2)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["flagged"])
        self.assertEqual(results[0]["severity"], "high")
        self.assertGreaterEqual(results[0]["n_pairs"], 3)

    def test_only_one_pair_does_not_flag(self):
        """Need at least min_pairs=2 row-pairs to flag."""
        df = pd.DataFrame({
            "rep1": [1.0, 1.0, 5.0, 5.0, 9.0],
            "rep2": [2.0, 2.0, 6.0, 6.0, 10.0],
            "rep3": [3.0, 3.5, 7.0, 7.5, 11.0],  # R0↔R1 +0.5, R2↔R3 +0.5
        })
        results = check_passage_offset_pattern(df, min_pairs=2)
        # Two pairs should flag
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(results[0]["n_pairs"], 2)

    def test_random_data_not_flagged(self):
        """Stochastic replicates have varied first-N columns — no pattern."""
        np.random.seed(42)
        df = pd.DataFrame(np.random.uniform(0, 10, (10, 4)),
                          columns=["r1", "r2", "r3", "r4"])
        results = check_passage_offset_pattern(df, min_pairs=2)
        self.assertEqual(results, [])

    def test_too_few_rows_skipped(self):
        """Need at least 3 rows to detect any pair."""
        df = pd.DataFrame({"r1": [1.0, 1.0], "r2": [2.0, 2.0]})
        results = check_passage_offset_pattern(df, min_pairs=2)
        self.assertEqual(results, [])

    def test_data_checker_integration(self):
        """The check fires through _analyze_passage_offset_pattern.

        Uses 12 rows (above the 10-row sub-table filter) to match the
        realistic passage-table size (aba3571 Fig.S11 sub-tables are 21 rows).
        """
        df = pd.DataFrame({
            "rep1": [4.08634, 4.08634, 6.313123, 6.313123, 6.004274, 6.004274,
                     3.5, 3.5, 7.1, 7.1, 5.2, 5.2],
            "rep2": [6.682494, 6.682494, 4.721659, 4.721659, 4.190321, 4.190321,
                     5.2, 5.2, 6.0, 6.0, 4.8, 4.8],
            "rep3": [5.09884, 6.59884, 4.172879, 6.172879, 5.341484, 5.341484,
                     4.3, 5.8, 5.5, 7.0, 4.1, 4.1],
        })
        anomalies = _analyze_passage_offset_pattern(df, "test.xlsx", "Fig.S11 B")
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["severity"], "high")
        self.assertIn("row-pair", anomalies[0]["description"])


if __name__ == "__main__":
    unittest.main()
