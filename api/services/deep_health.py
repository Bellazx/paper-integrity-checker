"""Deep health: does the service, as it runs right now, still work end to end?

``/api/health`` answers from memory and stays green while submissions fail.
This module checks the things that actually broke in production:

* the code on disk imports (a half-edited module or a renamed symbol fails
  the first lazy import inside a request, which the client sees as a 500
  with a non-JSON body);
* the running process is older than the newest source file (its modules
  are stale relative to the ones it will lazily import later);
* the database answers, the data disk has room, and recent tasks failed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from api.config import BASE_DIR, DB_CONFIG, PYTHON, TASKS_DIR

SOURCE_DIRS = ("api", "core", "modules", "utils", "review_v3", "main.py")
# Modules the request path imports lazily; a failure here is what the user
# sees as "检测服务调用失败: Expecting value".
IMPORT_TARGETS = (
    "api.main", "api.worker", "api.services.detect", "api.services.detection_reports_worker",
    "api.services.review", "api.services.report", "core.pipeline",
    "modules.chinese_report_generator", "modules.english_report_generator", "review_v3.engine",
)
DISK_WARN_GB = 20
DISK_CRITICAL_GB = 5
FAILURE_WINDOW_SECONDS = 15 * 60
SKIP_PARTS = {"__pycache__", ".git", "node_modules", "venv"}


def process_started(pid: int | None = None) -> float:
    """Epoch seconds the process started (Linux /proc); falls back to the
    interpreter's import time of this module."""
    try:
        import psutil
        return psutil.Process(pid or os.getpid()).create_time()
    except Exception:
        return _MODULE_LOADED


_MODULE_LOADED = time.time()


def newer_sources(root: Path, since: float, dirs=SOURCE_DIRS, limit: int = 20) -> list[dict]:
    """Source files modified after ``since``: evidence that the running
    process no longer matches the code it will import."""
    found = []
    for name in dirs:
        base = root / name
        if base.is_file():
            paths = [base]
        elif base.is_dir():
            paths = (p for p in base.rglob("*.py") if not SKIP_PARTS & set(p.parts))
        else:
            continue
        for path in paths:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > since:
                found.append({"path": str(path.relative_to(root)), "modified": _iso(mtime)})
    found.sort(key=lambda f: f["modified"], reverse=True)
    return found[:limit]


def import_check(root: Path = BASE_DIR, python: str = PYTHON, targets=IMPORT_TARGETS,
                 timeout: int = 180) -> dict:
    """Import the request-path modules in a fresh interpreter from disk."""
    code = ("import importlib, sys\n"
            "for name in sys.argv[1:]:\n"
            "    importlib.import_module(name)\n"
            "print('ok')\n")
    env = {**os.environ, "PYTHONPATH": str(root), "PAPER_HEALTH_IMPORT_CHECK": "1"}
    started = time.monotonic()
    try:
        proc = subprocess.run([python, "-c", code, *targets], cwd=str(root), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"import check exceeded {timeout}s", "seconds": timeout}
    seconds = round(time.monotonic() - started, 1)
    if proc.returncode == 0 and proc.stdout.strip().endswith("ok"):
        return {"ok": True, "seconds": seconds, "modules": len(targets)}
    tail = [line for line in proc.stderr.strip().splitlines() if line.strip()][-6:]
    return {"ok": False, "seconds": seconds, "error": tail[-1] if tail else f"exit {proc.returncode}",
            "traceback_tail": tail}


def recent_task_failures(tasks_dir: Path = TASKS_DIR, window_seconds: int = FAILURE_WINDOW_SECONDS,
                         now: float | None = None) -> list[dict]:
    now = now or time.time()
    failures = []
    for status_file in tasks_dir.glob("*/status.json"):
        try:
            if now - status_file.stat().st_mtime > window_seconds:
                continue
            task = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if task.get("status") != "failed":
            continue
        papers = task.get("papers") or []
        failures.append({
            "task_id": task.get("task_id") or status_file.parent.name,
            "updated_at": task.get("updated_at"),
            "error": str(task.get("error") or "")[:200],
            "paper_errors": [str(p.get("error") or "")[:200] for p in papers if p.get("error")][:3],
            "submission_no": (task.get("config") or {}).get("submission_no") or "",
        })
    failures.sort(key=lambda f: f.get("updated_at") or "", reverse=True)
    return failures


