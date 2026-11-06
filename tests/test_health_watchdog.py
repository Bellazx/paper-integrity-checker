"""The watchdog turns the deep-health report and the service journal into
alerts, mails each distinct problem once, reminds, and reports recovery."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "health_watchdog", Path(__file__).resolve().parents[1] / "scripts" / "ops" / "health_watchdog.py")
wd = importlib.util.module_from_spec(SPEC)
sys.modules["health_watchdog"] = wd
SPEC.loader.exec_module(wd)

JOURNAL = """2026-11-06T13:48:09+0800 host bash[2878513]: Traceback (most recent call last):
2026-11-06T13:48:09+0800 host bash[2878513]:   File "/x/uvicorn/h11_impl.py", line 415, in run_asgi
2026-11-06T13:48:09+0800 host bash[2878513]:     result = await app(
2026-11-06T13:48:09+0800 host bash[2878513]:   File "/x/api/services/detection_reports_worker.py", line 12, in <module>
2026-11-06T13:48:09+0800 host bash[2878513]:     from api.config import API_PAPER_DEADLINE_SECONDS
2026-11-06T13:48:09+0800 host bash[2878513]: ImportError: cannot import name 'API_PAPER_DEADLINE_SECONDS' from 'api.config'
2026-11-06T13:48:09+0800 host bash[2878513]: INFO:     10.119.1.50:0 - "POST /api/detection/submit HTTP/1.1" 500 Internal Server Error
2026-11-06T13:48:21+0800 host bash[2878513]: INFO:     10.119.1.50:0 - "POST /api/detection/submit HTTP/1.1" 500 Internal Server Error
2026-11-06T16:32:13+0800 host bash[302897]: 16:32:13 [ERROR] api.services.detection_reports_worker: Detection failed for task_20261106-0074874d: 'ascii' codec can't encode characters in position 65-82: ordinal not in range(128)
2026-11-06T16:32:21+0800 host bash[302897]: INFO:     127.0.0.1:42312 - "POST /api/upload HTTP/1.1" 200 OK
"""


def test_journal_findings_fingerprint_500s_tracebacks_and_error_logs():
    findings = {f["kind"]: f for f in wd.journal_findings(JOURNAL)}
    assert findings["http_500"]["count"] == 2
    assert "ImportError: cannot import name 'API_PAPER_DEADLINE_SECONDS'" in findings["traceback"]["message"]
    assert findings["traceback"]["severity"] == "critical"
    assert "ascii" in findings["error_log"]["message"]
    assert "0074874d" not in findings["error_log"]["key"]  # task ids do not make new fingerprints
    assert "codec" not in findings  # the [ERROR] pattern already took that line


def test_health_findings_cover_unreachable_api_and_each_problem():
    assert wd.health_findings(None, "connection refused")[0]["key"] == "api:unreachable"
    report = {"status": "degraded", "problems": [
        {"severity": "degraded", "check": "stale_code", "message": "进程旧于源代码"},
        {"severity": "degraded", "check": "task_failures", "message": "1 个任务失败"}],
        "checks": {"stale_code": [{"path": "api/config.py"}],
                   "task_failures": [{"task_id": "20261106-0074874d", "submission_no": "s1", "error": "persist", "paper_errors": ["ascii"]}]}}
    findings = wd.health_findings(report, "")
    assert [f["key"] for f in findings] == ["health:stale_code", "task:20261106-0074874d"]
    assert "api/config.py" in findings[0]["message"] and "ascii" in findings[1]["message"]


def test_report_rejects_404_and_html_bodies():
    assert wd._report('{"detail":"Not Found"}', 404)[0] is None
    assert wd._report("<html>502</html>", 502)[0] is None
    assert wd._report('{"status":"critical","problems":[]}', 503)[0]["status"] == "critical"


def test_reconcile_mails_once_reminds_later_and_reports_recovery(monkeypatch):
    monkeypatch.setattr(wd, "REMIND_SECONDS", 3600)
    state = {"open": {}}
    stale = {"key": "health:stale_code", "severity": "warning", "message": "旧代码", "count": 1}
    new, remind, cleared = wd.reconcile(state, [stale], now=1000)
    assert [f["key"] for f in new] == ["health:stale_code"] and not remind and not cleared
    new, remind, cleared = wd.reconcile(state, [stale], now=1300)
    assert not new and not remind and not cleared
    new, remind, cleared = wd.reconcile(state, [stale], now=1000 + 3600)
    assert [f["key"] for f in remind] == ["health:stale_code"] and remind[0]["count"] == 3
    new, remind, cleared = wd.reconcile(state, [], now=1000 + 7200)
    assert [f["key"] for f in cleared] == ["health:stale_code"] and state["open"] == {}
    # journal findings are per interval: they drop out silently
    wd.reconcile(state, [{"key": "journal:http_500:x", "severity": "critical", "message": "500", "count": 2}], now=1)
    assert wd.reconcile(state, [], now=2)[2] == []


def test_compose_and_send_mail_use_smtp_settings(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            sent["host"], sent["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def login(self, user, password): sent["login"] = (user, password)
        def send_message(self, message): sent["message"] = message

    monkeypatch.setattr(wd.smtplib, "SMTP_SSL", FakeSMTP)
    for key, value in {"SMTP_HOST": "smtp.example.edu", "SMTP_PORT": "465", "SMTP_USER": "ops@example.edu",
                       "SMTP_PASSWORD": "pw", "ALERT_FROM": "ops@example.edu", "ALERT_TO": "a@x.edu, b@x.edu"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(wd, "LOG_PATH", Path("/tmp/watchdog_test.log"))
    new = [{"key": "journal:traceback:x", "severity": "critical", "count": 2,
            "message": "ImportError: cannot import name 'time_left'", "first_line": "2026-11-06T13:48:09 Traceback"}]
    subject, body = wd.compose(new, [], [], {"status": "critical", "process": {"started": "2026-11-06T16:28:55"}}, now=1_800_000_000)
    assert subject.startswith("[论文检测系统 paper-api] 严重：ImportError")
    assert "systemctl restart paper-api.service" in body and "（2 次）" in body
    assert wd.send_mail(subject, body) is True
    assert sent["host"] == "smtp.example.edu" and sent["login"] == ("ops@example.edu", "pw")
    assert sent["message"]["To"] == "a@x.edu, b@x.edu" and sent["message"]["Subject"] == subject
