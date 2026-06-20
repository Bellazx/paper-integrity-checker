import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.doi_utils import normalize_doi


class DoiNormalization(unittest.TestCase):
    def test_polluted_oup_doi_is_truncated(self):
        value = (
            "10.1210/clinem/dgac577; The Journal of Clinical Endocrinology & Metabolism, "
            "108, 2. Abstract Context Thyroid hormones are essential"
        )
        self.assertEqual(normalize_doi(value), "10.1210/clinem/dgac577")

    def test_folder_encoded_extra_slash_is_restored(self):
        self.assertEqual(normalize_doi("10.1210/clinem_dgac577"), "10.1210/clinem/dgac577")
        self.assertEqual(normalize_doi("10.1088/1361-6560_ab9f60"), "10.1088/1361-6560/ab9f60")

    def test_existing_doi_remains_unchanged(self):
        self.assertEqual(normalize_doi("10.6061/clinics/2021/e1713"), "10.6061/clinics/2021/e1713")


if __name__ == "__main__":
    unittest.main()
