import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.services.report as report


class FakeProc:
    def __init__(self, action=None, returncode=0):
        self.returncode = returncode
        self._action = action

    async def communicate(self):
        if self._action:
            self._action()
        return b"ok", b""

    def kill(self):
        pass


class GenerateReports(unittest.IsolatedAsyncioTestCase):
    async def test_review_error_results_are_not_rendered_as_final_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / "task"
            task_dir.mkdir()

            with self.assertRaisesRegex(ValueError, "Refusing to generate final review PDF"):
                await report.generate_reports(
                    [{
                        "doi": "10.1/x",
                        "result": "高风险",
                        "trigger": "review_error",
                        "reason": "Reviewer 1 exhausted retries",
                    }],
                    task_dir,
                )

    async def test_stale_existing_pdf_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "review"
            task_dir = Path(tmp) / "task"
            review_dir.mkdir()
            task_dir.mkdir()
            stale = review_dir / "yujing_quanliang" / "review_10.1_x.pdf"
            stale.parent.mkdir()
            stale.write_text("old", encoding="utf-8")

            async def fake_create(*args, **kwargs):
                return FakeProc()

            with mock.patch.object(report, "REVIEW_DIR", review_dir), \
                 mock.patch.object(report.asyncio, "create_subprocess_exec", fake_create):
                paths = await report.generate_reports([{"doi": "10.1/x"}], task_dir)

            self.assertEqual(paths, [])

    async def test_new_expected_pdf_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "review"
            task_dir = Path(tmp) / "task"
            review_dir.mkdir()
            task_dir.mkdir()
            expected = review_dir / "yujing_quanliang" / "review_10.1_x.pdf"

            async def fake_create(*args, **kwargs):
                return FakeProc(action=lambda: expected.write_text("new", encoding="utf-8"))

            with mock.patch.object(report, "REVIEW_DIR", review_dir), \
                 mock.patch.object(report.asyncio, "create_subprocess_exec", fake_create):
                paths = await report.generate_reports([{"doi": "10.1/x"}], task_dir)

            self.assertEqual(paths, [str(expected)])

    async def test_report_namespace_is_used_for_output_and_script_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "review"
            task_dir = Path(tmp) / "task"
            review_dir.mkdir()
            task_dir.mkdir()
            expected = review_dir / "detection_reports" / "review_10.1_x.pdf"
            seen_cmd = {}

            async def fake_create(*args, **kwargs):
                seen_cmd["args"] = args
                return FakeProc(action=lambda: expected.write_text("new", encoding="utf-8"))

            with mock.patch.object(report, "REVIEW_DIR", review_dir), \
                 mock.patch.object(report.asyncio, "create_subprocess_exec", fake_create):
                paths = await report.generate_reports(
                    [{"doi": "10.1/x"}],
                    task_dir,
                    table_name="detection_reports",
                    write_db=False,
                    report_namespace="detection_reports",
                )

            self.assertEqual(paths, [str(expected)])
            self.assertIn("--namespace", seen_cmd["args"])
            self.assertIn("detection_reports", seen_cmd["args"])


if __name__ == "__main__":
    unittest.main()
