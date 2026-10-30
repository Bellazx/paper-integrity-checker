from pathlib import Path

from api.models import DetectionSubmitRequest, PaperInfo
from api.services.detection_reports_worker import (
    _english_initial_report_url,
    _english_review_report_url,
)


def test_submit_language_defaults_to_bilingual_and_accepts_english():
    assert DetectionSubmitRequest(file_id="x").language == "both"
    assert DetectionSubmitRequest(file_id="x", language="en").language == "en"


def test_english_report_urls_use_gateway_paths():
    findings = {"paper": {"doi": "10.1000/example", "title": "Example"}}
    initial = _english_initial_report_url(findings, "detection_reports/task-1")
    assert initial.startswith("http://202.120.57.100/english_reports/")
    assert initial.endswith("_en.pdf")

    paper = PaperInfo(doi_slug="10.1000_example", doi="10.1000/example")
    review = {"doi": "10.1000/example"}
    review_url = _english_review_report_url(paper, review, "detection_reports/task-1")
    assert "/review_reports/detection_reports/task-1/review_10.1000_example_en.pdf" in review_url
