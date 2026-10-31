import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
SKILL_SCRIPTS = Path(os.environ.get(
    "PAPER_REVIEW_SKILL_SCRIPTS",
    "/home/library/.claude/skills/paper-batch-review/scripts",
))
sys.path.insert(0, str(SKILL_SCRIPTS))

from api.services import review
import coverage_validator


def _complete_result(result="高风险"):
    verdict = "建议高风险" if result == "高风险" else "建议低风险"
    return {
        "doi": "10.1/test",
        "result": result,
        "trigger": "完整复核",
        "image_review": "图像维度已核查全部图像告警并给出具体结论。",
        "data_review": "数据维度已核查源数据异常并给出具体结论。",
        "ref_review": "参考文献维度已核查全部文献告警并给出具体结论。",
        "methodology_review": "研究设计与统计维度已核查样本量和方法声明。",
        "verdict": verdict,
        "reason": "四个维度均已完成核查。",
        "dimension_verdicts": {
            "image": "建议低风险",
            "data": verdict,
            "ref": "建议低风险",
            "methodology": "建议低风险",
        },
    }


def _evidence():
    return {
        "coverage_manifest": {"counts": {}, "must_address": []},
        "high_findings": [],
        "main_figures": [],
        "deterministic_findings": {"source_data_audit": {"high_findings": []}},
    }


