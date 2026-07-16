#!/usr/bin/env python3
"""Watchdog for the 20260629 yujing_audit initial-screening batch.

Restarts the batch when the worker process disappears before batch_summary.json
is written. This is meant to recover from OOM-kill or similar host-level exits.
"""
from __future__ import annotations

import os
import argparse
import subprocess
import time
from datetime import datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
WATCHDOG_LOG = BASE / "data/tmp/yujing_audit_20260629_watchdog.log"
CHECK_INTERVAL_SECONDS = 60

CMD: list[str] = []
LOG = BASE / "data/tmp/yujing_audit_initial.log"
PID_FILE = BASE / "data/tmp/yujing_audit.pid"
RUN_DATE = "20260629"
WORKERS = 130


def log(msg: str) -> None:
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    with WATCHDOG_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(errors="ignore").split()
        if len(stat) > 2 and stat[2] == "Z":
            return False
    except OSError:
        return False
    return True


def read_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def find_existing_batch_pid() -> int:
    try:
        out = subprocess.check_output(
            [
                "pgrep",
                "-f",
                f"main.py --batch data/input/yujing_audit/{RUN_DATE} --workers {WORKERS} --table-name yujing_audit",
            ],
            text=True,
        )
    except subprocess.CalledProcessError:
        return 0
    self_pid = os.getpid()
    for line in out.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != self_pid and alive(pid):
            return pid
    return 0


def start_batch(reason: str) -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    log(f"starting batch ({reason})")
    log_f = LOG.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        CMD,
        cwd=str(BASE),
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    log(f"started pid={proc.pid}")
    return proc.pid


def complete() -> bool:
    try:
        with LOG.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 20000))
            tail = f.read().decode("utf-8", errors="ignore")
        return "Batch complete:" in tail
    except OSError:
        return False


def configure() -> None:
    global CMD, LOG, PID_FILE, WATCHDOG_LOG, RUN_DATE, WORKERS
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260629")
    ap.add_argument("--workers", type=int, default=130)
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    RUN_DATE = args.date
    WORKERS = args.workers
    LOG = BASE / f"data/tmp/yujing_audit_{RUN_DATE}_initial_{WORKERS}.log"
    WATCHDOG_LOG = BASE / f"data/tmp/yujing_audit_{RUN_DATE}_watchdog.log"
    PID_FILE = BASE / f"data/tmp/yujing_audit_{RUN_DATE}_{WORKERS}.pid"
    CMD = [
        str(BASE / "venv/bin/python"),
        "-u",
        "main.py",
        "--batch",
        f"data/input/yujing_audit/{RUN_DATE}",
        "--output",
        args.output or "data/output/yujing_audit",
        "--workers",
        str(WORKERS),
        "--table-name",
        "yujing_audit",
        "--force",
        "--resume-existing-output",
    ]


def main() -> int:
    configure()
    pid = read_pid()
    if not alive(pid):
        pid = find_existing_batch_pid()
        if pid:
            PID_FILE.write_text(str(pid), encoding="utf-8")
            log(f"adopted existing pid={pid}")
    log(f"watchdog started pid={pid or 'none'}")

    while True:
        if complete():
            log("batch complete marker found; watchdog exiting")
            return 0
        pid = read_pid()
        if not alive(pid):
            pid = start_batch("worker not alive and summary missing")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
