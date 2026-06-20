"""Verify that review_error placeholder results never reach a user-facing PDF.

Three layers must be tested:

1. ``api.services.report.generate_reports`` — raises ValueError for any
   review_error result so the report script is never invoked.
2. ``batch_rereview.generate_reports`` — filters review_error results out of
   the batch file written for the report script.
3. ``generate_review_report.py`` (CLI entry) — refuses to render a PDF and
   exits with non-zero status when the input results file contains a
   review_error entry.

These three layers together guarantee that no user-facing PDF can ever
contain the placeholder text "复核过程中出现错误" / "复核流程执行失败" / etc.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# generate_review_report.py lives in the user-level skills directory, NOT under
# the repo. Resolve via the conventional location first, then fall back to the
# repo-local copy if installed there.
_SCRIPT_CANDIDATES = [
    Path("/opt/.claude/skills/paper-batch-review/scripts/generate_review_report.py"),
    REPO_ROOT / ".claude/skills/paper-batch-review/scripts/generate_review_report.py",
]
_GENERATE_REPORT_SCRIPT = next((p for p in _SCRIPT_CANDIDATES if p.exists()), _SCRIPT_CANDIDATES[0])


def _make_review_error_result(doi: str = "10.1/bad") -> dict:
    return {
        "doi": doi,
        "result": "高风险",
        "trigger": "review_error",
        "image_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "data_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "ref_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
        "verdict": "建议高风险",
        "reason": "复核流程执行失败，未获得可解析的复核结果；按规则暂列建议高风险，建议人工进一步确认。",
    }


def _make_good_result(doi: str = "10.1/good") -> dict:
    return {
        "doi": doi,
        "result": "高风险",
        "trigger": "image_reuse",
        "image_review": "Fig.1a 与 Fig.3b 跨图逐像素一致(同位内参控图)。",
        "data_review": "数据维度无独立支撑高风险结论的可疑模式。",
        "ref_review": "参考文献无异常。",
        "verdict": "建议高风险",
        "reason": "图像源文件复用,未在图注说明",
    }


class TestApiReportServiceRefusesReviewError(unittest.IsolatedAsyncioTestCase):
    async def test_review_error_raises_value_error(self):
        from api.services import report
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir()
            with self.assertRaisesRegex(ValueError, "Refusing to generate final review PDF"):
                await report.generate_reports(
                    [_make_review_error_result()],
                    task_dir,
                )

    async def test_good_results_still_pass(self):
        from api.services import report
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "review"
            task_dir = Path(tmp) / "task"
            review_dir.mkdir()
            task_dir.mkdir()
            expected = review_dir / "yujing_quanliang" / "review_10.1_good.pdf"

            class FakeProc:
                returncode = 0

                async def communicate(self):
                    expected.write_text("ok", encoding="utf-8")
                    return b"ok", b""

                def kill(self):
                    pass

            async def fake_create(*args, **kwargs):
                return FakeProc()

            with mock.patch.object(report, "REVIEW_DIR", review_dir), \
                 mock.patch.object(report.asyncio, "create_subprocess_exec", fake_create):
                paths = await report.generate_reports(
                    [_make_good_result()],
                    task_dir,
                )
            self.assertEqual(paths, [str(expected)])


class TestBatchRereviewSplitsReviewErrors(unittest.TestCase):
    def test_helper_detects_trigger_and_text_markers(self):
        spec = importlib.util.spec_from_file_location(
            "batch_rereview", REPO_ROOT / "batch_rereview.py"
        )
        br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)

        # 1. Plain trigger=review_error
        self.assertTrue(br._is_review_error(_make_review_error_result()))
        # 2. Text-marker fallback (someone forgot to set trigger but kept the text)
        bad_text_only = _make_good_result("10.1/bad-text")
        bad_text_only["image_review"] = "复核过程中出现错误"  # text marker
        self.assertTrue(br._is_review_error(bad_text_only))
        # 3. Genuinely good result
        self.assertFalse(br._is_review_error(_make_good_result()))

    def test_split_separates_renderable_and_failed(self):
        spec = importlib.util.spec_from_file_location(
            "batch_rereview", REPO_ROOT / "batch_rereview.py"
        )
        br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)

        results = [
            _make_good_result("10.1/good-1"),
            _make_review_error_result("10.1/bad-1"),
            _make_good_result("10.1/good-2"),
            _make_review_error_result("10.1/bad-2"),
        ]
        renderable, failed = br._split_review_errors(results)
        self.assertEqual(len(renderable), 2)
        self.assertEqual(len(failed), 2)
        self.assertEqual(
            sorted(r.get("doi") for r in failed),
            ["10.1/bad-1", "10.1/bad-2"],
        )
        self.assertEqual(
            sorted(r.get("doi") for r in renderable),
            ["10.1/good-1", "10.1/good-2"],
        )


class TestGenerateReviewReportCliRefusesReviewError(unittest.TestCase):
    """Direct CLI test: the script must not render a PDF and must exit non-zero
    when the input results file contains a review_error entry."""

    SCRIPT = _GENERATE_REPORT_SCRIPT

    def setUp(self):
        if not self.SCRIPT.exists():
            # The skill may be installed at a different path; skip if absent
            self.skipTest(f"generate_review_report.py not found at {self.SCRIPT}")

    def test_review_error_results_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            results_file = tmp / "results.json"
            output_dir = tmp / "output"
            results_file.write_text(
                json.dumps(
                    [_make_review_error_result("10.1/bad-1"),
                     _make_good_result("10.1/good-1")],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            proc = subprocess.run(
                ["python3", str(self.SCRIPT),
                 "--results", str(results_file),
                 "--output", str(output_dir),
                 "--no-db"],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )

            # The bad entry must be flagged in stderr
            self.assertIn("REVIEW_ERROR", proc.stderr)
            self.assertIn("10.1/bad-1", proc.stderr)

            # The good entry must still produce a PDF
            good_pdf = output_dir / "review_10.1_good-1.pdf"
            self.assertTrue(good_pdf.exists(),
                            f"good result should have produced a PDF at {good_pdf}")

            # The bad entry must NOT produce a PDF
            bad_pdf = output_dir / "review_10.1_bad-1.pdf"
            self.assertFalse(bad_pdf.exists(),
                             f"bad (review_error) result must not produce a PDF, but {bad_pdf} exists")

            # Exit code must be 2 so callers can detect the failure
            self.assertEqual(proc.returncode, 2,
                             f"expected exit code 2 when review_error present, got {proc.returncode}")


if __name__ == "__main__":
    unittest.main()
