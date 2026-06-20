import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.paper_preprocessor import clean_junk_files, preprocess_paper_dir


class PaperPreprocessorTests(unittest.TestCase):
    def test_default_dry_run_keeps_byte_identical_duplicate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            root.mkdir()
            (root / "Manuscript.pdf").write_bytes(b"%PDF same content")
            nested = root / "copy"
            nested.mkdir()
            (nested / "peer_review.pdf").write_bytes(b"%PDF same content")
            (root / "different.pdf").write_bytes(b"%PDF different content")

            result = preprocess_paper_dir(root, log_fn=lambda _msg: None)

            self.assertEqual(result["actions"]["duplicates_removed"], 1)
            self.assertEqual(result["actions"]["duplicate_groups"], 1)
            self.assertTrue((root / "Manuscript.pdf").exists())
            self.assertTrue((nested / "peer_review.pdf").exists())
            self.assertTrue((root / "different.pdf").exists())

    def test_apply_removes_byte_identical_duplicate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            root.mkdir()
            (root / "Manuscript.pdf").write_bytes(b"%PDF same content")
            nested = root / "copy"
            nested.mkdir()
            (nested / "peer_review.pdf").write_bytes(b"%PDF same content")
            (root / "different.pdf").write_bytes(b"%PDF different content")

            result = preprocess_paper_dir(root, dry_run=False, log_fn=lambda _msg: None)

            self.assertEqual(result["actions"]["duplicates_removed"], 1)
            self.assertEqual(result["actions"]["duplicate_groups"], 1)
            self.assertTrue((root / "Manuscript.pdf").exists())
            self.assertFalse((nested / "peer_review.pdf").exists())
            self.assertTrue((root / "different.pdf").exists())

    def test_dry_run_reports_duplicates_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            root.mkdir()
            (root / "source_data.xlsx").write_bytes(b"same excel bytes")
            (root / "copy_source_data.xlsx").write_bytes(b"same excel bytes")

            result = preprocess_paper_dir(root, dry_run=True, log_fn=lambda _msg: None)

            self.assertEqual(result["actions"]["duplicates_removed"], 1)
            self.assertTrue((root / "source_data.xlsx").exists())
            self.assertTrue((root / "copy_source_data.xlsx").exists())

    def test_same_size_different_content_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            root.mkdir()
            (root / "a.pdf").write_bytes(b"abcdef")
            (root / "b.pdf").write_bytes(b"abcdeg")

            result = preprocess_paper_dir(root, log_fn=lambda _msg: None)

            self.assertEqual(result["actions"]["duplicates_removed"], 0)
            self.assertTrue((root / "a.pdf").exists())
            self.assertTrue((root / "b.pdf").exists())

    def test_apply_is_rejected_inside_protected_input_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data" / "input" / "paper"
            root.mkdir(parents=True)
            source = root / "source.pdf"
            duplicate = root / "duplicate.pdf"
            source.write_bytes(b"%PDF same content")
            duplicate.write_bytes(b"%PDF same content")

            protected_root = (Path(tmp) / "data" / "input").resolve()
            with mock.patch(
                "utils.paper_preprocessor._PROTECTED_INPUT_ROOTS",
                (protected_root,),
            ):
                with self.assertRaises(PermissionError):
                    preprocess_paper_dir(root, dry_run=False, log_fn=lambda _msg: None)

            self.assertTrue(source.exists())
            self.assertTrue(duplicate.exists())

    def test_clean_junk_files_removes_only_whitelisted_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            root.mkdir()
            junk_dir = root / "__MACOSX"
            junk_dir.mkdir()
            (junk_dir / "._source.xlsx").write_bytes(b"appledouble")
            ds = root / ".DS_Store"
            apple = root / "._figure.pdf"
            thumbs = root / "Thumbs.db"
            desktop = root / "desktop.ini"
            office_lock = root / "~$source.xlsx"
            real_pdf = root / "figure.pdf"
            real_xlsx = root / "source.xlsx"
            archive = root / "source.zip"
            for p in (ds, apple, thumbs, desktop, office_lock):
                p.write_bytes(b"junk")
            real_pdf.write_bytes(b"%PDF real")
            real_xlsx.write_bytes(b"xlsx real")
            archive.write_bytes(b"zip real")

            result = clean_junk_files(root, log_fn=lambda _msg: None)

            self.assertEqual(result["actions"]["junk_files_removed"], 6)
            self.assertEqual(result["actions"]["junk_dirs_removed"], 1)
            self.assertFalse(junk_dir.exists())
            for p in (ds, apple, thumbs, desktop, office_lock):
                self.assertFalse(p.exists())
            self.assertTrue(real_pdf.exists())
            self.assertTrue(real_xlsx.exists())
            self.assertTrue(archive.exists())

    def test_clean_junk_files_is_allowed_inside_protected_input_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data" / "input" / "paper"
            root.mkdir(parents=True)
            junk = root / "._source.pdf"
            real = root / "source.pdf"
            junk.write_bytes(b"appledouble")
            real.write_bytes(b"%PDF real")

            protected_root = (Path(tmp) / "data" / "input").resolve()
            with mock.patch(
                "utils.paper_preprocessor._PROTECTED_INPUT_ROOTS",
                (protected_root,),
            ):
                result = clean_junk_files(root, log_fn=lambda _msg: None)

            self.assertEqual(result["actions"]["junk_files_removed"], 1)
            self.assertFalse(junk.exists())
            self.assertTrue(real.exists())


if __name__ == "__main__":
    unittest.main()