class MultiDimensionReviewTest(unittest.IsolatedAsyncioTestCase):
    def test_missing_dimension_labels_are_not_invented_from_overall_risk(self):
        result = _complete_result("高风险")
        result.pop("dimension_verdicts")

        normalized = review._normalize_review_result(result)

        self.assertEqual("高风险", normalized["result"])
        self.assertEqual({}, normalized['dimension_verdicts'])
        _, gaps = coverage_validator.validate(normalized, _evidence())
        self.assertEqual(4, sum('维度缺少明确风险结论' in gap for gap in gaps))
        self.assertNotIn("dimension_verdicts", result)

    def test_clean_lowrisk_evidence_closes_without_second_pass(self):
        info = _complete_result("低风险")
        accepted, reason = review._accept_single_review(
            "10.1/test", info, dict(info), _evidence()
        )
        self.assertTrue(accepted)
        self.assertIn("无高风险线索", reason)

    async def test_timeout_path_reaps_review_subprocess(self):
        class FakeProcess:
            returncode = -9

            def __init__(self):
                self.killed = False
                self.reaped = False

            async def communicate(self):
                raise TimeoutError()

            def kill(self):
                self.killed = True

            async def wait(self):
                self.reaped = True

        process = FakeProcess()
        create_process = mock.AsyncMock(return_value=process)
        with mock.patch.object(review, "_find_claude_cli", return_value="claude"), \
             mock.patch.object(review.asyncio, "create_subprocess_exec", create_process), \
             mock.patch.object(review, "_review_subprocess_timeout_seconds", return_value=1):
            result = await review._run_claude_prompt(
                "10.1/test", "test", "return JSON", max_turns=1,
                allowed_tools=None,
            )

        self.assertEqual("review_error", result["trigger"])
        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)
        command = create_process.await_args.args
        self.assertIn("--model", command)
        self.assertEqual(review.REVIEW_FALLBACK_MODEL, command[command.index("--model") + 1])
        self.assertEqual(3, create_process.await_count)
        self.assertEqual('', command[command.index('--tools') + 1])

    async def test_source_data_seed_does_not_short_circuit_full_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            report = root / "report.json"
            report.write_text("{}")
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(_evidence()))
            seed = _complete_result()
            full = _complete_result()
            full["reason"] = "完整四维复核结果"
            run = mock.AsyncMock(return_value=full)

            with mock.patch.object(review, "REVIEW_STATE_DIR", root / "state"), \
                 mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(evidence_path)), \
                 mock.patch.object(review, "_source_data_audit_highrisk_review_result", return_value=seed), \
                 mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
                 mock.patch.object(review, "_run_with_retry", run):
                result = await review.run_review_single(
                    "10.1/test", str(report), str(input_dir), str(output_dir)
                )

            run.assert_awaited_once()
            self.assertEqual("完整四维复核结果", result["reason"])

    async def test_highrisk_verifier_with_seed_skips_duplicate_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            output_dir.mkdir()
            report = root / "report.json"
            report.write_text("{}")
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(_evidence()))
            low = _complete_result("低风险")
            high = _complete_result("高风险")
            high["trigger"] = "独立高风险复核"
            high["reason"] = (
                "问题位置：MOESM10_ESM.xlsx 工作表 Fig. 5h。"
                "问题类型：跨处理组数值派生。发现：逐周逐只呈精确常数差。"
                "为什么重要：独立重复的独立性基础不成立。"
                "核查依据：逐列计算相关系数和斜率均为1。"
            )
            with mock.patch.object(review, "REVIEW_STATE_DIR", root / "state"), \
                 mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(evidence_path)), \
                 mock.patch.object(review, "_source_data_audit_highrisk_review_result", return_value=high), \
                 mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
                 mock.patch.object(review, "_run_with_retry", new_callable=mock.AsyncMock, return_value=low), \
                 mock.patch.object(review, "_run_lowrisk_verifier_with_retry", new_callable=mock.AsyncMock, return_value=high), \
                 mock.patch.object(review, "_run_judge", new_callable=mock.AsyncMock) as judge:
                result = await review.run_review_single(
                    "10.1/test", str(report), str(input_dir), str(output_dir)
                )

            judge.assert_not_awaited()
            self.assertEqual("高风险", result["result"])

    def test_high_risk_with_missing_dimensions_has_coverage_gaps(self):
        partial = _complete_result()
        partial["image_review"] = ""
        partial["ref_review"] = ""
        partial["dimension_verdicts"] = {"data": "建议高风险"}

        validated, gaps = coverage_validator.validate(partial, _evidence())

        self.assertEqual("高风险", validated["result"])
        self.assertTrue(any("image维度" in gap for gap in gaps))
        self.assertTrue(any("ref维度" in gap for gap in gaps))

    def test_high_risk_coverage_gap_cannot_close_first_pass(self):
        partial = _complete_result()
        partial["_coverage_gaps"] = ["reference维度未覆盖"]
        accepted, reason = review._accept_single_review(
            "10.1/test", partial, partial, _evidence()
        )
        self.assertFalse(accepted)
        self.assertIn("全部维度", reason)

    def test_lowrisk_consensus_rejects_uncovered_high_finding(self):
        first = _complete_result("低风险")
        second = _complete_result("低风险")
        first["_coverage_gaps"] = ["未说明重复列对: fig3e [col_9<>col_29]"]
        second["_coverage_gaps"] = ["未说明重复列对: fig3e [col_9<>col_29]"]

        with mock.patch.object(review, "_prior_review_result_for_doi", return_value=None):
            accepted = review._accept_lowrisk_consensus(
                "10.1/test", first, first, second, second
            )

        self.assertFalse(accepted)

    def test_medium_digit_findings_do_not_create_per_column_blockers(self):
        evidence = _evidence()
        evidence["deterministic_findings"]["trailing_digit_uniformity"] = [
            {"severity": "medium", "header": f"col_{i}", "sheet": "Figure 5d"}
            for i in range(40)
        ]
        complete = _complete_result()

        _, gaps = coverage_validator.validate(complete, evidence)

        self.assertFalse(any("末位数字" in gap for gap in gaps))

    def test_high_digit_findings_require_one_category_conclusion(self):
        evidence = _evidence()
        evidence["deterministic_findings"]["trailing_digit_uniformity"] = [
            {"severity": "high", "header": f"col_{i}", "sheet": "Figure 5d"}
            for i in range(12)
        ]
        complete = _complete_result()
        _, missing = coverage_validator.validate(complete, evidence)
        self.assertEqual(1, sum("HIGH末位数字" in gap for gap in missing))

        complete["data_review"] += "末位数字异常已按类别核查，属于浮点存储尾数。"
        _, covered = coverage_validator.validate(complete, evidence)
        self.assertFalse(any("HIGH末位数字" in gap for gap in covered))

    def test_coverage_repair_prompt_names_each_missing_category(self):
        gaps = [
            "未说明小数精度不一致类别（共15项）",
            "未对HIGH发现给出结论: data/rowwise_value_reuse (共1项)",
        ]
        prompt = review._build_judge_prompt(
            "10.1/test", "/tmp/report.json", "/tmp/input",
            _complete_result(), _complete_result(), coverage_gaps=gaps,
        )

        self.assertIn("本次必须补齐的覆盖缺口", prompt)
        for gap in gaps:
            self.assertIn(gap, prompt)

    def test_natural_language_category_conclusions_cover_high_findings(self):
        evidence = _evidence()
        evidence["deterministic_findings"]["decimal_precision_mismatch"] = [{}] * 15
        evidence["deterministic_findings"]["trailing_digit_uniformity"] = [
            {"severity": "high"} for _ in range(15)
        ]
        evidence["high_findings"] = [
            {"dim": "data", "test": "rowwise_value_reuse"},
            {"dim": "data", "test": "rectangular_matrix_reuse"},
        ]
        complete = _complete_result()
        complete["data_review"] += (
            "Figure 3l 的行内复用属于正常测量值碰撞，非异常。"
            "跨图/跨表矩阵共49格逐格完全相同，属于复用问题。"
            "其余小数精度特征与末位数字规律已核查，属于可解释的常见精度特征。"
        )

        _, gaps = coverage_validator.validate(complete, evidence)

        self.assertFalse(any("小数精度" in gap for gap in gaps))
        self.assertFalse(any("末位数字" in gap for gap in gaps))
        self.assertFalse(any("rowwise_value_reuse" in gap for gap in gaps))
        self.assertFalse(any("rectangular_matrix_reuse" in gap for gap in gaps))


if __name__ == "__main__":
    unittest.main()
