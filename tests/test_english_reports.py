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


def test_english_json_call_retries_once_on_malformed_json(monkeypatch):
    from modules import english_report_generator as eg
    replies = iter(['{"overview": "a" "image_review": "b"}', '{"overview": "A", "image_review": "B"}'])
    prompts = []

    def fake_chat(prompt, system=None, temperature=None):
        prompts.append(prompt)
        return next(replies)
    monkeypatch.setattr(eg, "chat", fake_chat)
    out = eg._chat_json("translate", ("overview", "image_review"), strict_missing=True)
    assert out == {"overview": "A", "image_review": "B"}
    assert len(prompts) == 2 and "was not valid JSON" in prompts[1] and prompts[1].startswith("translate")


def test_english_json_call_gives_up_after_two_malformed_replies(monkeypatch):
    import pytest
    from modules import english_report_generator as eg
    monkeypatch.setattr(eg, "chat", lambda prompt, system=None, temperature=None: "not json at all")
    with pytest.raises(ValueError, match="invalid JSON 2 times"):
        eg._chat_json("translate", ("overview",), strict_missing=True)


def test_english_json_call_does_not_retry_missing_fields(monkeypatch):
    import pytest
    from modules import english_report_generator as eg
    calls = []

    def fake_chat(prompt, system=None, temperature=None):
        calls.append(prompt)
        return '{"overview": "A"}'
    monkeypatch.setattr(eg, "chat", fake_chat)
    with pytest.raises(ValueError, match="missing fields"):
        eg._chat_json("translate", ("overview", "image_review"), strict_missing=True)
    assert len(calls) == 1
