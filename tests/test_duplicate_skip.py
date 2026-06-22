import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nature_adapter import find_image_pdfs
from modules.image_checker import check_image_duplicates
from utils.pdf_utils import ExtractedImage
from utils.pdf_utils import extract_standalone_images


class DuplicateSkipTests(unittest.TestCase):
    def test_standalone_image_duplicates_are_skipped_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            out = Path(tmp) / "out"
            root.mkdir()
            arr = np.full((240, 240, 3), 128, dtype=np.uint8)
            first = root / "figure1.png"
            second = root / "copy_figure1.png"
            Image.fromarray(arr).save(first)
            second.write_bytes(first.read_bytes())

            images = extract_standalone_images(str(root), str(out))

            self.assertEqual(len(images), 1)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_nature_supplementary_pdf_duplicates_are_skipped_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "paper"
            ext = root / "extended_data"
            ext.mkdir(parents=True)
            first = ext / "supp1.pdf"
            second = ext / "supp1_copy.pdf"
            first.write_bytes(b"%PDF duplicate bytes")
            second.write_bytes(first.read_bytes())

            pdfs = find_image_pdfs(str(root))

            self.assertEqual(len(pdfs), 1)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_duplicate_checker_skips_same_filepath_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            image_path = Path(tmp) / "page_003_img_000.png"
            arr = np.full((240, 240, 3), 128, dtype=np.uint8)
            Image.fromarray(arr).save(image_path)

            images = [
                ExtractedImage(3, 0, str(image_path), arr, 240, 240, xref=7),
                ExtractedImage(0, 0, str(image_path), arr.copy(), 240, 240),
            ]

            matches = check_image_duplicates(images, str(out))

            self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
