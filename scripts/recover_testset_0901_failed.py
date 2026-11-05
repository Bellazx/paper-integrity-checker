#!/usr/bin/env python3
"""Recover failed reviews for the 0901-0907 Nature/NC batch.

This runs alongside the main batch with a separate checkpoint file.  It only
retries entries currently marked ``review_error`` and never renders or writes
database rows; the parent batch merges the validated results afterwards.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from scripts import run_scholar_batch as batch
from scripts.run_scholar_batch import run_reviews


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--fresh-review",
        action="store_true",
        help="Start new model sessions while reusing deterministic evidence artifacts",
    )
    parser.add_argument(
        "--checkpoint",
        help="Durable aggregate checkpoint path (defaults to the batch recovery checkpoint)",
    )
    parser.add_argument(
        "--progress",
        help="Per-run progress checkpoint path (defaults to the batch current checkpoint)",
    )
    parser.add_argument(
        "--results",
        default="data/output/yujing_testset_0901_0907_nature_nc/review_results_0901-0907-Nature_NC_highrisk_20261031.json",
    )
    parser.add_argument(
        "--output-root",
        default="data/output/yujing_testset_0901_0907_nature_nc",
    )
    args = parser.parse_args()

    output_root = (BASE / args.output_root).resolve()
    source = (BASE / args.results).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    failed = [item for item in payload if item.get("trigger") == "review_error"]
    # Avoid duplicate recovery work when this script is resumed.
    checkpoint = Path(args.checkpoint).expanduser() if args.checkpoint else output_root / "failed_review_recovery_20261101.json"
    current_checkpoint = Path(args.progress).expanduser() if args.progress else output_root / "failed_review_recovery_20261101_current.json"
    if not checkpoint.is_absolute():
        checkpoint = (BASE / checkpoint).resolve()
    if not current_checkpoint.is_absolute():
        current_checkpoint = (BASE / current_checkpoint).resolve()
    recovered = {}
    old_items = []
    for checkpoint_path in (checkpoint, current_checkpoint):
        if not checkpoint_path.is_file():
            continue
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                old_items.extend(payload)
        except Exception:
            continue
    # Keep only successful recoveries out of the checkpoint. A retry that is
    # still review_error must remain eligible for the next pass.
    recovered = {
        str(item.get("doi")): item
        for item in old_items
        if item.get("doi") and item.get("trigger") != "review_error"
    }
    failed = [item for item in failed if str(item.get("doi") or "") not in recovered]
    items = []
    for item in failed:
        report_json = item.get("_report_json")
        input_dir = item.get("_input_dir")
        doi = str(item.get("doi") or "").strip()
        if doi and report_json and input_dir and Path(report_json).is_file() and Path(input_dir).is_dir():
            items.append({
                "doi": doi,
                "report_json": report_json,
                "input_dir": input_dir,
                "status": "success",
            })
    if not items:
        print(f"no pending failed reviews; recovered={len(recovered)}", flush=True)
        return 0

    batch.TABLE = "yujing_testset_0901_0907_nature_nc"
    # Persist previous successes before the live pass replaces its own
    # progress file. If the process is interrupted, the durable aggregate
    # checkpoint must still include every already recovered DOI.
    durable = sorted(recovered.values(), key=lambda item: str(item.get("doi") or ""))
    checkpoint_tmp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
    checkpoint_tmp.write_text(json.dumps(durable, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint_tmp.replace(checkpoint)
    print(f"recovery scope: {len(items)}; already recovered={len(recovered)}; workers={args.workers}", flush=True)
    results = asyncio.run(run_reviews(
        items,
        output_root,
        max(1, args.workers),
        scholar="0901-0907-Nature&NC",
        summary_tag="failed_recovery_20261101",
        result_path=current_checkpoint,
        resume=not args.fresh_review,
    ))
    merged_by_doi = dict(recovered)
    for item in results:
        doi = str(item.get("doi") or "")
        if doi:
            merged_by_doi[doi] = item
    merged = list(merged_by_doi.values())
    merged.sort(key=lambda item: str(item.get("doi") or ""))
    checkpoint_tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint_tmp.replace(checkpoint)
    print(f"recovery completed={len(results)} total_checkpoint={len(merged)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
