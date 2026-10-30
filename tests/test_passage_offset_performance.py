import time
import unittest

import numpy as np
import pandas as pd

from utils.stats import check_passage_offset_pattern


class PassageOffsetPerformanceTests(unittest.TestCase):
    def test_preserves_exact_and_fixed_offset_detection(self):
        frame = pd.DataFrame([
            [1.25, 2.5, 4.0],
            [1.25, 2.5, 4.0],
            [7.5, 8.5, 10.0],
            [7.5, 8.5, 11.5],
        ], columns=["rep1", "rep2", "rep3"])

        result = check_passage_offset_pattern(frame, min_pairs=2)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["severity"], "high")
        self.assertEqual(result[0]["n_exact_matches"], 1)
        self.assertEqual(result[0]["n_integer_offsets"], 1)

    def test_large_unique_table_does_not_compare_every_row_pair(self):
        rows = 10_000
        values = np.arange(rows, dtype=float)
        frame = pd.DataFrame({
            "rep1": values,
            "rep2": values + 0.125,
            "rep3": values + 0.25,
        })

        started = time.monotonic()
        result = check_passage_offset_pattern(frame)
        elapsed = time.monotonic() - started

        self.assertEqual(result, [])
        self.assertLess(elapsed, 2.0)

    def test_large_repeated_group_counts_without_materializing_pairs(self):
        rows = 20_000
        frame = pd.DataFrame({
            "rep1": np.ones(rows),
            "rep2": np.full(rows, 2.5),
            "rep3": np.tile([0.0, 1.0], rows // 2),
        })

        started = time.monotonic()
        result = check_passage_offset_pattern(frame, min_pairs=2)
        elapsed = time.monotonic() - started

        self.assertEqual(len(result), 1)
        finding = result[0]
        self.assertEqual(finding["n_exact_matches"], 99_990_000)
        self.assertEqual(finding["n_integer_offsets"], 100_000_000)
        self.assertEqual(finding["n_pairs"], 199_990_000)
        self.assertLessEqual(len(finding["pairs"]), 10)
        self.assertTrue(finding["pairs_truncated"])
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
