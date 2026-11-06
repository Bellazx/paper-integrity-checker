"""One API paper shares a single wall clock across detection, review and report."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from api.models import PaperInfo, TaskStatus
from api.services import detection_reports_worker as worker
from api.services import report
from api.services.review_runtime import deadline_after, time_left


class _TaskCapture:
    def __init__(self):
        self.updates = []

    def update_task(self, task_id, **kwargs):
        self.updates.append(kwargs)


def test_time_left_shortens_a_stage_to_the_paper_deadline():
    assert deadline_after(0) is None
    assert time_left(None, 900) == 900
    now = time.monotonic()
    assert 290 < time_left(now + 300, 900) <= 300
    assert time_left(now + 3000, 900) == 900
    # Reserve is taken off the time left; an expired stage may not start.
    assert 190 < time_left(now + 300, 900, reserve=100) <= 200
    with pytest.raises(TimeoutError):
        time_left(now - 10, 900)
    assert 0 < time_left(now + 10, 900, floor=60) <= 10


def test_v3_engine_deadline_only_shortens_the_paper_limit(monkeypatch, tmp_path):
    from review_v3 import config, engine
    seen = {}

    async def fake_review(doi, report_json, input_dir, output_dir, run_dir, deadline):
        seen["deadline"] = deadline
        return {"result": "低风险"}
    monkeypatch.setattr(engine, "_review", fake_review)
    monkeypatch.setattr(config, "PAPER_TIMEOUT_SECONDS", 90 * 60)
    (tmp_path / "in").mkdir()
    asyncio.run(engine.run_review("10.1/x", "", str(tmp_path / "in"), str(tmp_path / "out"),
                                  deadline=time.monotonic() + 600))
    assert seen["deadline"] - time.monotonic() <= 600
    asyncio.run(engine.run_review("10.1/x", "", str(tmp_path / "in"), str(tmp_path / "out2"),
                                  deadline=time.monotonic() + 10 * 3600))
    assert 89 * 60 < seen["deadline"] - time.monotonic() <= 90 * 60


def test_report_retry_stops_at_the_paper_deadline(monkeypatch, tmp_path):
    render = AsyncMock(side_effect=[TimeoutError("timeout"), ["report.pdf"]])
    monkeypatch.setattr(report, "_generate_reports_once", render)
    monkeypatch.setattr(report.asyncio, "sleep", AsyncMock())
    with pytest.raises(TimeoutError):
        asyncio.run(report.generate_reports([], tmp_path, deadline=time.monotonic() - 1))
    assert render.await_count == 0


def test_user_worker_gives_each_stage_what_is_left_of_75_minutes(monkeypatch, tmp_path):
    tid = "deadline-task"
    out = tmp_path / "output"
    out.mkdir()
    findings = {"paper": {"doi": "10.1/example"}, "summary": {}, "pdf_generated": True}
    (out / "report.json").write_text(json.dumps(findings))
    paper = PaperInfo(doi_slug="10.1_example", doi="10.1/example", output_dir=str(out),
                      input_dir=str(tmp_path), status="pending", submission_no="upload",
                      language="zh", submission_decision="fresh_after_terminal")
    pdf = tmp_path / "review" / "detection_reports" / tid / "review_10.1_example.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    detector = AsyncMock(return_value=findings)
    reviewer = AsyncMock(return_value={"doi": paper.doi, "result": "高风险", "trigger": "review"})
    renderer = AsyncMock(return_value=[str(pdf)])
    monkeypatch.setattr(worker, "API_PAPER_DEADLINE_SECONDS", 75 * 60)
    monkeypatch.setattr(worker, "API_REPORT_RESERVE_SECONDS", 5 * 60)
    monkeypatch.setattr(worker, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(worker, "REVIEW_DIR", tmp_path / "review")
    monkeypatch.setattr(worker, "run_detection_single", detector)
    monkeypatch.setattr(worker, "run_review_single", reviewer)
    monkeypatch.setattr(worker, "generate_reports", renderer)
    monkeypatch.setattr(worker, "verify_public_pdf", AsyncMock())
    monkeypatch.setattr(worker, "upsert_chushen", lambda **kw: None)
    monkeypatch.setattr(worker, "verify_chushen_write", lambda **kw: (True, ""))
    monkeypatch.setattr(worker, "update_review", lambda **kw: True)
    monkeypatch.setattr(worker, "update_review_result", lambda **kw: True)
    monkeypatch.setattr(worker, "verify_review_write", lambda **kw: (True, ""))
    monkeypatch.setattr("modules.chinese_report_generator._compute_overall_risk",
                        lambda _: {"level": "高风险"})
    started = time.monotonic()
    asyncio.run(worker._run_inner(tid, _TaskCapture(), [paper], {"max_workers": 1}))
    # Detection keeps its own 30-minute limit inside the 75-minute clock.
    assert detector.await_args.kwargs["timeout"] == 30 * 60
    # Review ends the report reserve before the paper deadline.
    review_deadline = reviewer.await_args.kwargs["deadline"]
    assert 69 * 60 < review_deadline - started <= 70 * 60 + 1
    # The report renders against the paper deadline itself.
    report_deadline = renderer.await_args.kwargs["deadline"]
    assert report_deadline - review_deadline == pytest.approx(5 * 60)
