import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.services.review as review


def _review_result(result="低风险", trigger="ok", reason="覆盖完整"):
    verdict = "建议高风险" if result == "高风险" else "建议低风险"
    return {
        "doi": "10.1/test",
        "result": result,
        "trigger": trigger,
        "image_review": "图像复核已覆盖。",
        "data_review": "数据复核已覆盖。",
        "ref_review": "参考文献复核已覆盖。",
        "methodology_review": "",
        "verdict": verdict,
        "reason": reason,
    }


def _evidence_bundle(counts):
    base = {
        "high_findings": 0,
        "main_figures": 0,
        "duplicate_column_pairs": 0,
        "cross_sheet_reuse": 0,
        "decimal_precision_mismatch": 0,
        "total_must_address": 0,
    }
    base.update(counts)
    return {
        "coverage_manifest": {
            "counts": base,
            "must_address": [f"item::{i}" for i in range(base["total_must_address"])],
        },
        "high_findings": [],
        "main_figures": [],
        "deterministic_findings": {
            "duplicate_column_pairs": [],
            "cross_sheet_reuse": [],
            "decimal_precision_mismatch": [],
        },
    }


class AdaptiveReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.input_dir = self.root / "input"
        self.input_dir.mkdir()
        self.report = self.root / "report.json"
        self.report.write_text("{}", encoding="utf-8")
        self.output_dir = self.root / "out"
        self.output_dir.mkdir()
        self.evidence_path = self.root / "evidence.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_evidence(self, counts):
        self.evidence_path.write_text(
            json.dumps(_evidence_bundle(counts), ensure_ascii=False),
            encoding="utf-8",
        )
        return str(self.evidence_path)

    def _patch_state_dir(self):
        return mock.patch.object(review, "REVIEW_STATE_DIR", self.state_dir)

    def test_extract_json_tolerates_unescaped_quotes_in_review_text(self):
        text = '''prefix
```json
{
  "doi": "10.1/test",
  "result": "低风险",
  "trigger": "ok",
  "image_review": "图像正常",
  "data_review": "论文正文写到"secondary supply ratio remains stable"，这是模型性质。",
  "ref_review": "参考文献正常",
  "methodology_review": "",
  "verdict": "建议低风险",
  "reason": "可解释"
}
```
'''
        parsed = review._extract_json_from_text(text)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["result"], "低风险")
        self.assertIn("secondary supply ratio", parsed["data_review"])

    async def test_high_risk_first_reviewer_returns_without_escalation(self):
        self._write_evidence({"total_must_address": 4})
        run_with_retry = mock.AsyncMock(return_value=_review_result("高风险"))
        judge = mock.AsyncMock()

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
             mock.patch.object(review, "_run_with_retry", run_with_retry), \
             mock.patch.object(review, "_run_judge", judge):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "高风险")
        self.assertEqual(run_with_retry.await_count, 1)
        self.assertEqual(run_with_retry.await_args.kwargs["agent_id"], 1)
        judge.assert_not_awaited()
        self.assertTrue(list(self.state_dir.glob("*.json")))

    async def test_low_risk_requires_second_verifier_but_skips_judge_on_consensus(self):
        self._write_evidence({"main_figures": 1, "total_must_address": 1})
        run_with_retry = mock.AsyncMock(return_value=_review_result("低风险"))
        verifier = mock.AsyncMock(return_value=_review_result("低风险", reason="二次核验低风险"))
        judge = mock.AsyncMock()

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
             mock.patch.object(review, "_run_with_retry", run_with_retry), \
             mock.patch.object(review, "_run_lowrisk_verifier_with_retry", verifier), \
             mock.patch.object(review, "_run_judge", judge):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "低风险")
        self.assertEqual(run_with_retry.await_count, 1)
        verifier.assert_awaited_once()
        judge.assert_not_awaited()

    async def test_low_risk_disagreement_escalates_to_judge(self):
        self._write_evidence({"high_findings": 5, "total_must_address": 10})
        run_with_retry = mock.AsyncMock(return_value=_review_result("低风险", reason="首轮低风险"))
        verifier = mock.AsyncMock(return_value=_review_result("高风险", reason="二次核验发现问题"))
        judge = mock.AsyncMock(return_value=_review_result("低风险", reason="judge低风险"))

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
             mock.patch.object(review, "_run_with_retry", run_with_retry), \
             mock.patch.object(review, "_run_lowrisk_verifier_with_retry", verifier), \
             mock.patch.object(review, "_run_judge", judge):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "低风险")
        self.assertEqual(run_with_retry.await_count, 1)
        self.assertEqual(run_with_retry.await_args.kwargs["agent_id"], 1)
        verifier.assert_awaited_once()
        judge.assert_awaited_once()

    async def test_reviewer1_failure_uses_full_reviewer2_and_judge(self):
        self._write_evidence({"total_must_address": 2})
        run_with_retry = mock.AsyncMock(side_effect=[
            _review_result("高风险", trigger="review_error", reason="首轮失败"),
            _review_result("低风险", reason="普通二轮低风险"),
        ])
        verifier = mock.AsyncMock()
        judge = mock.AsyncMock(return_value=_review_result("高风险", reason="失败后终审高风险"))

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
             mock.patch.object(review, "_run_with_retry", run_with_retry), \
             mock.patch.object(review, "_run_lowrisk_verifier_with_retry", verifier), \
             mock.patch.object(review, "_run_judge", judge):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "高风险")
        self.assertEqual(run_with_retry.await_count, 2)
        self.assertEqual(run_with_retry.await_args_list[0].kwargs["agent_id"], 1)
        self.assertEqual(run_with_retry.await_args_list[1].kwargs["agent_id"], 2)
        verifier.assert_not_awaited()
        judge.assert_awaited_once()

    async def test_coverage_warning_does_not_flip_double_low_to_high(self):
        self._write_evidence({"total_must_address": 3})
        first = _review_result("低风险", reason="首轮低风险")
        first_validated = _review_result("低风险", reason="覆盖率留痕不足但结论低风险")
        first_validated["_coverage_status"] = "warning"
        first_validated["_coverage_gaps"] = ["未见图文一致性核查证据"]
        second = _review_result("低风险", reason="二次核验低风险")
        judge = mock.AsyncMock()

        def validate_side_effect(_doi, _report, _input, result, _evidence):
            if result is first:
                return first_validated
            return result

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=validate_side_effect), \
             mock.patch.object(review, "_run_with_retry", mock.AsyncMock(return_value=first)), \
             mock.patch.object(review, "_run_lowrisk_verifier_with_retry", mock.AsyncMock(return_value=second)) as verifier, \
             mock.patch.object(review, "_run_judge", judge):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "低风险")
        self.assertEqual(result.get("_coverage_status"), "warning")
        verifier.assert_awaited_once()
        judge.assert_not_awaited()

    async def test_lowrisk_verifier_failure_escalates_to_judge(self):
        self._write_evidence({"total_must_address": 2})
        run_with_retry = mock.AsyncMock(return_value=_review_result("低风险", reason="首轮低风险"))
        verifier = mock.AsyncMock(return_value=_review_result(
            "高风险", trigger="review_error", reason="二次核验失败"
        ))
        judge = mock.AsyncMock(return_value=_review_result("高风险", reason="核验失败后终审高风险"))

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
             mock.patch.object(review, "_run_with_retry", run_with_retry), \
             mock.patch.object(review, "_run_lowrisk_verifier_with_retry", verifier), \
             mock.patch.object(review, "_run_judge", judge):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "高风险")
        verifier.assert_awaited_once()
        judge.assert_awaited_once()

    async def test_lowrisk_verifier_and_judge_process_failures_do_not_create_high_risk(self):
        self._write_evidence({"total_must_address": 2})
        first = _review_result("低风险", reason="首轮实质复核低风险")
        verifier_error = _review_result("高风险", trigger="review_error", reason="二次核验失败")
        judge_error = _review_result("高风险", trigger="review_error", reason="终审失败")

        with self._patch_state_dir(), \
             mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
             mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
             mock.patch.object(review, "_run_with_retry", mock.AsyncMock(return_value=first)), \
             mock.patch.object(review, "_run_lowrisk_verifier_with_retry", mock.AsyncMock(return_value=verifier_error)), \
             mock.patch.object(review, "_run_judge", mock.AsyncMock(return_value=judge_error)):
            result = await review.run_review_single(
                "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
            )

        self.assertEqual(result["result"], "低风险")
        self.assertEqual(result["verdict"], "建议低风险")
        self.assertIn("流程错误", result.get("_review_warning", ""))

    async def test_cached_final_skips_evidence_and_agents(self):
        final = _review_result("低风险", reason="已有最终结果")

        with self._patch_state_dir():
            state_path = review._review_state_path(
                "10.1/test", str(self.report), str(self.input_dir)
            )
            state = review._new_review_state("10.1/test", str(self.report), str(self.input_dir))
            state["stages"]["final"] = final
            review._save_review_state(state_path, state)

            run_with_retry = mock.AsyncMock()
            with mock.patch.object(review, "_ensure_evidence_bundle") as ensure_evidence, \
                 mock.patch.object(review, "_run_with_retry", run_with_retry):
                result = await review.run_review_single(
                    "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
                )

        self.assertEqual(result["reason"], "已有最终结果")
        ensure_evidence.assert_not_called()
        run_with_retry.assert_not_awaited()

    async def test_cached_review_error_final_is_repaired_from_substantive_review(self):
        first = _review_result("低风险", reason="已有实质低风险复核")
        failed_final = _review_result("高风险", trigger="review_error", reason="旧版流程失败上调")

        with self._patch_state_dir():
            state_path = review._review_state_path(
                "10.1/test", str(self.report), str(self.input_dir)
            )
            state = review._new_review_state("10.1/test", str(self.report), str(self.input_dir))
            state["stages"]["reviewer1_validated"] = first
            state["stages"]["reviewer2_validated"] = _review_result(
                "高风险", trigger="review_error", reason="二次核验失败"
            )
            state["stages"]["final"] = failed_final
            review._save_review_state(state_path, state)

            with mock.patch.object(review, "_ensure_evidence_bundle") as ensure_evidence:
                result = await review.run_review_single(
                    "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
                )

        self.assertEqual(result["result"], "低风险")
        self.assertIn("流程错误", result.get("_review_warning", ""))
        ensure_evidence.assert_not_called()

    async def test_cached_review_error_without_substantive_result_is_rerun(self):
        self._write_evidence({"total_must_address": 2})
        failed = _review_result("高风险", trigger="review_error", reason="Reviewer exhausted retries")
        rerun_result = _review_result("高风险", reason="重新复核后的实质高风险")

        with self._patch_state_dir():
            state_path = review._review_state_path(
                "10.1/test", str(self.report), str(self.input_dir)
            )
            state = review._new_review_state("10.1/test", str(self.report), str(self.input_dir))
            state["stages"]["evidence_path"] = str(self.evidence_path)
            state["stages"]["reviewer1"] = failed
            state["stages"]["reviewer1_validated"] = failed
            state["stages"]["reviewer2"] = failed
            state["stages"]["reviewer2_validated"] = failed
            state["stages"]["judge"] = failed
            state["stages"]["final"] = failed
            review._save_review_state(state_path, state)

            run_with_retry = mock.AsyncMock(return_value=rerun_result)
            with mock.patch.object(review, "_ensure_evidence_bundle", return_value=str(self.evidence_path)), \
                 mock.patch.object(review, "_validate_review_result", side_effect=lambda *args: args[3]), \
                 mock.patch.object(review, "_run_with_retry", run_with_retry), \
                 mock.patch.object(review, "_run_judge", mock.AsyncMock()) as judge:
                result = await review.run_review_single(
                    "10.1/test", str(self.report), str(self.input_dir), str(self.output_dir)
                )

        self.assertEqual(result["reason"], "重新复核后的实质高风险")
        self.assertEqual(run_with_retry.await_count, 1)
        judge.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
