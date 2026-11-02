#!/usr/bin/env python3
"""Engine-only regrade of the 0901 testset, compared with the adjudicated outcome.

    REVIEW_V3_REGRADE=1 venv/bin/python scripts/review_v3_regrade_vs_adjudicated.py --out data/review_v3_ab/tc_regrade1 [--concurrency 32]

Each paper's investigated run (as it was before any manual adjudication) is
copied into --out and regraded there by the engine alone: the investigation
stays, calibration and the final review run under the current rules. The
published runs, the table and the database are not touched. The summary
compares each engine verdict with the adjudicated one (the comparison table's
current result).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from review_v3 import config, engine  # noqa: E402

TABLE = BASE / "data" / "output" / "yujing_testset_0901_0907_nature_nc_v3cmp"


def stage(row: dict, out: Path) -> None:
    run = Path(row["_review_workdir"])
    target = out / "review_v3" / run.parent.name / run.name
    if target.exists():
        return
    shutil.copytree(run, target, ignore=shutil.ignore_patterns("before_adjudication", "*.lock"))
    # The engine regrades the run as investigated, not as adjudicated.
    for name in ("finals.json", "final.json"):
        kept = run / "before_adjudication" / name
        if kept.is_file():
            shutil.copy2(kept, target / name)


async def regrade(row: dict, out: Path, gate: asyncio.Semaphore) -> dict:
    async with gate:
        try:
            result = await engine.run_review(row["doi"], row["_report_json"], row["_input_dir"], str(out))
            error = result.get("reason") if result.get("trigger") == "review_error" else None
            got = result.get("result")
        except Exception as exc:  # recorded, the batch goes on
            got, error = None, f"{type(exc).__name__}: {exc}"
    record = {"doi": row["doi"], "expected": row["result"], "got": got, "error": error}
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


async def main_async(args) -> int:
    if not config.REGRADE:
        raise SystemExit("set REVIEW_V3_REGRADE=1")
    out = Path(args.out).resolve()
    rows = [r for r in json.loads((TABLE / "review_results_v3.json").read_text()) if r.get("_review_workdir")]
    if args.doi:
        rows = [r for r in rows if r["doi"] in set(args.doi)]
    for row in rows:
        stage(row, out)
    gate = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(regrade(row, out, gate) for row in rows))
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    agree = sum(r["got"] == r["expected"] for r in results)
    print(f"finished {len(results)}; agreement with adjudicated {agree}/{len(results)}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--doi", nargs="*")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
