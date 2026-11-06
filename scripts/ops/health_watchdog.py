#!/usr/bin/env python
"""Health watchdog for paper-api: run every few minutes from a systemd timer.

Looks at three things and mails whoever is on ALERT_TO when one of them is
new: the deep health report (/api/health/deep: on-disk code imports, stale
process, database, disk, failed tasks), the service journal since the last
run (tracebacks, HTTP 500s, codec errors), and whether the API answers at
all. Each distinct problem is mailed once when it appears, reminded after
REMIND_MINUTES if it persists, and reported once more when it clears. A
daily summary goes out at the first run after DIGEST_HOUR.

Configuration comes from the environment (systemd EnvironmentFile):
  SMTP_HOST, SMTP_PORT (465 = SSL, else STARTTLS), SMTP_USER, SMTP_PASSWORD,
  ALERT_FROM, ALERT_TO (comma separated)
  HEALTH_URL (default http://127.0.0.1:8001/api/health/deep)
  JOURNAL_UNIT (default paper-api.service)
  WATCHDOG_STATE (default <root>/data/ops/watchdog_state.json)
  REMIND_MINUTES (default 120), DIGEST_HOUR (default 9)
Options: --dry-run (print mails instead of sending), --test-mail.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(os.environ.get("PAPER_INTEGRITY_ROOT", "/home/sjtu/paper-integrity-checker"))
HEALTH_URL = os.environ.get("HEALTH_URL", "http://127.0.0.1:8001/api/health/deep")
JOURNAL_UNIT = os.environ.get("JOURNAL_UNIT", "paper-api.service")
STATE_PATH = Path(os.environ.get("WATCHDOG_STATE", str(ROOT / "data" / "ops" / "watchdog_state.json")))
LOG_PATH = ROOT / "data" / "output" / "ops_logs" / "watchdog.log"
REMIND_SECONDS = int(os.environ.get("REMIND_MINUTES", "120")) * 60
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
SERVICE_NAME = "论文检测系统 paper-api"

JOURNAL_PATTERNS = (
    ("http_500", re.compile(r'" 500 Internal Server Error')),
    ("traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("error_log", re.compile(r"\[ERROR\] (?P<logger>\S+): (?P<msg>.*)")),
    ("codec", re.compile(r"codec can't (?:encode|decode)")),
)
# Normalise the varying parts of a message so repeats share one fingerprint.
_VOLATILE = re.compile(r"\d{8}-[0-9a-f]{8}|\d{14}_\w+_[0-9a-f]{8}|\b\d+(?:\.\d+)?\b|0x[0-9a-f]+")


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now():%F %T} {message}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"open": {}, "last_run": 0, "last_digest_day": "", "history": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------- sources

def fetch_health(url: str = HEALTH_URL, timeout: int = 240) -> tuple[dict | None, str]:
    """(report, error). 503 still carries a report (critical)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return _report(response.read().decode("utf-8"), response.status)
    except urllib.error.HTTPError as exc:
        return _report(exc.read().decode("utf-8", "replace"), exc.code)
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
        return None, str(exc)[:300]


def _report(body: str, code: int) -> tuple[dict | None, str]:
    """A deep-health body has a status; anything else (404 from an old
    process without the endpoint, an HTML error page) is a failure."""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("status") in ("ok", "degraded", "critical"):
        return data, ""
    return None, f"HTTP {code}，返回的不是深度健康报告：{body[:160]}"


def read_journal(since_epoch: float, unit: str = JOURNAL_UNIT) -> str:
    since = datetime.fromtimestamp(since_epoch).strftime("%Y-%m-%d %H:%M:%S")
    try:
        proc = subprocess.run(["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "short-iso"],
                              capture_output=True, text=True, timeout=60)
        return proc.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"journalctl failed: {exc}")
        return ""


def journal_findings(text: str) -> list[dict]:
    """One finding per distinct error fingerprint, with count and first line."""
    found: dict[str, dict] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for kind, pattern in JOURNAL_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            if kind == "traceback":
                # The exception line is the last non-indented line of the block.
                detail = ""
                for follow in lines[index + 1:index + 60]:
                    body = follow.split("]: ", 1)[-1]
                    if body and not body.startswith((" ", "Traceback")) and not body.startswith("INFO:"):
                        detail = body.strip()
                        break
                message = detail or "Traceback"
            elif kind == "error_log":
                message = f"{match.group('logger')}: {match.group('msg').strip()}"
            else:
                message = line.split("]: ", 1)[-1].strip()
            key = f"journal:{kind}:{_VOLATILE.sub('#', message)[:160]}"
            entry = found.setdefault(key, {"key": key, "kind": kind, "severity": "critical" if kind in ("http_500", "traceback") else "warning",
                                           "message": message[:300], "count": 0, "first_line": line[:300]})
            entry["count"] += 1
            break
    return list(found.values())


