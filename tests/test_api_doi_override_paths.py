import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.config import OUTPUT_DIR
from api.models import PaperInfo
from api.services.zip_handler import apply_doi_override


class ApiDoiOverridePaths(unittest.TestCase):
    def test_override_renames_input_and_output_to_doi_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "task_abc"
            src.mkdir()
            paper = PaperInfo(
                doi_slug="task_abc",
                input_dir=str(src),
                output_dir=str(OUTPUT_DIR / "task_abc"),
            )

            apply_doi_override(paper, "10.1234/example.doi")

            expected_input = Path(tmp) / "10.1234_example.doi"
            self.assertEqual(Path(paper.input_dir), expected_input)
            self.assertTrue(expected_input.exists())
            self.assertFalse(src.exists())
            self.assertEqual((expected_input / "doi.txt").read_text(encoding="utf-8"), "10.1234/example.doi")
            self.assertEqual(paper.doi, "10.1234/example.doi")
            self.assertEqual(paper.doi_slug, "10.1234_example.doi")
            self.assertEqual(Path(paper.output_dir), OUTPUT_DIR / "10.1234_example.doi")


if __name__ == "__main__":
    unittest.main()
