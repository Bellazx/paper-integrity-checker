#!/usr/bin/env python3
"""v3 review of the initial high-risk papers in the 0901-0907 Nature/NC testset,
written to a separate comparison table.

    venv/bin/python scripts/run_testset_0901_v3_compare.py --workers 24 [--wait-initial]

Reads the initial reports under data/output/<table>/init, reviews every paper
the initial screening rated 高风险 with the v3 engine, then renders the review
PDFs and writes review_result / review_report_url into <table>. The table has
no scholar column, so DB updates are matched by DOI only. Progress is
checkpointed; rerunning resumes and skips papers already reviewed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ["REVIEW_ENGINE"] = "v3"
os.environ.setdefault("REVIEW_V3_MAX_SESSIONS", "300")

from scripts import run_scholar_batch as batch  # noqa: E402
from scripts.run_scholar_batch import render_and_update_reviews, run_reviews  # noqa: E402

TABLE = "yujing_testset_0901_0907_nature_nc_v3cmp"


def initial_items(input_root: Path, output_root: Path) -> tuple[list[dict], list[dict]]:
    items, skipped = [], []
    for report_path in sorted((output_root / "init").glob("*/report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            skipped.append({"report": str(report_path), "error": str(exc)})
            continue
        if report.get("initial_risk_level") != "高风险":
            continue
        paper = report.get("paper") or {}
        doi = str(paper.get("doi") or "").strip()
        paper_dir = input_root / report_path.parent.name
        if not doi or not paper_dir.is_dir():
            skipped.append({"report": str(report_path), "doi": doi, "input_dir": str(paper_dir)})
            continue
        items.append({"dir": paper_dir.name, "doi": doi, "title": str(paper.get("title") or ""), "status": "success",
                      "report_json": str(report_path), "input_dir": str(paper_dir)})
    return items, skipped


def initial_running(output_root: Path) -> bool:
    """The initial batch writes batch_summary.json when it ends."""
    return not (output_root / "batch_summary.json").is_file()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument("--input-root", default="data/input/testset/0901-0907-Nature&NC")
    parser.add_argument("--wait-initial", action="store_true", help="wait for the initial batch to finish first")
    args = parser.parse_args()
    input_root = (BASE / args.input_root).resolve()
    output_root = BASE / "data" / "output" / TABLE
    batch.TABLE = TABLE
    while args.wait_initial and initial_running(output_root):
        time.sleep(60)

    items, skipped = initial_items(input_root, output_root)
    results_path = output_root / "review_results_v3.json"
    progress_path = output_root / "review_progress_v3.json"
    done_path = output_root / "review_done_v3.json"
    done = {}
    # The progress file holds papers finished by an interrupted run and is
    # rewritten as soon as this run starts; finished papers are kept in
    # done_path so a second interruption loses nothing.
    for path in (done_path, progress_path, results_path):
        if path.is_file():
            done.update({str(r.get("doi")): r for r in json.loads(path.read_text(encoding="utf-8"))
                         if r.get("doi") and r.get("trigger") != "review_error"})
    done_path.write_text(json.dumps(list(done.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    pending = [item for item in items if item["doi"] not in done]
    (output_root / "review_scope_v3.json").write_text(
        json.dumps({"high_risk": len(items), "pending": len(pending), "skipped": skipped}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"initial high-risk {len(items)}; already reviewed {len(done)}; pending {len(pending)}", flush=True)

    reviewed = asyncio.run(run_reviews(pending, output_root, max(1, args.workers), result_path=progress_path,
                                       resume=True)) if pending else []
    merged = sorted(list(done.values()) + reviewed, key=lambda r: str(r.get("doi") or ""))
    progress_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    results_path.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    # Empty scholar: this table has no scholar column, updates match by DOI.
    rc = render_and_update_reviews(merged, output_root, "", result_path=results_path)
    errors = sum(1 for r in merged if r.get("trigger") == "review_error")
    print(f"reviewed {len(merged)}; review errors {errors}; renderer rc {rc}", flush=True)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
