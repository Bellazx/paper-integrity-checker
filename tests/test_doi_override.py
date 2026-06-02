"""User-supplied DOI override reaches the detection flow on BOTH paths.

Verifies the override is authoritative — it must land in findings["paper"]["doi"]
and be passed to the reference self-DOI header filter — for the normal PDF path
(analyze_paper) and the Nature crawl path (analyze_nature_paper). Heavy collaborators
(image/data/splice/refs/PDF) are patched out; this isolates the DOI plumbing.

Run:  python3 -m unittest tests.test_doi_override
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.pipeline as P
import core.nature_adapter as N

OVERRIDE = "10.9999/override.test.doi"


class NormalPathDoiOverride(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="t_doi_")

    @mock.patch.object(P, "check_references")
    @mock.patch("utils.crossref.enrich_metadata", return_value=None)
    @mock.patch.object(P, "check_data_anomalies", return_value=[])
    @mock.patch.object(P, "check_splicing", return_value=[])
    @mock.patch.object(P, "check_image_duplicates", return_value=[])
    @mock.patch("utils.pdf_utils.extract_standalone_images", return_value=[])
    @mock.patch.object(P, "extract_images", return_value=[])
    @mock.patch.object(P, "extract_text", return_value=[{"text": "References\n[1] x"}])
    @mock.patch.object(P, "generate_chinese_pdf", return_value=("/tmp/x.pdf", {"authors_full": [], "affiliations": []}))
    @mock.patch.object(P, "extract_metadata", return_value={"title": "T", "author": "", "doi": "10.1/original", "journal": "J"})
    @mock.patch.object(P, "find_data_dir", return_value=None)
    @mock.patch.object(P, "find_pdf", return_value="/tmp/fake.pdf")
    @mock.patch("fitz.open")
    def test_override_reaches_findings_and_refcheck(
        self, m_fitz, m_findpdf, m_finddata, m_meta, m_pdf, m_text,
        m_imgs, m_stand, m_imgdup, m_splice, m_data, m_enrich, m_refs,
    ):
        m_fitz.return_value = mock.MagicMock(__len__=lambda s: 1, metadata={}, close=lambda: None)
        m_refs.return_value = []

        findings = P.analyze_paper("/tmp/paper", self.out, skip_refs=False, doi_override=OVERRIDE)

        # 1) override is authoritative in the findings (beats extract_metadata's DOI)
        self.assertEqual(findings["paper"]["doi"], OVERRIDE)
        # 2) override was passed to the reference self-DOI header filter
        m_refs.assert_called_once()
        _pdf_arg, doi_arg = m_refs.call_args[0]
        self.assertEqual(doi_arg, OVERRIDE)

    @mock.patch.object(P, "check_references", return_value=[])
    @mock.patch("utils.crossref.enrich_metadata", return_value=None)
    @mock.patch.object(P, "check_data_anomalies", return_value=[])
    @mock.patch.object(P, "check_splicing", return_value=[])
    @mock.patch.object(P, "check_image_duplicates", return_value=[])
    @mock.patch("utils.pdf_utils.extract_standalone_images", return_value=[])
    @mock.patch.object(P, "extract_images", return_value=[])
    @mock.patch.object(P, "extract_text", return_value=[{"text": ""}])
    @mock.patch.object(P, "generate_chinese_pdf", return_value=("/tmp/x.pdf", {"authors_full": [], "affiliations": []}))
    @mock.patch.object(P, "extract_metadata", return_value={"title": "T", "author": "", "doi": "10.1/original", "journal": "J"})
    @mock.patch.object(P, "find_data_dir", return_value=None)
    @mock.patch.object(P, "find_pdf", return_value="/tmp/fake.pdf")
    @mock.patch("fitz.open")
    def test_no_override_keeps_extracted_doi(
        self, m_fitz, m_findpdf, m_finddata, m_meta, m_pdf, m_text,
        m_imgs, m_stand, m_imgdup, m_splice, m_data, m_enrich, m_refs,
    ):
        m_fitz.return_value = mock.MagicMock(__len__=lambda s: 1, metadata={}, close=lambda: None)
        findings = P.analyze_paper("/tmp/paper", self.out, skip_refs=False, doi_override="")
        self.assertEqual(findings["paper"]["doi"], "10.1/original")


class NaturePathDoiOverride(unittest.TestCase):
    """The bug I initially missed: Nature path must honor the override too (symmetry)."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="t_doi_nat_")

    @mock.patch.object(N, "generate_chinese_pdf", return_value=("/tmp/x.pdf", {}))
    @mock.patch.object(N, "_find_sjtu_authors", return_value={"sjtu_authors": [], "sjtu_departments": []})
    @mock.patch.object(N, "extract_text_from_html", return_value="body")
    @mock.patch.object(N, "extract_references_from_html", return_value=[])
    @mock.patch.object(N, "check_references_from_html", return_value=[])
    @mock.patch.object(N, "_find_data_dirs", return_value=[])
    @mock.patch.object(N, "check_splicing", return_value=[])
    @mock.patch.object(N, "find_image_pdfs", return_value=[])
    @mock.patch.object(N, "_load_manifest", return_value={"status": "success"})
    @mock.patch.object(N, "extract_metadata_from_manifest")
    def test_override_beats_manifest_doi(
        self, m_meta, m_manifest, m_imgpdfs, m_splice, m_datadirs,
        m_refs_html, m_extref, m_text, m_sjtu, m_pdf,
    ):
        m_meta.return_value = {
            "title": "T", "doi": "10.1/manifest", "journal": "J", "author": "A",
            "authors_full": [], "affiliations": [], "dc_type": "Article", "dc_date": "",
        }
        with mock.patch.object(N.Path, "exists", return_value=True):
            findings = N.analyze_nature_paper("/tmp/nat", self.out, skip_refs=True, doi_override=OVERRIDE)
        self.assertEqual(findings["paper"]["doi"], OVERRIDE)


if __name__ == "__main__":
    unittest.main()
