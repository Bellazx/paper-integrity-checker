"""Isolated A/B run of review engine v3 against papers with known verdicts.

Inputs are copied into a scratch directory so the run can never touch files
used by the live API, and nothing is written to the database. Usage:

    venv/bin/python scripts/review_v3_ab_test.py --candidates c.json --out data/review_v3_ab/run1 \
        [--doi 10.x/y ...] [--limit 10] [--concurrency 4]

The candidates file is a JSON list of {doi, result, input_dir, report, ...}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_v3 import engine  # noqa: E402


def _slug(doi: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in doi)[:80]


def stage(candidate: dict, out: Path) -> dict:
    target = out / _slug(candidate["doi"])
    input_copy = target / "input"
    if not input_copy.exists():
        shutil.copytree(candidate["input_dir"], input_copy, symlinks=False)
    report_copy = target / "report.json"
    if not report_copy.exists():
        shutil.copy2(candidate["report"], report_copy)
    return {**candidate, "input_copy": str(input_copy), "report_copy": str(report_copy), "work": str(target)}


async def review_one(item: dict, gate: asyncio.Semaphore) -> dict:
    async with gate:
        started = time.monotonic()
        try:
            result = await engine.run_review(item["doi"], item["report_copy"], item["input_copy"],
                                             str(Path(item["work"]) / "out"), resume=True)
        except Exception as exc:  # keep the batch going; record the failure
            result = {"trigger": "review_error", "_review_error": f"{type(exc).__name__}: {exc}"}
        wall = time.monotonic() - started
    row = {"doi": item["doi"], "expected": item.get("result"), "got": result.get("result"),
           "error": result.get("_review_error") if result.get("trigger") == "review_error" else None,
           "wall_seconds": round(wall, 1), "dimension_verdicts": result.get("dimension_verdicts"),
           "metrics": result.get("_review_metrics"), "workdir": result.get("_review_workdir")}
    Path(item["work"], "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


async def main_async(args) -> None:
    candidates = json.loads(Path(args.candidates).read_text())
    if args.doi:
        wanted = set(args.doi)
        candidates = [c for c in candidates if c["doi"] in wanted]
    seen, unique = set(), []
    for c in candidates:
        if c["doi"] not in seen:
            seen.add(c["doi"])
            unique.append(c)
    unique = unique[: args.limit] if args.limit else unique
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    staged = [stage(c, out) for c in unique]
    gate = asyncio.Semaphore(args.concurrency)
    rows = await asyncio.gather(*(review_one(item, gate) for item in staged))
    (out / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    done = [r for r in rows if not r["error"]]
    agree = sum(1 for r in done if r["got"] == r["expected"])
    print(f"\nfinished {len(done)}/{len(rows)}; verdict agreement {agree}/{len(done)}; "
          f"median wall {sorted(r['wall_seconds'] for r in rows)[len(rows) // 2]:.0f}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--doi", nargs="*")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    os.nice(10)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_with_signals(main_async(args)))


async def _with_signals(coro):
    """Cancel on SIGTERM/SIGINT so every model session process group is reaped."""
    import signal
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        print("cancelled; model sessions terminated", flush=True)


if __name__ == "__main__":
    main()
