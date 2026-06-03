#!/usr/bin/env python3
"""Crash-resilient runner for screen_4050.py.

The single long-running screen_4050.py process leaks memory (ThreadPoolExecutor shares
one heap; SIFT/image analysis accumulates ~20MB/paper) and was OOM-killed at ~1350 papers
(31GB RSS). This wrapper runs screen_4050.py in fresh-process CHUNKS: each chunk exits and
releases all memory, and --skip-done means every chunk only picks up papers not yet in
yujing_4050. So the whole batch completes within a bounded memory ceiling, and any single
chunk crash is recovered by the next chunk.

Usage
  python run_4050_chunked.py                       # finish all remaining (PDF-preferred)
  python run_4050_chunked.py --chunk 150 --workers 4
  python run_4050_chunked.py --reanalyze-html-done # also re-do HTML-done papers that now have a PDF
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import pymssql

BASE = Path(__file__).resolve().parent
TABLE = "yujing_4050"
DB_CONFIG = {"server": "10.119.5.44", "user": "yujing",
             "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8"}


def _done_count() -> int:
    conn = pymssql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
    n = cur.fetchone()[0]
    conn.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=150, help="papers per fresh process (memory ceiling)")
    ap.add_argument("--workers", type=int, default=4, help="workers per chunk (lower = less peak RAM)")
    ap.add_argument("--max-chunks", type=int, default=100, help="safety cap on chunk count")
    ap.add_argument("--include-dirs-file", default="", help="Pass an allowlist of input directory names to screen_4050.py")
    ap.add_argument("--exclude-dirs-file", default="", help="Pass a blocklist of input directory names to screen_4050.py")
    args = ap.parse_args()

    print(
        "[runner] starting chunked run: "
        f"chunk={args.chunk} workers={args.workers} "
        f"include={args.include_dirs_file or '-'} exclude={args.exclude_dirs_file or '-'}",
        flush=True,
    )
    last_done = -1
    for i in range(args.max_chunks):
        done = _done_count()
        print(f"[runner] chunk {i+1}: yujing_4050 has {done} rows", flush=True)
        # Stop if a full chunk made no progress (nothing left, or repeated failure).
        if done == last_done:
            print("[runner] no progress since last chunk — done or stuck. Stopping.", flush=True)
            break
        last_done = done
        cmd = [sys.executable, str(BASE / "screen_4050.py"),
               "--prefer-pdf", "--skip-done",
               "--limit", str(args.chunk), "--workers", str(args.workers)]
        if args.include_dirs_file:
            cmd.extend(["--include-dirs-file", args.include_dirs_file])
        if args.exclude_dirs_file:
            cmd.extend(["--exclude-dirs-file", args.exclude_dirs_file])
        t0 = time.time()
        rc = subprocess.run(cmd, cwd=str(BASE)).returncode
        print(f"[runner] chunk {i+1} rc={rc} in {time.time()-t0:.0f}s", flush=True)
        # rc != 0 (e.g. OOM) is tolerated: the next chunk resumes via --skip-done.
    print(f"[runner] finished. yujing_4050 rows: {_done_count()}", flush=True)


if __name__ == "__main__":
    main()