def health_findings(report: dict | None, error: str) -> list[dict]:
    if report is None:
        return [{"key": "api:unreachable", "kind": "api", "severity": "critical",
                 "message": f"/api/health/deep 无法访问：{error}", "count": 1}]
    findings = []
    for problem in report.get("problems") or []:
        check = problem.get("check")
        detail = problem.get("message", "")
        if check == "task_failures":
            for failure in (report.get("checks", {}).get("task_failures") or [])[:5]:
                reason = "; ".join(filter(None, [failure.get("error", ""), *failure.get("paper_errors", [])]))[:300]
                findings.append({"key": f"task:{failure['task_id']}", "kind": "task", "severity": "warning",
                                 "message": f"任务 {failure['task_id']}（{failure.get('submission_no') or '无提交号'}）失败：{reason}",
                                 "count": 1})
            continue
        if check == "stale_code":
            files = ", ".join(f["path"] for f in (report.get("checks", {}).get("stale_code") or [])[:5])
            detail = f"{detail}；文件：{files}"
        if check == "imports":
            tail = (report.get("checks", {}).get("imports") or {}).get("traceback_tail") or []
            detail = f"{detail}\n" + "\n".join(tail)
        findings.append({"key": f"health:{check}", "kind": check, "count": 1,
                         "severity": "critical" if problem.get("severity") == "critical" else "warning",
                         "message": detail})
    return findings


# ---------------------------------------------------------------- alerting

def reconcile(state: dict, findings: list[dict], now: float) -> tuple[list[dict], list[dict], list[dict]]:
    """Split findings into new, reminders due, and problems that cleared."""
    open_alerts = state.setdefault("open", {})
    new, remind, cleared = [], [], []
    seen = set()
    for finding in findings:
        key = finding["key"]
        seen.add(key)
        record = open_alerts.get(key)
        if record is None:
            open_alerts[key] = {"first_seen": now, "last_seen": now, "last_mailed": now,
                                "message": finding["message"], "severity": finding["severity"], "count": finding.get("count", 1)}
            new.append(finding)
        else:
            record["last_seen"] = now
            record["count"] = record.get("count", 0) + finding.get("count", 1)
            if now - record.get("last_mailed", now) >= REMIND_SECONDS:
                record["last_mailed"] = now
                remind.append({**finding, "since": record["first_seen"], "count": record["count"]})
    for key in list(open_alerts):
        if key not in seen:
            record = open_alerts.pop(key)
            # Journal findings are per-interval by nature: they clear silently.
            if not key.startswith("journal:"):
                cleared.append({"key": key, "message": record.get("message", ""), "since": record.get("first_seen", now)})
    return new, remind, cleared


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def compose(new: list[dict], remind: list[dict], cleared: list[dict], report: dict | None, now: float) -> tuple[str, str]:
    worst = "critical" if any(f["severity"] == "critical" for f in new + remind) else "warning"
    host = socket.gethostname()
    if new or remind:
        subject = f"[{SERVICE_NAME}] {'严重' if worst == 'critical' else '警告'}：{(new or remind)[0]['message'][:60]}"
    else:
        subject = f"[{SERVICE_NAME}] 已恢复：{cleared[0]['message'][:60]}"
    lines = [f"主机 {host}，检查时间 {datetime.fromtimestamp(now):%F %T}", ""]
    if new:
        lines.append("新发现的问题：")
        for f in new:
            lines.append(f"  [{f['severity']}] {f['message']}" + (f"（{f['count']} 次）" if f.get("count", 1) > 1 else ""))
            if f.get("first_line"):
                lines.append(f"      日志：{f['first_line']}")
        lines.append("")
    if remind:
        lines.append("仍未解决（提醒）：")
        for f in remind:
            lines.append(f"  [{f['severity']}] {f['message']}（自 {_fmt(f['since'])} 起，累计 {f['count']} 次）")
        lines.append("")
    if cleared:
        lines.append("已恢复：")
        for f in cleared:
            lines.append(f"  {f['message']}（自 {_fmt(f['since'])} 起）")
        lines.append("")
    if report:
        lines.append(f"当前服务状态：{report.get('status')}；进程启动于 {report.get('process', {}).get('started')}")
    lines += ["", "处理提示：运行中代码过期 → systemctl restart paper-api.service；导入失败 → 查看 git status 中未完成的改动；",
              "任务失败 → data/tasks/<task_id>/status.json 与 journalctl -u paper-api.service。",
              f"看门狗日志：{LOG_PATH}"]
    return subject, "\n".join(lines)


