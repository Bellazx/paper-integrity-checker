import os
import string
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get(
    "PAPER_INTEGRITY_ROOT",
    Path(__file__).resolve().parents[1],
)).resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from api.services import review


TEMPLATE_FIELDS = {
    "PROMPT_HEADER": {
        "doi", "report_json_path", "input_dir", "evidence_path",
        "soft_explain_guardrails",
    },
    "DETERMINISTIC_AGENT_PROMPT_TEMPLATE": {
        "doi", "report_json_path", "input_dir", "verified_evidence_json",
    },
    "LOWRISK_VERIFIER_PROMPT_TEMPLATE": {
        "original_review_instruction", "doi", "report_json_path", "input_dir",
        "evidence_path", "soft_explain_guardrails", "prior_result_json",
        "skill_rules", "reader_facing_text_rules",
    },
    "JUDGE_PROMPT_TEMPLATE": {
        "doi", "report_json_path", "input_dir", "evidence_path",
        "soft_explain_guardrails", "original_review_instruction",
        "original_review_task", "coverage_repair_instruction", "skill_rules",
        "reader_facing_text_rules",
        "r1_result", "r1_image", "r1_data", "r1_ref", "r1_methodology",
        "r1_verdict", "r1_reason", "r2_result", "r2_image", "r2_data",
        "r2_ref", "r2_methodology", "r2_verdict", "r2_reason",
    },
}


class ReviewPromptTemplateTest(unittest.TestCase):
    def test_prompt_templates_contain_only_declared_format_fields(self):
        formatter = string.Formatter()
        for name, expected_fields in TEMPLATE_FIELDS.items():
            parsed_fields = {
                field_name
                for _, field_name, _, _ in formatter.parse(getattr(review, name))
                if field_name is not None
            }
            self.assertEqual(expected_fields, parsed_fields, name)

    def test_all_formatted_review_prompts_render_literal_evidence_image_json(self):
        prior = {
            "doi": "10.1/example",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "无",
            "data_review": "无",
            "ref_review": "无",
            "reason": "未发现实质问题",
        }
        prompts = [
            review._build_deterministic_agent_prompt(
                "10.1/example", "/tmp/report.json", "/tmp/input",
                {"checks": [], "verified": True},
            ),
            review._build_lowrisk_verifier_prompt(
                "10.1/example", "/tmp/report.json", "/tmp/input", prior,
            ),
            review._build_judge_prompt(
                "10.1/example", "/tmp/report.json", "/tmp/input", prior, prior,
            ),
        ]
        for prompt in prompts:
            self.assertIn('"evidence_images": [{"dimension":', prompt)
            self.assertIn("严重笔误", prompt)

    def test_first_pass_prompt_keeps_literal_json_without_double_braces(self):
        prompt = review._build_prompt(
            "10.1/example", "/tmp/report.json", "/tmp/input",
        )
        self.assertIn('"evidence_images": [{"dimension":', prompt)
        self.assertNotIn('"evidence_images": [{{"dimension":', prompt)

    def test_reader_facing_contract_requires_all_high_risk_labels(self):
        for label in ("问题位置：", "问题类型：", "发现：", "为什么重要：", "核查依据："):
            self.assertIn(label, review.READER_FACING_TEXT_RULES)

    def test_risk_boundary_does_not_upgrade_editorial_corrections(self):
        for phrase in ("笔误", "引用编号", "排版格式", "工程近似", "数据、图表、计算", "严重笔误"):
            self.assertIn(phrase, review.COMPACT_REVIEW_RULES)

    def test_deterministic_prompt_keeps_typo_boundary(self):
        prompt = review._build_deterministic_agent_prompt(
            "10.1/example", "/tmp/report.json", "/tmp/input",
            {"checks": [], "verified": True},
        )
        self.assertIn("普通拼写、引用编号、书目字段、排版格式", prompt)
        self.assertIn("严重笔误", prompt)


if __name__ == "__main__":
    unittest.main()
