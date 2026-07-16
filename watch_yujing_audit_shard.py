#!/usr/bin/env python3
"""Watch one yujing_audit shard and restart it if the worker exits early."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
CHECK_INTERVAL_SECONDS = 60


def _alive(pid: int) -> bool:
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
        return len(stat) <= 2 or stat[2] != "Z"
    except OSError:
        return False


def _tail_has_complete(log_path: Path) -> bool:
    try:
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 20000))
            tail = f.read().decode("utf-8", errors="ignore")
        return "Batch complete:" in tail or "All papers already processed. Nothing to do." in tail
    except OSError:
        return False


def _read_pid(pid_path: Path) -> int:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _cleanup_shard_dir(batch: str, pid_path: Path, log) -> None:
    shard_dir = (BASE / batch).resolve()
    shard_root = (BASE / "data/tmp/yujing_audit_shards").resolve()
    try:
        shard_dir.relative_to(shard_root)
    except ValueError:
        log(f"skip cleanup for non-shard batch path: {shard_dir}")
        return

    try:
        if shard_dir.exists():
            shutil.rmtree(shard_dir)
            log(f"removed temporary shard dir: {shard_dir}")
        pid_path.unlink(missing_ok=True)
        log(f"removed pid file: {pid_path}")
    except Exception as exc:
        log(f"cleanup failed: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--output", default="data/output/yujing_audit")
    args = ap.parse_args()

    tmp = BASE / "data/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    log_path = tmp / f"yujing_audit_{args.tag}.log"
    watchdog_log = tmp / f"yujing_audit_{args.tag}_watchdog.log"
    pid_path = tmp / f"yujing_audit_{args.tag}.pid"

    cmd = [
        str(BASE / "venv/bin/python"),
        "-u",
        "main.py",
        "--batch",
        args.batch,
        "--output",
        args.output,
        "--workers",
        str(args.workers),
        "--table-name",
        "yujing_audit",
        "--force",
        "--resume-existing-output",
        "--summary-tag",
        args.tag,
    ]

    def log(msg: str) -> None:
        with watchdog_log.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

    def start(reason: str) -> int:
        log(f"starting shard ({reason})")
        log_f = log_path.open("a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE),
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid_path.write_text(str(proc.pid), encoding="utf-8")
        log(f"started pid={proc.pid}")
        return proc.pid

    pid = _read_pid(pid_path)
    log(f"watchdog started pid={pid if _alive(pid) else 'none'}")
    while True:
        if _tail_has_complete(log_path):
            log("completion marker found; watchdog exiting")
            _cleanup_shard_dir(args.batch, pid_path, log)
            return 0
        pid = _read_pid(pid_path)
        if not _alive(pid):
            start("worker not alive and shard incomplete")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
