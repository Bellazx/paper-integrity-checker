import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.doi_utils import choose_authoritative_doi, normalize_doi


class DoiUtilsTests(unittest.TestCase):
    def test_normal_doi_is_unchanged(self):
        self.assertEqual(
            normalize_doi("10.1002/cjoc.202300313"),
            "10.1002/cjoc.202300313",
        )

    def test_doi_url_prefix_is_removed(self):
        self.assertEqual(
            normalize_doi("https://doi.org/10.1038/s41467-026-71010-0"),
            "10.1038/s41467-026-71010-0",
        )

    def test_missing_slash_typo_is_repaired(self):
        self.assertEqual(
            normalize_doi("10.1038s41467-026-71010-0"),
            "10.1038/s41467-026-71010-0",
        )

    def test_embedded_doi_is_extracted(self):
        self.assertEqual(
            normalize_doi("DOI: 10.1002/cjoc.202300313."),
            "10.1002/cjoc.202300313",
        )

    def test_non_doi_placeholders_are_empty(self):
        for value in ("N/A", "unknown", "无", "not available"):
            with self.subTest(value=value):
                self.assertEqual(normalize_doi(value), "")

    def test_choose_authoritative_doi_preserves_supplied_doi(self):
        doi, changed, conflict = choose_authoritative_doi(
            "10.1002/cjoc.202300313",
            "10.1038/s41467-026-71010-0",
        )

        self.assertEqual(doi, "10.1002/cjoc.202300313")
        self.assertFalse(changed)
        self.assertTrue(conflict)

    def test_choose_authoritative_doi_fills_empty_current(self):
        doi, changed, conflict = choose_authoritative_doi(
            "N/A",
            "DOI: 10.1038/s41467-026-71010-0.",
        )

        self.assertEqual(doi, "10.1038/s41467-026-71010-0")
        self.assertTrue(changed)
        self.assertFalse(conflict)


if __name__ == "__main__":
    unittest.main()
