"""Risk-scoring rules in modules.chinese_report_generator.

Focus: the splice (拼接) integration added so image_splicing actually drives risk.
Pure-function tests — no DB, no network, no LLM.

Run:  python3 -m unittest tests.test_risk_scoring  (from repo root)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.chinese_report_generator import (
    _compute_image_risk,
    _compute_overall_risk,
    _build_risk_score_html,
    _format_splice_findings,
)


def _findings(n_splice=0, image=None, data=None, ref=None, paper=None):
    """Build a minimal findings dict with n_splice always-medium splice suspects."""
    return {
        "paper": paper or {"doi": "10.1/x", "title": "T", "total_references": 0},
        "image_duplicates": image or [],
        "data_anomalies": data or [],
        "reference_issues": ref or [],
        "image_splicing": [
            {
                "test": "image_splicing",
                "page": i + 1,
                "severity": "medium",
                "details": f"在第{i}列检出疑似拼接边界（背景灰度断层）",
            }
            for i in range(n_splice)
        ],
    }


class SpliceTrigger(unittest.TestCase):
    def test_two_suspects_escalate_to_high(self):
        r = _compute_overall_risk(_findings(n_splice=2))
        self.assertEqual(r["level"], "高风险")
        self.assertIn("image", r["high_dimensions"])

    def test_one_suspect_stays_low(self):
        # A single suspect is too false-positive-prone (a legit lane divider can trip
        # one seam) — it must NOT raise risk on its own.
        r = _compute_overall_risk(_findings(n_splice=1))
        self.assertEqual(r["level"], "低风险")
        self.assertNotIn("image", r["high_dimensions"])

    def test_many_suspects_still_high(self):
        r = _compute_overall_risk(_findings(n_splice=7))
        self.assertEqual(r["level"], "高风险")

    def test_missing_key_does_not_crash(self):
        # report.json written before this fix has no image_splicing key.
        bare = {"paper": {}, "image_duplicates": [], "data_anomalies": [], "reference_issues": []}
        r = _compute_overall_risk(bare)
        self.assertEqual(r["level"], "低风险")

    def test_splice_sets_consistent_image_score(self):
        # Splice-only high risk must not produce the contradictory
        # score=0 + level=高风险 combination.
        f = _findings(n_splice=3)
        img_dim = _compute_image_risk(f)
        overall = _compute_overall_risk(f)
        self.assertGreaterEqual(img_dim["score"], 56)
        self.assertGreaterEqual(overall["score"], 56)
        self.assertEqual(overall["level"], "高风险")


class SplicePdfRendering(unittest.TestCase):
    def test_two_suspects_render_high_line(self):
        html = _build_risk_score_html(_findings(n_splice=2))
        self.assertIn("图像拼接检测", html)
        self.assertIn("检出 2 处", html)
        # the splice line itself must be 高风险 (look only at the splice fragment)
        frag = html.split("图像拼接检测")[1][:120]
        self.assertIn("高风险", frag)

    def test_one_suspect_renders_low_line_with_count(self):
        html = _build_risk_score_html(_findings(n_splice=1))
        self.assertIn("检出 1 处", html)
        frag = html.split("图像拼接检测")[1][:120]
        self.assertIn("低风险", frag)

    def test_no_suspects_renders_low_line(self):
        html = _build_risk_score_html(_findings(n_splice=0))
        self.assertIn("图像拼接检测", html)
        self.assertIn("未检出疑似拼接图像", html)
        frag = html.split("图像拼接检测")[1][:120]
        self.assertIn("低风险", frag)

    def test_no_score_or_zhongfengxian_leaks(self):
        # Report constraints: never print a numeric score in the splice line; never 中风险.
        html = _build_risk_score_html(_findings(n_splice=2))
        self.assertNotIn("中风险", html)

    def test_splice_findings_are_available_to_llm_prompt(self):
        text = _format_splice_findings(_findings(n_splice=2)["image_splicing"])
        self.assertIn("疑似图像拼接", text)
        self.assertIn("标注图", _format_splice_findings([{
            "page": 3,
            "details": "在第10列检出疑似拼接边界",
            "annotation_path": "/tmp/ann.png",
        }]))


class ExistingTriggersUnaffected(unittest.TestCase):
    """Guard that adding splice didn't disturb the data high-risk gate."""

    def test_data_high_still_triggers_without_splice(self):
        data = [
            {"test": "coefficient_of_variation", "severity": "high", "details": {}}
            for _ in range(5)
        ]
        r = _compute_overall_risk(_findings(n_splice=0, data=data))
        self.assertEqual(r["level"], "高风险")
        self.assertIn("data", r["high_dimensions"])


if __name__ == "__main__":
    unittest.main()
