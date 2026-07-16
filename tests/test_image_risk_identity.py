from modules.chinese_report_generator import _is_cross_image_duplicate


def test_same_page_in_same_pdf_is_not_cross_image_evidence():
    issue = {"image_a": "a.png", "image_b": "b.png", "page_a": 3, "page_b": 3,
             "source_a": "/paper/main.pdf", "source_b": "/paper/main.pdf"}
    assert not _is_cross_image_duplicate(issue)


def test_same_page_number_in_different_pdfs_is_cross_image_evidence():
    issue = {"image_a": "a.png", "image_b": "b.png", "page_a": 3, "page_b": 3,
             "source_a": "/paper/main.pdf", "source_b": "/paper/supplement.pdf"}
    assert _is_cross_image_duplicate(issue)


def test_unconfirmed_sift_candidate_does_not_contribute_to_risk():
    issue = {"image_a": "a.png", "image_b": "b.png", "page_a": 3, "page_b": 9,
             "source_a": "/paper/main.pdf", "source_b": "/paper/main.pdf",
             "risk_evidence": False}
    assert not _is_cross_image_duplicate(issue)
