import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.services.detect import run_detection_single


class _FakeProc:
    returncode = 0

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    async def communicate(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "report.json").write_text(
            json.dumps({
                "paper": {"doi": "10.1234/example", "title": "Example"},
                "summary": {},
                "pdf_generated": True,
            }),
            encoding="utf-8",
        )
        return b"", b""

    def kill(self):
        pass


class DetectionUsesSharedPdfTemplate(unittest.TestCase):
    def test_api_detection_delegates_initial_pdf_generation_to_main_pipeline(self):
        calls = []

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            calls.append((cmd, kwargs))
            output_root = Path(cmd[cmd.index("--output") + 1])
            input_dir = Path(cmd[cmd.index("--input") + 1])
            return _FakeProc(output_root / input_dir.name)

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("api.services.detect.asyncio.create_subprocess_exec", fake_create_subprocess_exec), \
             mock.patch("api.services.detect._insert_to_custom_table"):
            input_dir = Path(tmp) / "paper"
            input_dir.mkdir()
            output_dir = Path(tmp) / "output" / "paper"

            result = asyncio.run(run_detection_single(
                input_dir=str(input_dir),
                output_dir=str(output_dir),
                table_name="detection_reports",
                write_db=False,
                doi="10.1234/example",
                report_namespace="detection_reports/task123",
            ))

        self.assertEqual(result["paper"]["doi"], "10.1234/example")
        self.assertEqual(len(calls), 1)
        cmd, kwargs = calls[0]
        self.assertIn("main.py", str(cmd[1]))
        self.assertIn("--report-namespace", cmd)
        self.assertEqual(cmd[cmd.index("--report-namespace") + 1], "detection_reports/task123")
        self.assertIn("--no-db", cmd)
        self.assertEqual(Path(kwargs["cwd"]), Path(cmd[1]).parent)

    def test_api_layer_does_not_import_template_rendering_internals(self):
        api_files = Path("api").rglob("*.py")
        forbidden = ("_render_pdf", "_build_full_html", "generate_chinese_pdf")
        offenders = []
        for path in api_files:
            text = path.read_text(encoding="utf-8")
            if path.as_posix() == "api/services/detect.py":
                # detect.py may import scoring helpers, but must not render initial PDFs itself.
                pass
            for marker in forbidden:
                if marker in text:
                    offenders.append(f"{path}:{marker}")

        allowed = {
            "api/services/detect.py:generate_chinese_pdf",
        }
        offenders = [o for o in offenders if o not in allowed]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
