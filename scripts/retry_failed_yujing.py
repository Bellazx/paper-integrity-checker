#!/usr/bin/env python3
"""Retry failed yujing_audit papers using real input directories."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
from datetime import datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
OUTPUT_INIT = BASE / "data/output/yujing_audit/init"


def load_failed(date: str, workers_tag: str) -> list[str]:
    papers: list[str] = []
    seen: set[str] = set()
    pattern = f"failed_papers_{date}_shard*_{workers_tag}.json"
    for path in sorted((BASE / "data/output/yujing_audit").glob(pattern)):
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data:
            paper = row.get("paper")
            if paper and paper not in seen:
                seen.add(paper)
                papers.append(paper)
    return papers


def run_one(date: str, paper: str, tag: str) -> dict:
    input_dir = BASE / f"data/input/yujing_audit/{date}" / paper
    report = OUTPUT_INIT / paper / "report.json"
    log_path = BASE / "data/tmp" / f"yujing_audit_{date}_{tag}_{paper}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if report.exists():
        return {"paper": paper, "status": "skipped_existing"}
    if not input_dir.is_dir():
        return {"paper": paper, "status": "missing_input", "error": str(input_dir)}

    cmd = [
        str(BASE / "venv/bin/python"),
        "-u",
        "main.py",
        "--input",
        str(input_dir),
        "--table-name",
        "yujing_audit",
    ]
    start = datetime.now()
    with log_path.open("w", encoding="utf-8") as log_f:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE),
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
        )
    elapsed = (datetime.now() - start).total_seconds()
    if proc.returncode == 0 and report.exists():
        return {"paper": paper, "status": "success", "elapsed_seconds": round(elapsed, 1)}
    return {
        "paper": paper,
        "status": "failed",
        "returncode": proc.returncode,
        "elapsed_seconds": round(elapsed, 1),
        "log": str(log_path.relative_to(BASE)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--workers-tag", default="w16")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--tag", default="retry_failed")
    args = ap.parse_args()

    papers = load_failed(args.date, args.workers_tag)
    todo = [p for p in papers if not (OUTPUT_INIT / p / "report.json").exists()]

    result_path = BASE / "data/tmp" / f"yujing_audit_{args.date}_{args.tag}_summary.json"
    log_path = BASE / "data/tmp" / f"yujing_audit_{args.date}_{args.tag}.log"
    result_path.parent.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    with log_path.open("a", encoding="utf-8") as log_f:
        log_f.write(f"{datetime.now():%F %T} retry start date={args.date} total_failed={len(papers)} todo={len(todo)} concurrency={args.concurrency}\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            future_map = {pool.submit(run_one, args.date, paper, args.tag): paper for paper in todo}
            for idx, fut in enumerate(concurrent.futures.as_completed(future_map), 1):
                row = fut.result()
                results.append(row)
                log_f.write(f"{datetime.now():%F %T} [{idx}/{len(todo)}] {row}\n")
                log_f.flush()

    summary = {
        "date": args.date,
        "tag": args.tag,
        "source_failed": len(papers),
        "todo": len(todo),
        "success": sum(1 for r in results if r["status"] == "success"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped_existing": len(papers) - len(todo) + sum(1 for r in results if r["status"] == "skipped_existing"),
        "results": sorted(results, key=lambda r: r["paper"]),
    }
    result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
