"""/api/health/deep must notice what /api/health cannot: code on disk that no
longer imports, a process older than its sources, and recent task failures."""
import json
import os
import sys
import time
from pathlib import Path

from api.services import deep_health as dh


def test_newer_sources_lists_only_files_changed_after_process_start(tmp_path):
    (tmp_path / "api").mkdir()
    old = tmp_path / "api" / "old.py"; old.write_text("x = 1")
    new = tmp_path / "api" / "new.py"; new.write_text("x = 2")
    cache = tmp_path / "api" / "__pycache__"; cache.mkdir(); (cache / "new.cpython-312.pyc").write_bytes(b"")
    started = time.time()
    os.utime(old, (started - 100, started - 100))
    os.utime(new, (started + 100, started + 100))
    stale = dh.newer_sources(tmp_path, started, dirs=("api",))
    assert [f["path"] for f in stale] == ["api/new.py"]


def test_import_check_reports_the_failing_symbol(tmp_path):
    pkg = tmp_path / "brokenpkg"; pkg.mkdir(); (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text("VALUE = 1\n")
    (pkg / "bad.py").write_text("from brokenpkg.good import time_left\n")
    ok = dh.import_check(tmp_path, sys.executable, targets=("brokenpkg.good",), timeout=60)
    assert ok["ok"] and ok["modules"] == 1
    bad = dh.import_check(tmp_path, sys.executable, targets=("brokenpkg.good", "brokenpkg.bad"), timeout=60)
    assert not bad["ok"]
    assert "time_left" in bad["error"]


def test_recent_task_failures_reads_failed_tasks_in_window(tmp_path):
    def task(task_id, status, age, error="", paper_error=""):
        d = tmp_path / task_id; d.mkdir()
        f = d / "status.json"
        f.write_text(json.dumps({"task_id": task_id, "status": status, "error": error, "updated_at": "2026-11-06T16:32:13",
                                 "config": {"submission_no": "20261106162555_youxiao_61daaa43"},
                                 "papers": [{"error": paper_error}]}), encoding="utf-8")
        os.utime(f, (time.time() - age, time.time() - age))
    task("t-fresh-failed", "failed", 60, "Detection/initial report persistence failed for 1 paper(s)",
         "'ascii' codec can't encode characters in position 65-82")
    task("t-old-failed", "failed", 3600 * 3, "old")
    task("t-fresh-ok", "completed", 60)
    failures = dh.recent_task_failures(tmp_path, window_seconds=900)
    assert [f["task_id"] for f in failures] == ["t-fresh-failed"]
    assert "ascii" in failures[0]["paper_errors"][0]
    assert failures[0]["submission_no"].endswith("youxiao_61daaa43")


def _report(**overrides):
    checks = {"imports": {"ok": True}, "stale": [], "database": {"ok": True},
              "disk": {"ok": True, "free_gb": 500.0, "critical": False}, "failures": []}
    checks.update(overrides)
    return dh.deep_status(Path("/nonexistent"), Path("/nonexistent"), started=time.time(), checks=checks)


def test_deep_status_grades_import_failure_critical_and_stale_code_degraded():
    assert _report()["status"] == "ok"
    stale = _report(stale=[{"path": "api/services/detect.py", "modified": "2026-11-06T16:00:00"}])
    assert stale["status"] == "degraded"
    assert stale["problems"][0]["check"] == "stale_code"
    broken = _report(imports={"ok": False, "error": "ImportError: cannot import name 'time_left'"})
    assert broken["status"] == "critical"
    assert "time_left" in broken["problems"][0]["message"]
    failed = _report(failures=[{"task_id": "t1", "error": "x"}])
    assert failed["status"] == "degraded" and failed["problems"][0]["check"] == "task_failures"
    full = _report(disk={"ok": False, "free_gb": 2.0, "critical": True})
    assert full["status"] == "critical"


def test_health_deep_route_returns_503_only_when_critical(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import health

    app = FastAPI(); app.include_router(health.router, prefix="/api")
    client = TestClient(app)
    monkeypatch.setattr(dh, "deep_status", lambda: {"status": "degraded", "problems": [], "checks": {}})
    assert client.get("/api/health/deep").status_code == 200
    monkeypatch.setattr(dh, "deep_status", lambda: {"status": "critical", "problems": [], "checks": {}})
    response = client.get("/api/health/deep")
    assert response.status_code == 503 and response.json()["status"] == "critical"


def test_watchdog_check_reports_a_silent_watchdog(tmp_path):
    state = tmp_path / "watchdog_state.json"
    assert dh.watchdog_check(state, now=1000.0)["ok"] is False          # never ran
    state.write_text(json.dumps({"last_run": 1000.0 - 20 * 60}))
    fresh = dh.watchdog_check(state, max_age=75 * 60, now=1000.0)
    assert fresh["ok"] and fresh["age_minutes"] == 20
    stale = dh.watchdog_check(state, max_age=75 * 60, now=1000.0 + 3 * 3600)
    assert stale["ok"] is False and stale["age_minutes"] == 200


def test_deep_status_flags_a_silent_watchdog_as_degraded():
    checks = {"imports": {"ok": True}, "stale": [], "database": {"ok": True},
              "disk": {"ok": True, "free_gb": 100.0, "critical": False}, "failures": [],
              "watchdog": {"ok": False, "last_run": "2026-11-09T00:00:00", "age_minutes": 500}}
    status = dh.deep_status(Path("/nonexistent"), Path("/nonexistent"), started=time.time(), checks=checks)
    assert status["status"] == "degraded"
    assert any(p["check"] == "watchdog" and "500 分钟" in p["message"] for p in status["problems"])
    checks["watchdog"] = {"ok": True, "last_run": "2026-11-09T12:00:00", "age_minutes": 5}
    assert dh.deep_status(Path("/nonexistent"), Path("/nonexistent"), started=time.time(), checks=checks)["status"] == "ok"
