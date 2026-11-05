#!/usr/bin/env python3
"""Calibration and final judge only, on investigated testset papers.

    venv/bin/python scripts/review_v3_calibration_ab.py --out data/review_v3_ab/cal_x [--sample 30] [--doi ...]

For an A/B of a calibration change, run it once per code version into
different --out directories and compare the summaries. Each run copies the
papers' investigated runs and, starting from the findings as the
investigation and arbitration ledgers recorded them, runs calibration and the
final judge under the time pressure of a real review (calibration gets its
floor, not the whole time box). Screenshots and report writing are skipped.
Nothing outside --out is written; no table or database.

2026-11-05: one calibrator for all issues agreed with the adjudicated outcome
on 22/30 papers; issues shared out round-robin among 4-6 calibrators on 19/30,
were not faster (median 644 s against 582 s) and cost 3.7 times as much.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from review_v3 import config, engine  # noqa: E402

TABLE = BASE / "data" / "output" / "yujing_testset_0901_0907_nature_nc_v3cmp"
BASELINE = BASE / "data" / "review_v3_ab" / "tc_regrade1" / "summary.json"


def stage(row: dict, out: Path) -> Path:
    """Copy the run as investigated, without any grading."""
    run = Path(row["_review_workdir"])
    target = out / "runs" / run.parent.name / run.name
    if not target.exists():
        shutil.copytree(run, target, ignore=shutil.ignore_patterns("before_adjudication", "*.lock"))
        for folder, patterns in (("ledgers", ("cal_*", "jdg_*", "evd_*")), ("sessions", ("cal_*", "jdg_*", "evd_*"))):
            for pattern in patterns:
                for path in (target / folder).glob(pattern):
                    path.unlink()
    return target


def investigated_findings(review: engine.Review) -> dict:
    """Findings as the investigation and arbitration left them (no grading)."""
    finals, disputes = review.merge()
    for path in sorted((review.run_dir / "ledgers").glob("arb*_*.json")) + sorted((review.run_dir / "ledgers").glob("fin*_*.json")):
        if path.name.count(".") > 1:
            continue
        for item_id, record in (engine.ledger.load(path).get("items") or {}).items():
            if record.get("status") in ("issue", "cleared", "not_applicable"):
                finals[item_id] = {**finals.get(item_id, {}), **{k: record.get(k) for k in ("id", "title", "members")
                                                                  if record.get(k) is not None},
                                   "dimension": record.get("dimension") or finals.get(item_id, {}).get("dimension"),
                                   **engine._compact(record)}
    for item in disputes:
        finals.setdefault(item["id"], {**{k: item.get(k) for k in ("id", "title", "kind", "members", "shard")},
                                       "dimension": item["dimension"], "status": "unresolved"})
    return finals


async def grade(row: dict, out: Path, gate: asyncio.Semaphore) -> dict:
    async with gate:
        run = stage(row, out)
        state = json.loads((run / "state.json").read_text())
        record = {"doi": row["doi"], "expected": row["result"]}
        # Calibration starts with only its floor left, as in a real review.
        review = engine.Review(row["doi"], row["_report_json"], row["_input_dir"], run, None,
                               started=time.monotonic() - config.CALIBRATE_SOFT_SECONDS)
        try:
            await review.prepare(state["evidence_path"])
            finals = investigated_findings(review)
            issues = [r for r in finals.values() if r.get("status") == "issue"]
            record.update(issues=len(issues), substantive=sum(r.get("severity") == "substantive" for r in issues),
                          investigated=engine.Review.verdicts(finals)["result"])
            started = time.monotonic()
            finals = await review.calibrate(finals)
            record["calibrate_s"] = round(time.monotonic() - started, 1)
            record["calibrated"] = engine.Review.verdicts(finals)["result"]
            started = time.monotonic()
            finals = await review.judge(finals)
            record["judge_s"] = round(time.monotonic() - started, 1)
            record["got"] = engine.Review.verdicts(finals)["result"]
            record["severities"] = {k: (r.get("status"), r.get("severity"), r.get("category"))
                                    for k, r in finals.items() if r.get("status") in ("issue", "cleared")}
        except Exception as exc:  # recorded, the batch goes on
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            await review.close()
    print(json.dumps({k: v for k, v in record.items() if k != "severities"}, ensure_ascii=False), flush=True)
    return record


def pick(rows: list[dict], sample: int) -> list[dict]:
    """Papers where sharing out can matter: several confirmed issues. Half
    are the earlier engine regrade's disagreements, the rest the papers with
    the most issues."""
    baseline = {r["doi"]: r for r in json.loads(BASELINE.read_text())} if BASELINE.exists() else {}

    def issues(row):
        finals = Path(row["_review_workdir"]) / "before_adjudication" / "finals.json"
        if not finals.is_file():
            finals = Path(row["_review_workdir"]) / "finals.json"
        try:
            records = json.loads(finals.read_text())["finals"].values()
        except (OSError, ValueError, KeyError):
            return 0
        return sum(1 for r in records if r.get("status") == "issue" or r.get("calibrated_from"))

    scored = [(row, issues(row)) for row in rows]
    scored = [(row, n) for row, n in scored if n >= 3]
    disagree = [(row, n) for row, n in scored if baseline.get(row["doi"], {}).get("got") not in (None, row["result"])]
    disagree = sorted(disagree, key=lambda x: -x[1])[:sample // 2]
    rest = sorted((x for x in scored if x not in disagree), key=lambda x: -x[1])
    return [row for row, _n in (disagree + rest)[:sample]]


async def main_async(args) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = [r for r in json.loads((TABLE / "review_results_v3.json").read_text()) if r.get("_review_workdir")]
    if args.doi:
        rows = [r for r in rows if r["doi"] in set(args.doi)]
    else:
        rows = pick(rows, args.sample)
    (out / "papers.json").write_text(json.dumps([r["doi"] for r in rows], indent=1))
    gate = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(grade(row, out, gate) for row in rows))
    (out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    agree = sum(r.get("got") == r["expected"] for r in results)
    print(f"finished {len(results)}; agreement with adjudicated {agree}/{len(results)}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--doi", nargs="*")
    parser.add_argument("--concurrency", type=int, default=8)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