def compose_digest(state: dict, report: dict | None, now: float) -> tuple[str, str]:
    day_ago = now - 86400
    history = [h for h in state.get("history", []) if h["at"] >= day_ago]
    completed, failed = task_counts(day_ago)
    lines = [f"过去 24 小时：任务完成 {completed}，失败 {failed}，告警 {len(history)} 条，当前未解决 {len(state.get('open', {}))} 条。", ""]
    for h in history[-20:]:
        lines.append(f"  {_fmt(h['at'])} [{h['severity']}] {h['message'][:120]}")
    if report:
        lines += ["", f"当前服务状态：{report.get('status')}；进程启动于 {report.get('process', {}).get('started')}；"
                  f"磁盘剩余 {report.get('checks', {}).get('disk', {}).get('free_gb')} GB"]
    return f"[{SERVICE_NAME}] 每日汇总 {datetime.fromtimestamp(now):%F}", "\n".join(lines)


def task_counts(since_epoch: float) -> tuple[int, int]:
    completed = failed = 0
    for status_file in (ROOT / "data" / "tasks").glob("*/status.json"):
        try:
            if status_file.stat().st_mtime < since_epoch:
                continue
            status = json.loads(status_file.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            continue
        completed += status == "completed"
        failed += status == "failed"
    return completed, failed


def send_mail(subject: str, body: str, dry_run: bool = False) -> bool:
    recipients = [r.strip() for r in os.environ.get("ALERT_TO", "").split(",") if r.strip()]
    if dry_run:
        print(f"--- MAIL to {recipients or '(ALERT_TO unset)'}\nSubject: {subject}\n\n{body}\n---")
        return True
    host, port = os.environ.get("SMTP_HOST", ""), int(os.environ.get("SMTP_PORT", "465"))
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("ALERT_FROM") or user
    if not (host and recipients and sender):
        log(f"mail not sent (SMTP_HOST/ALERT_TO/ALERT_FROM missing): {subject}")
        return False
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, sender, ", ".join(recipients)
    message.set_content(body)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            if user:
                server.login(user, password)
            server.send_message(message)
        log(f"mail sent to {recipients}: {subject}")
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log(f"mail FAILED ({exc}): {subject}")
        return False


# ---------------------------------------------------------------- main

def run(dry_run: bool = False, now: float | None = None) -> int:
    now = now or time.time()
    state = load_state()
    since = state.get("last_run") or (now - 600)
    report, error = fetch_health()
    findings = health_findings(report, error) + journal_findings(read_journal(min(since, now - 60)))
    new, remind, cleared = reconcile(state, findings, now)
    for f in new:
        state.setdefault("history", []).append({"at": now, "severity": f["severity"], "message": f["message"]})
    state["history"] = state.get("history", [])[-500:]
    if new or remind or cleared:
        subject, body = compose(new, remind, cleared, report, now)
        send_mail(subject, body, dry_run)
    today = datetime.fromtimestamp(now).strftime("%F")
    if datetime.fromtimestamp(now).hour >= DIGEST_HOUR and state.get("last_digest_day") != today:
        subject, body = compose_digest(state, report, now)
        if send_mail(subject, body, dry_run) or dry_run:
            state["last_digest_day"] = today
    state["last_run"] = now
    save_state(state)
    status = report.get("status") if report else "unreachable"
    log(f"run: health={status} findings={len(findings)} new={len(new)} remind={len(remind)} cleared={len(cleared)}")
    return 0 if status == "ok" and not findings else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print mails instead of sending them")
    parser.add_argument("--test-mail", action="store_true", help="send one test mail and exit")
    args = parser.parse_args()
    if args.test_mail:
        ok = send_mail(f"[{SERVICE_NAME}] 测试邮件", f"看门狗邮件通道正常。主机 {socket.gethostname()}，{datetime.now():%F %T}。", args.dry_run)
        return 0 if ok else 2
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
