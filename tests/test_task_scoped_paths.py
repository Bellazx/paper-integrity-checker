import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.config import sanitize_report_namespace, upload_dir_for_file_id
import api.services.zip_handler as zh


class TaskScopedPaths(unittest.TestCase):
    def _make_batch_zip(self, path: Path):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("batch/paper_a/doi.txt", "10.1234/same")
            zf.writestr("batch/paper_a/a.pdf", "%PDF-1.4\n")
            zf.writestr("batch/paper_b/doi.txt", "10.1234/same")
            zf.writestr("batch/paper_b/b.pdf", "%PDF-1.4\n")

    def test_upload_dir_uses_date_task_bucket(self):
        self.assertEqual(
            upload_dir_for_file_id("20260603123456_user_abcd").as_posix(),
            "/opt/paper-integrity-checker/data/uploads/20260603-task/20260603123456_user_abcd",
        )

    def test_batch_extract_is_task_scoped_and_de_duplicates_same_doi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "upload.zip"
            self._make_batch_zip(archive)
            input_root = root / "input" / "20260603-task" / "task123"
            output_root = root / "output" / "tasks" / "task123"

            with mock.patch.object(zh, "input_task_root", return_value=input_root), \
                 mock.patch.object(zh, "output_task_root", return_value=output_root):
                papers = zh.extract_batch(archive, "task123")

            self.assertEqual([Path(p.input_dir).parent for p in papers], [input_root, input_root])
            self.assertEqual([Path(p.input_dir).name for p in papers], ["10.1234_same", "10.1234_same__2"])
            self.assertEqual([Path(p.output_dir) for p in papers], [
                output_root / "10.1234_same",
                output_root / "10.1234_same__2",
            ])

    def test_report_namespace_allows_safe_task_subdirectory(self):
        self.assertEqual(
            sanitize_report_namespace("../detection_reports//20260603-task?.x"),
            "detection_reports/20260603-task_.x",
        )


if __name__ == "__main__":
    unittest.main()
