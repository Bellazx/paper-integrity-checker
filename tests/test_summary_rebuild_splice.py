"""Summary-rebuild paths preserve splice counts (#4).

recheck_batch.py and backfill_ref_extraction_fix.py rebuild findings['summary'].
They must fold image_splicing into total_issues + medium_severity and expose
image_splicing_suspects — otherwise re-running them makes the summary contradict the
findings. (DB risk level is unaffected because _compute_overall_risk reads the
image_splicing key directly, but the summary artifact must stay coherent.)

Run:  python3 -m unittest tests.test_summary_rebuild_splice
"""
import os
import sys
import importlib.util
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_func(module_filename, func_name):
    """Import a single function from a top-level script without running it."""
    path = os.path.join(ROOT, module_filename)
    spec = importlib.util.spec_from_file_location(module_filename.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, func_name)


class BackfillRecomputeSummary(unittest.TestCase):
    """backfill_ref_extraction_fix._recompute_summary is a clean importable function."""

    @classmethod
    def setUpClass(cls):
        cls.fn = staticmethod(_load_func("backfill_ref_extraction_fix.py", "_recompute_summary"))

    def test_splice_folded_into_totals(self):
        findings = {
            "image_duplicates": [{"severity": "high"}],
            "data_anomalies": [{"severity": "low"}],
            "reference_issues": [],
            "image_splicing": [{"severity": "medium"}, {"severity": "medium"}],
        }
        s = self.fn(findings)
        self.assertEqual(s["image_splicing_suspects"], 2)
        # total = 1 img + 1 data + 0 ref + 2 splice
        self.assertEqual(s["total_issues"], 4)
        # medium = 0 (from allr) + 2 splice
        self.assertEqual(s["medium_severity"], 2)
        # invariant: total == high + medium + low + (splice already in medium)
        self.assertEqual(s["total_issues"],
                         s["high_severity"] + s["medium_severity"] + s["low_severity"])

    def test_no_splice_key(self):
        s = self.fn({"image_duplicates": [], "data_anomalies": [], "reference_issues": []})
        self.assertEqual(s["image_splicing_suspects"], 0)
        self.assertEqual(s["total_issues"], 0)


if __name__ == "__main__":
    unittest.main()
