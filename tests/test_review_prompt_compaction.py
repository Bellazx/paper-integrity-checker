import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.services import review


class ReviewPromptCompaction(unittest.TestCase):
    def test_llm_evidence_summary_groups_duplicate_pairs(self):
        evidence = {
            "doi": "10.1/example",
            "input_dir": "/tmp/in",
            "report_json": "/tmp/report.json",
            "paper": {"title": "T", "doi": "10.1/example", "filename": "paper.pdf"},
            "coverage_manifest": {
                "counts": {
                    "high_findings": 1,
                    "main_figures": 0,
                    "duplicate_column_pairs": 3,
                    "cross_sheet_reuse": 0,
                    "cross_sheet_reuse_groups": 0,
                    "cross_sheet_reuse_representatives": 0,
                    "decimal_precision_mismatch": 0,
                    "total_must_address": 4,
                }
            },
            "high_findings": [{"dim": "data", "test": "cross_group_duplicate", "location": "Sheet1"}],
            "deterministic_findings": {
                "duplicate_column_pairs": [
                    {"file": "S.xlsx", "sheet": "Fig1", "col_a_header": "Potential", "col_b_header": "Potential", "n_points": 100, "overlap": 1.0},
                    {"file": "S.xlsx", "sheet": "Fig1", "col_a_header": "Potential", "col_b_header": "Potential", "n_points": 90, "overlap": 1.0},
                    {"file": "S.xlsx", "sheet": "Fig2", "col_a_header": "Signal", "col_b_header": "Signal", "n_points": 8, "overlap": 1.0},
                ],
                "cross_sheet_reuse_groups": [],
                "decimal_precision_mismatch": [],
            },
        }

        summary = review._build_llm_evidence_summary(evidence, "/tmp/full.json")
        groups = summary["deterministic_findings"]["duplicate_column_pair_groups"]

        self.assertEqual(summary["review_scope"]["raw_must_address_count"], 4)
        self.assertEqual(len(groups), 2)
        self.assertTrue(any(g["count"] == 2 and g["likely_axis_or_shared_variable"] for g in groups))

    def test_prompt_uses_compact_rules_and_summary_path(self):
        prompt = review._build_prompt(
            "10.1/example",
            "/tmp/report.json",
            "/tmp/input",
            "/tmp/evidence_llm_summary.json",
            full_evidence_path="/tmp/evidence.json",
        )

        self.assertIn("/tmp/evidence_llm_summary.json", prompt)
        self.assertIn("/tmp/evidence.json", prompt)
        self.assertIn("复核核心规则", prompt)
        self.assertNotIn("#### 0. Evidence Gate", prompt)
        self.assertLess(len(prompt), 12000)

    def test_judge_prompt_uses_actual_evidence_path(self):
        prompt = review._build_judge_prompt(
            "10.1/example",
            "/tmp/report.json",
            "/tmp/input",
            {"result": "高风险", "verdict": "建议高风险", "reason": "数据问题"},
            {"result": "低风险", "verdict": "建议低风险", "reason": "可解释"},
            ev=review.EvidencePaths(
                "/tmp/out/review_artifacts/paper_evidence_llm_summary.json",
                "/tmp/out/review_artifacts/paper_evidence.json",
            ),
        )

        self.assertIn("/tmp/out/review_artifacts/paper_evidence_llm_summary.json", prompt)
        self.assertIn("/tmp/out/review_artifacts/paper_evidence.json", prompt)
        self.assertNotIn("data/output/review_v2/ 下 <DOI下划线化>_evidence.json", prompt)

    def test_ensure_llm_evidence_bundle_writes_smaller_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            full = Path(tmp) / "paper_evidence.json"
            payload = {
                "doi": "10.1/example",
                "coverage_manifest": {"counts": {"total_must_address": 20}},
                "deterministic_findings": {
                    "duplicate_column_pairs": [
                        {"file": "S.xlsx", "sheet": f"Fig{i % 3}", "col_a_header": "Potential", "col_b_header": "Potential", "n_points": 100}
                        for i in range(20)
                    ],
                    "cross_sheet_reuse_groups": [],
                    "decimal_precision_mismatch": [],
                },
                "high_findings": [],
            }
            full.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

            summary_path = Path(review._ensure_llm_evidence_bundle("10.1/example", str(full)))

            self.assertTrue(summary_path.exists())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIn("duplicate_column_pair_groups", summary["deterministic_findings"])
            self.assertEqual(summary["full_evidence_path"], str(full))
            self.assertLess(
                len(summary["deterministic_findings"]["duplicate_column_pair_groups"]),
                len(payload["deterministic_findings"]["duplicate_column_pairs"]),
            )


if __name__ == "__main__":
    unittest.main()