def database_check() -> dict:
    try:
        import pymssql
        conn = pymssql.connect(**{"login_timeout": 10, **DB_CONFIG})
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            conn.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def disk_check(path: Path = BASE_DIR) -> dict:
    usage = shutil.disk_usage(path)
    free_gb = round(usage.free / 1024 ** 3, 1)
    return {"ok": free_gb >= DISK_WARN_GB, "free_gb": free_gb, "total_gb": round(usage.total / 1024 ** 3, 1),
            "critical": free_gb < DISK_CRITICAL_GB}


# The health watchdog (systemd timer, every 30 min) is what turns these checks
# into mail; when it silently stops (the unit failed overnight on 2026-11-09,
# 00:25-08:38, after an install with EnvironmentFile= that PID 1 could not
# read under SELinux) nobody is told. Its last completed run is therefore a
# check of its own, visible on this endpoint and in the nightly digest.
WATCHDOG_STATE = Path(os.environ.get("WATCHDOG_STATE", str(BASE_DIR / "data" / "ops" / "watchdog_state.json")))
WATCHDOG_MAX_AGE_SECONDS = int(os.environ.get("WATCHDOG_MAX_AGE_SECONDS", str(75 * 60)))


def watchdog_check(state_path: Path = WATCHDOG_STATE, max_age: int = WATCHDOG_MAX_AGE_SECONDS,
                   now: float | None = None) -> dict:
    """Age of the watchdog's last completed run, from its state file."""
    now = now or time.time()
    try:
        last_run = float(json.loads(state_path.read_text(encoding="utf-8")).get("last_run") or 0)
    except (OSError, ValueError, TypeError):
        last_run = 0.0
    if not last_run:
        return {"ok": False, "last_run": None, "age_minutes": None, "error": "没有运行记录"}
    age = now - last_run
    return {"ok": age <= max_age, "last_run": _iso(last_run), "age_minutes": round(age / 60)}


def deep_status(root: Path = BASE_DIR, tasks_dir: Path = TASKS_DIR, started: float | None = None,
                checks: dict | None = None) -> dict:
    """Run every check and grade them: ``critical`` when requests cannot be
    served (imports fail, database down, disk nearly full), ``degraded``
    when they can but something needs attention, otherwise ``ok``."""
    started = started or process_started()
    checks = checks or {}
    imports = checks.get("imports") or import_check(root)
    stale = checks.get("stale") if "stale" in checks else newer_sources(root, started)
    database = checks.get("database") or database_check()
    disk = checks.get("disk") or disk_check(root)
    failures = checks.get("failures") if "failures" in checks else recent_task_failures(tasks_dir)
    watchdog = checks.get("watchdog") or watchdog_check()

    problems = []
    if not imports["ok"]:
        problems.append(("critical", "imports", f"磁盘上的代码无法导入：{imports.get('error')}"))
    if not database["ok"]:
        problems.append(("critical", "database", f"数据库不可用：{database.get('error')}"))
    if disk.get("critical"):
        problems.append(("critical", "disk", f"数据盘仅剩 {disk['free_gb']} GB"))
    elif not disk["ok"]:
        problems.append(("degraded", "disk", f"数据盘剩余 {disk['free_gb']} GB（低于 {DISK_WARN_GB} GB）"))
    if stale:
        problems.append(("degraded", "stale_code",
                         f"服务进程启动于 {_iso(started)}，之后有 {len(stale)} 个源文件被修改（运行中的是旧代码，需重启）"))
    if failures:
        problems.append(("degraded", "task_failures", f"最近 {FAILURE_WINDOW_SECONDS // 60} 分钟内 {len(failures)} 个任务失败"))
    if not watchdog["ok"]:
        if watchdog.get("last_run") is None:
            detail = "从未运行"
        else:
            detail = f"已 {watchdog['age_minutes']} 分钟未运行（上次 {watchdog['last_run']}）"
        problems.append(("degraded", "watchdog", f"健康看门狗{detail}；检查 paper-health-watchdog.timer"))

    level = "ok"
    if any(p[0] == "critical" for p in problems):
        level = "critical"
    elif problems:
        level = "degraded"
    return {
        "status": level,
        "checked_at": _iso(time.time()),
        "process": {"pid": os.getpid(), "started": _iso(started), "python": sys.version.split()[0]},
        "problems": [{"severity": s, "check": c, "message": m} for s, c, m in problems],
        "checks": {"imports": imports, "stale_code": stale, "database": database, "disk": disk,
                   "task_failures": failures, "watchdog": watchdog},
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")
