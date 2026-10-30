from datetime import datetime

import pytest

from scripts import recover_initial_reports as recovery


def test_old_public_pdf_is_not_a_completed_regeneration(tmp_path):
    import fitz

    pdf = tmp_path / "old.pdf"
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "Original screening report")
        document.save(pdf)
    assert not recovery.pdf_ready({"pdf_generated": False}, pdf)
    assert recovery.pdf_ready({"pdf_generated": True}, pdf)


def test_swallowed_db_error_does_not_count_as_success(monkeypatch):
    import utils.db

    monkeypatch.setattr(utils.db, "insert_findings", lambda *a, **kw: None)
    monkeypatch.setattr(recovery.time, "sleep", lambda *a: None)
    monkeypatch.setattr(recovery, "db_row", lambda _: {
        "generation_time": datetime(2026, 10, 23),
        "report_url": "url", "risk_level": "low", "total_score": "0",
    })
    with pytest.raises(RuntimeError, match="DB write was not confirmed"):
        recovery.verified_write({}, {"initial_risk_level": "low", "initial_risk_score": 0}, "url")


def test_deadlock_retry_requires_fresh_matching_record(monkeypatch):
    import utils.db

    calls = []
    monkeypatch.setattr(utils.db, "insert_findings", lambda *a, **kw: calls.append(kw["namespace"]))
    monkeypatch.setattr(recovery.time, "sleep", lambda *a: None)
    rows = iter([
        {"generation_time": datetime(2026, 10, 23)},
        {"generation_time": datetime.now(), "report_url": "url", "risk_level": "low", "total_score": "2"},
    ])
    monkeypatch.setattr(recovery, "db_row", lambda _: next(rows))
    row = recovery.verified_write({}, {"initial_risk_level": "low", "initial_risk_score": 2}, "url")
    assert row["total_score"] == "2"
    assert calls == ["yujing_audit", "yujing_audit"]
