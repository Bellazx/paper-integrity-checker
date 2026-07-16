#!/usr/bin/env python3
"""Create and start yujing_quanliang initial-screening shards."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return safe[:120] or "batch"


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


def stop_existing(table_name: str, date_tag: str) -> None:
    patterns = [
        f"watch_yujing_shard.py --batch data/tmp/{table_name}_shards/{date_tag}/",
        f"main.py --batch data/tmp/{table_name}_shards/{date_tag}/",
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


def paper_dirs(input_root: Path) -> list[Path]:
    result = []
    for d in sorted(input_root.iterdir()):
        if not d.is_dir():
            continue
        has_manifest = (d / "manifest.json").exists()
        has_html = (d / "article.html").exists() or (d / "html" / "article.html").exists()
        if has_manifest and has_html:
            result.append(d)
        elif any(d.glob("*.pdf")) or any(d.glob("*.PDF")):
            result.append(d)
    return result


def has_report(table_name: str, slug: str) -> bool:
    return (BASE / "data/output" / table_name / "init" / slug / "report.json").exists()


def cleanup_partial(table_name: str, slug: str) -> None:
    p = BASE / "data/output" / table_name / "init" / slug
    if p.exists() and not (p / "report.json").exists():
        shutil.rmtree(p)


def make_shards(input_root: Path, table_name: str, date_tag: str, shards: int, clean_partial: bool) -> list[Path]:
    remaining = [d for d in paper_dirs(input_root) if not has_report(table_name, d.name)]
    if clean_partial:
        for d in remaining:
            cleanup_partial(table_name, d.name)

    shard_root = BASE / "data/tmp" / f"{table_name}_shards" / date_tag
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


def start_watchdog(table_name: str, date_tag: str, shard_dir: Path, shard_idx: int, workers: int) -> int:
    tag = f"{date_tag}_shard{shard_idx:02d}_w{workers}"
    cmd = [
        "setsid",
        str(BASE / "venv/bin/python"),
        "-u",
        "watch_yujing_shard.py",
        "--batch",
        str(shard_dir.relative_to(BASE)),
        "--tag",
        tag,
        "--workers",
        str(workers),
        "--table-name",
        table_name,
        "--output",
        f"data/output/{table_name}",
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
    ap.add_argument("--input-root", default="data/input/20260707")
    ap.add_argument("--date-tag", default="20260707")
    ap.add_argument("--table-name", default="yujing_quanliang")
    ap.add_argument("--shards", type=int, default=20)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--stop-existing", action="store_true")
    ap.add_argument("--clean-partial", action="store_true")
    args = ap.parse_args()

    table_name = _safe_name(args.table_name)
    date_tag = _safe_name(args.date_tag)
    input_root = (BASE / args.input_root).resolve()
    if not input_root.exists():
        raise SystemExit(f"input root does not exist: {input_root}")

    if args.stop_existing:
        stop_existing(table_name, date_tag)

    shards = make_shards(input_root, table_name, date_tag, args.shards, args.clean_partial)
    total = 0
    for idx, sdir in enumerate(shards, 1):
        n = sum(1 for p in sdir.iterdir() if p.is_symlink() or p.is_dir())
        total += n
        if n == 0:
            continue
        pid = start_watchdog(table_name, date_tag, sdir, idx, args.workers)
        print(f"{date_tag} shard {idx:02d}: {n} papers, watchdog pid={pid}")
    print(f"started {total} remaining papers for {table_name} with {args.shards} shards x {args.workers} workers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
