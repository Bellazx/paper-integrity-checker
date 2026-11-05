#!/usr/bin/env python3
"""Review only the already-screened high-risk papers in the 0901-0907 testset.

The initial reports are treated as the scope manifest.  This script does not
rerun initial screening and does not send low-risk papers to the review stage.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scripts import run_scholar_batch as batch
from scripts.run_scholar_batch import (
    BASE,
    render_and_update_reviews,
    run_reviews,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--input-root",
        default="data/input/testset/0901-0907-Nature&NC",
    )
    parser.add_argument(
        "--output-root",
        default="data/output/yujing_testset_0901_0907_nature_nc",
    )
    args = parser.parse_args()

    input_root = (BASE / args.input_root).resolve()
    output_root = (BASE / args.output_root).resolve()
    # The renderer uses run_scholar_batch.TABLE as its database target.  Set
    # it explicitly because the module default is yujing_scholar.
    batch.TABLE = "yujing_testset_0901_0907_nature_nc"
    initial_root = output_root / "init"
    items = []
    skipped = []
    for report_path in sorted(initial_root.glob("*/report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            skipped.append({"report": str(report_path), "error": str(exc)})
            continue
        if report.get("initial_risk_level") != "高风险":
            continue
        doi = str((report.get("paper") or {}).get("doi") or "").strip()
        paper_dir = input_root / report_path.parent.name
        if not doi or not paper_dir.is_dir():
            skipped.append({"report": str(report_path), "doi": doi, "input_dir": str(paper_dir)})
            continue
        items.append({
            "dir": paper_dir.name,
            "doi": doi,
            "title": str((report.get("paper") or {}).get("title") or ""),
            "status": "success",
            "report_json": str(report_path),
            "input_dir": str(paper_dir),
        })

    existing_results = {}
    if args.resume:
        result_sources = list(output_root.glob("review_results_*"))
        completed_backup = output_root / "completed_valid_results_20261031.json"
        if completed_backup.is_file():
            result_sources.append(completed_backup)
        for result_path in result_sources:
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, list):
                for item in payload:
                    doi = str(item.get("doi") or "").strip()
                    if doi and item.get("trigger") != "review_error":
                        existing_results[doi] = item
    if existing_results:
        items = [item for item in items if item["doi"] not in existing_results]
    manifest = output_root / "highrisk_review_scope_20261031.json"
    manifest.write_text(json.dumps({"count": len(items), "items": items, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"review scope: {len(items)} pending initial high-risk papers; completed={len(existing_results)}; skipped={len(skipped)}", flush=True)
    if not items:
        return 1

    reviewed = asyncio.run(run_reviews(
        items,
        output_root,
        max(1, args.workers),
        scholar="0901-0907-Nature&NC",
        summary_tag="highrisk_20261031",
        resume=args.resume,
    ))
    if existing_results:
        reviewed = list(existing_results.values()) + reviewed
        reviewed.sort(key=lambda item: str(item.get("doi") or ""))
    result_path = output_root / "review_results_0901-0907-Nature_NC_highrisk_20261031.json"
    result_path.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8")
    rc = render_and_update_reviews(
        reviewed,
        output_root,
        "0901-0907-Nature&NC",
        summary_tag="highrisk_20261031",
        result_path=result_path,
    )
    print(f"reviewed={len(reviewed)} renderer_rc={rc} results={result_path}", flush=True)
    return 0 if not any(item.get("trigger") == "review_error" for item in reviewed) else 2


if __name__ == "__main__":
    raise SystemExit(main())
