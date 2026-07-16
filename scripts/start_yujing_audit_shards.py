#!/usr/bin/env python3
"""Create and start yujing_audit initial-screening shards."""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
OUT_DIRS = [
    BASE / "data/output/yujing_audit/init",
    BASE / "data/output/yujing_audit_20260630/init",
]


def alive(pid: int) -> bool:
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


def stop_existing() -> None:
    patterns = [
        "watch_yujing_audit_20260629.py --date 20260629 --workers 64",
        "watch_yujing_audit_20260629.py --date 20260630 --workers 64",
        "watch_yujing_audit_shard.py --batch data/tmp/yujing_audit_shards/",
        "main.py --batch data/input/yujing_audit/20260629 --output",
        "main.py --batch data/input/yujing_audit/20260630 --output",
        "main.py --batch data/tmp/yujing_audit_shards/",
    ]
    pids: set[int] = set()
    self_pid = os.getpid()
    for pat in patterns:
        try:
            out = subprocess.check_output(["pgrep", "-f", pat], text=True)
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid != self_pid:
                pids.add(pid)
    if not pids:
        return
    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 60
    while time.time() < deadline and any(alive(pid) for pid in pids):
        time.sleep(2)
    for pid in sorted(pids):
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def paper_dirs(date: str) -> list[Path]:
    root = BASE / f"data/input/yujing_audit/{date}"
    result = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if (d / "manifest.json").exists() and ((d / "article.html").exists() or (d / "html/article.html").exists()):
            result.append(d)
        elif any(d.glob("*.pdf")) or any(d.glob("*.PDF")):
            result.append(d)
    return result


def has_report(slug: str) -> bool:
    return any((out / slug / "report.json").exists() for out in OUT_DIRS)


def cleanup_partial(slug: str) -> None:
    for out in OUT_DIRS:
        p = out / slug
        if p.exists() and not (p / "report.json").exists():
            shutil.rmtree(p)


def make_shards(date: str, shards: int, clean_partial: bool) -> list[Path]:
    input_root = BASE / f"data/input/yujing_audit/{date}"
    remaining = [d for d in paper_dirs(date) if not has_report(d.name)]
    if clean_partial:
        for d in remaining:
            cleanup_partial(d.name)

    shard_root = BASE / f"data/tmp/yujing_audit_shards/{date}"
    if shard_root.exists():
        shutil.rmtree(shard_root)
    shard_root.mkdir(parents=True, exist_ok=True)
    for xlsx in sorted(input_root.glob("*.xlsx")):
        (shard_root / xlsx.name).symlink_to(xlsx)

    shard_dirs = []
    for idx in range(shards):
        sdir = shard_root / f"shard_{idx + 1:02d}"
        sdir.mkdir(parents=True, exist_ok=True)
        shard_dirs.append(sdir)
    for idx, src in enumerate(remaining):
        dst = shard_dirs[idx % shards] / src.name
        dst.symlink_to(src, target_is_directory=True)
    return shard_dirs


def start_watchdog(date: str, shard_dir: Path, shard_idx: int, workers: int) -> int:
    tag = f"{date}_shard{shard_idx:02d}_w{workers}"
    cmd = [
        "setsid",
        str(BASE / "venv/bin/python"),
        "-u",
        "watch_yujing_audit_shard.py",
        "--batch",
        str(shard_dir.relative_to(BASE)),
        "--tag",
        tag,
        "--workers",
        str(workers),
        "--output",
        "data/output/yujing_audit",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=False,
    )
    return proc.pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stop-existing", action="store_true")
    ap.add_argument("--clean-partial", action="store_true")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--date", action="append",
                    help="Batch date to shard, e.g. 20260701. Can be repeated.")
    ap.add_argument("--shards", type=int,
                    help="Number of shards for every --date. Defaults to the built-in 20260629/20260630 plan.")
    args = ap.parse_args()

    if args.stop_existing:
        stop_existing()

    if args.date:
        if not args.shards:
            raise SystemExit("--shards is required when --date is provided")
        plan = [(date, args.shards) for date in args.date]
    else:
        plan = [("20260629", 4), ("20260630", 6)]

    for date, nshards in plan:
        shards = make_shards(date, nshards, clean_partial=args.clean_partial)
        for idx, sdir in enumerate(shards, 1):
            n = sum(1 for p in sdir.iterdir() if p.is_symlink() or p.is_dir())
            if n == 0:
                continue
            pid = start_watchdog(date, sdir, idx, args.workers)
            print(f"{date} shard {idx:02d}: {n} papers, watchdog pid={pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
