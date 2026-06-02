"""Report-URL construction matches the actual PDF filename.

Finding #6: the custom-table insert built "{doi_slug}.pdf", but the file written by
generate_chinese_pdf is _make_filename(doi, title) = "{doi}_{title30}.pdf". This locks
the URL to the same scheme via the shared utils.db._make_report_url helper, so a DB row
never points at a nonexistent report.

Run:  python3 -m unittest tests.test_report_url
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.chinese_report_generator import _make_filename, doi_to_slug
from utils.db import _make_report_url, REPORT_BASE_URL


class ReportUrlScheme(unittest.TestCase):
    def test_url_matches_make_filename(self):
        findings = {"paper": {"doi": "10.1186/s12951-024-02315-9", "title": "A Nano Delivery System"}}
        url = _make_report_url(findings)
        expect = f"{REPORT_BASE_URL}/{_make_filename('10.1186/s12951-024-02315-9', 'A Nano Delivery System')}"
        self.assertEqual(url, expect)

    def test_url_is_not_the_old_doi_slug_only_form(self):
        # The pre-fix custom-table form. Guard against regressing to it.
        findings = {"paper": {"doi": "10.1/x", "title": "Some Title"}}
        old_broken = f"http://10.119.9.99/chinese_reports/{'10.1/x'.replace('/', '_')}.pdf"
        self.assertNotEqual(_make_report_url(findings), old_broken)

    def test_filename_slashes_and_title_prefix(self):
        fn = _make_filename("10.1186/s12951-024-02315-9", "A Very Long Title " * 5)
        self.assertNotIn("/", fn)              # DOI slashes flattened to _
        self.assertTrue(fn.endswith(".pdf"))
        self.assertIn("10.1186_s12951-024-02315-9", fn)

    def test_unknown_doi_does_not_crash(self):
        url = _make_report_url({"paper": {}})
        self.assertTrue(url.startswith(REPORT_BASE_URL))
        self.assertTrue(url.endswith(".pdf"))


class DoiSlugConsistency(unittest.TestCase):
    """#3: 初审 (_make_filename) and 复核 (doi_to_slug) must clean DOIs identically so a
    stored review URL always matches the file on disk."""

    def test_slug_is_prefix_of_make_filename_doi_part(self):
        for doi in ("10.1/x", "10.1186/s12951-024-02315-9", "10.1002/abc:def?x"):
            slug = doi_to_slug(doi)
            # _make_filename builds "{doi_part}_{title30}.pdf"; the doi part must equal slug
            self.assertTrue(_make_filename(doi, "T").startswith(slug + "_"),
                            f"slug {slug!r} not the doi stem of {_make_filename(doi, 'T')!r}")

    def test_special_chars_scrubbed(self):
        # plain doi.replace('/','_') would leave ':' and '?' — the old divergence.
        slug = doi_to_slug("10.1002/abc:def?x")
        for ch in ':?<>"\\|*':
            self.assertNotIn(ch, slug)

    def test_doi_org_prefix_stripped(self):
        self.assertEqual(doi_to_slug("https://doi.org/10.1/x"), doi_to_slug("10.1/x"))

    def test_empty_doi(self):
        self.assertEqual(doi_to_slug(""), "")
        self.assertEqual(doi_to_slug(None), "")


if __name__ == "__main__":
    unittest.main()
