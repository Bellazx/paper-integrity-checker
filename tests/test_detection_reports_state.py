from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path

from api.models import PaperInfo, TaskConfig, TaskMode, TaskStatus
from api.services import task_manager as task_manager_module
from api.services import detection_reports_worker as worker


class _TaskCapture:
    def __init__(self):
        self.updates = []

    def update_task(self, task_id, **kwargs):
        self.updates.append(kwargs)


def test_review_result_is_hidden_until_db_write_is_verified(monkeypatch, tmp_path):
    """A rendered PDF alone must not make a failed DB persistence look complete."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.json").write_text(json.dumps({"paper": {"doi": "10.1/example"}}))
    paper = PaperInfo(
        doi_slug="10.1_example",
        doi="10.1/example",
        input_dir=str(tmp_path / "input"),
        output_dir=str(output_dir),
        submission_no="file-1",
        language="zh",
    )
    Path(paper.input_dir).mkdir()
    tm = _TaskCapture()

    async def fake_detection(**kwargs):
        return {
            "pdf_generated": True,
            "paper": {"doi": "10.1/example", "title": "Example"},
            "summary": {},
        }

    async def fake_review(**kwargs):
        return {
            "doi": "10.1/example",
            "result": "高风险",
            "verdict": "建议高风险",
            "trigger": "auto_detection",
            "image_review": "image",
            "data_review": "data",
            "ref_review": "ref",
            "methodology_review": "method",
            "reason": "reason",
        }

    async def fake_reports(review_results, task_dir, **kwargs):
        namespace = kwargs["report_namespace"]
        path = worker.REVIEW_DIR / namespace / "review_10.1_example.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pdf")
        return [str(path)]

    monkeypatch.setattr(worker, "run_detection_single", fake_detection)
    monkeypatch.setattr(worker, "run_review_single", fake_review)
    monkeypatch.setattr(worker, "generate_reports", fake_reports)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(worker, "verify_public_pdf", AsyncMock())
    monkeypatch.setattr(worker, "upsert_chushen", lambda **kwargs: None)
    monkeypatch.setattr(worker, "verify_chushen_write", lambda **kwargs: (True, ""))
    monkeypatch.setattr(worker, "update_review", lambda **kwargs: False)
    monkeypatch.setattr(worker, "update_review_result", lambda **kwargs: True)
    monkeypatch.setattr(worker, "REVIEW_DIR", tmp_path / "review")
    monkeypatch.setattr(worker, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(
        "modules.chinese_report_generator._compute_overall_risk",
        lambda findings: {"level": "高风险", "score": 10},
    )

    asyncio.run(
        worker._run_inner(
            "task-1",
            tm,
            [paper],
            {"author_type": "", "max_workers": 1},
        )
    )

    final = tm.updates[-1]
    record = final["result"]["detection_reports"][0]
    assert final["status"].value == "failed"
    assert record["processing_status"] == "review_failed"
    assert record["review_result"] is None
    assert record["review_report_url"] is None
    assert record["review_error"]


def test_restart_recovery_rejects_partially_extracted_batch(monkeypatch, tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    monkeypatch.setattr(task_manager_module, "TASKS_DIR", tasks_dir)

    manager = task_manager_module.TaskManager()
    task_id = manager.create_task(TaskMode.BATCH, TaskConfig())
    valid_input = tmp_path / "paper-a"
    valid_input.mkdir()
    task = manager._tasks[task_id]
    task.status = TaskStatus.DETECTING
    task.papers = [
        PaperInfo(doi_slug="paper-a", input_dir=str(valid_input)),
        PaperInfo(doi_slug="paper-b", input_dir=""),
    ]
    manager._persist(task)

    recovered_manager = task_manager_module.TaskManager()
    recovered = recovered_manager.load_from_disk()

    assert recovered == []
    loaded = recovered_manager._tasks[task_id]
    assert loaded.status == TaskStatus.FAILED
    assert "inputs unavailable" in (loaded.error or "")


@pytest.mark.parametrize('initial_risk', ['高风险', '低风险'])
def test_user_worker_resumes_detection_and_retries_only_render(monkeypatch, tmp_path, initial_risk):
    from unittest.mock import AsyncMock
    from api.services import report

    tid = 'resume-task'
    out = tmp_path / 'output'
    out.mkdir()
    findings = {'paper': {'doi': '10.1/example'}, 'summary': {}, 'pdf_generated': True}
    report_json = out / 'report.json'
    report_json.write_text(json.dumps(findings))
    before = report_json.stat().st_mtime_ns
    paper = PaperInfo(doi_slug='10.1_example', doi='10.1/example',
                      output_dir=str(out), input_dir=str(tmp_path),
                      status='detected' if initial_risk == '高风险' else 'pending',
                      submission_no='upload', language='zh',
                      submission_decision='fresh_after_terminal')
    pdf = tmp_path / 'review' / 'detection_reports' / tid / 'review_10.1_example.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'%PDF-1.4')
    detector = (AsyncMock(side_effect=AssertionError('must not repeat detection'))
                if initial_risk == '高风险' else AsyncMock(return_value=findings))
    reviewer = AsyncMock(return_value={'doi': paper.doi, 'result': '高风险', 'trigger': 'review'})
    renderer = AsyncMock(side_effect=[TimeoutError('timeout'), [str(pdf)]])
    updates = []
    monkeypatch.setattr(worker, 'TASKS_DIR', tmp_path / 'tasks')
    monkeypatch.setattr(worker, 'REVIEW_DIR', tmp_path / 'review')
    monkeypatch.setattr(worker, 'run_detection_single', detector)
    monkeypatch.setattr(worker, 'run_review_single', reviewer)
    monkeypatch.setattr(worker, 'verify_public_pdf', AsyncMock())
    monkeypatch.setattr(worker, 'upsert_chushen', lambda **kw: None)
    monkeypatch.setattr(worker, 'verify_chushen_write', lambda **kw: (True, ''))
    monkeypatch.setattr(worker, 'update_review', lambda **kw: updates.append(kw) or True)
    monkeypatch.setattr(worker, 'update_review_result', lambda **kw: True)
    monkeypatch.setattr(worker, 'verify_review_write', lambda **kw: (True, ''))
    monkeypatch.setattr(report, '_generate_reports_once', renderer)
    monkeypatch.setattr(report.asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr('modules.chinese_report_generator._compute_overall_risk',
                        lambda _: {'level': initial_risk})
    manager = _TaskCapture()
    asyncio.run(worker._run_inner(tid, manager, [paper], {'max_workers': 1}))
    assert detector.await_count == (0 if initial_risk == '高风险' else 1)
    if initial_risk == '高风险':
        assert reviewer.await_count == 1
        assert reviewer.await_args.kwargs['resume'] is True
        assert renderer.await_count == 2
        assert updates[0]['task_id'] == tid
    else:
        assert reviewer.await_count == 0
        assert renderer.await_count == 0
        assert updates == []
    assert report_json.stat().st_mtime_ns == before
    assert manager.updates[-1]['status'] == TaskStatus.COMPLETED
    record = manager.updates[-1]['result']['detection_reports'][0]
    assert record['chushen_result'] == initial_risk
