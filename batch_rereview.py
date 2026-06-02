#!/usr/bin/env python3
"""Batch re-review papers using adaptive review from review.py.

Usage:
    python3 batch_rereview.py [--concurrency 3] [--limit 0] [--start 0]

Processes papers from data/tmp/rereview_all_ordered.json using run_review_single()
which implements adaptive review with per-paper resume. Completed papers are
saved immediately; PDF reports are generated in small batches.
"""

import asyncio
import glob
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/paper-integrity-checker")
from api.services.review import run_review_single

# Ensure runtime dir on the 1T data disk exists before logging attaches its file handler
Path("/opt/paper-integrity-checker/data/tmp").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/opt/paper-integrity-checker/data/tmp/batch_rereview.log"),
    ],
)
log = logging.getLogger(__name__)

BASE = Path("/opt/paper-integrity-checker")
OUTPUT_DIR = BASE / "data" / "output"
INPUT_DIR = BASE / "data" / "input"
REVIEW_V2_DIR = BASE / "data" / "output" / "review_v2"
# All runtime files live under data/ (the 1T disk), never /tmp (root disk fills up → nginx 500)
TMP_DIR = BASE / "data" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_FILE = TMP_DIR / "rereview_voted_results.json"
REPORT_SCRIPT = "/opt/.claude/skills/paper-batch-review/scripts/generate_review_report.py"
CONCURRENCY = 5
BATCH_SIZE = 5
DEFAULT_TIME_BUDGET_MIN = 55.0
DEFAULT_STOP_MARGIN_MIN = 5.0


def find_paths(doi: str):
    slug = doi.replace("/", "_")
    report = OUTPUT_DIR / slug / "report.json"
    if not report.exists():
        slug_lower = slug.lower()
        for c in OUTPUT_DIR.iterdir():
            if c.name.lower() == slug_lower and c.is_dir():
                report = c / "report.json"
                slug = c.name
                break

    if not report.exists():
        return None, None

    input_matches = glob.glob(str(INPUT_DIR / "*" / slug))
    if not input_matches:
        input_matches = glob.glob(str(INPUT_DIR / "*" / slug.lower()))
    if not input_matches:
        for d in INPUT_DIR.iterdir():
            if d.is_dir():
                for sub in d.iterdir():
                    if sub.name.lower() == slug.lower():
                        input_matches = [str(sub)]
                        break
            if input_matches:
                break

    input_dir = input_matches[0] if input_matches else str(OUTPUT_DIR / slug)
    return str(report), input_dir


def load_done():
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE) as f:
            results = json.load(f)
        return {r["doi"] for r in results}, results
    return set(), []


def save_results(results):
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def _seconds_left(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _can_start_more(deadline: float | None, stop_margin_seconds: float) -> bool:
    seconds_left = _seconds_left(deadline)
    return seconds_left is None or seconds_left > stop_margin_seconds


def generate_reports(batch_results: list[dict]):
    """Generate PDF reports and update DB for a batch of results."""
    if not batch_results:
        return
    batch_file = str(TMP_DIR / f"rereview_batch_{int(time.time())}.json")
    with open(batch_file, "w") as f:
        json.dump(batch_results, f, ensure_ascii=False, indent=2)

    log.info("Generating PDFs + updating DB for %d papers...", len(batch_results))
    try:
        proc = subprocess.run(
            [sys.executable, REPORT_SCRIPT, "--results", batch_file, "--output", str(REVIEW_V2_DIR)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.stdout:
            for line in proc.stdout.strip().split("\n")[-10:]:
                log.info("  %s", line)
        if proc.returncode != 0 and proc.stderr:
            log.warning("Report script stderr: %s", proc.stderr[-300:])
    except Exception as e:
        log.error("Report generation failed: %s", e)


async def process_one(doi: str, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        report_path, input_dir = find_paths(doi)
        if not report_path:
            log.warning("No report.json for %s, skipping", doi)
            return {
                "doi": doi,
                "result": "高风险",
                "trigger": "report_not_found",
                "image_review": "未找到检测报告，无法复核。",
                "data_review": "未找到检测报告，无法复核。",
                "ref_review": "未找到检测报告。",
                "verdict": "确认高风险",
                "reason": "检测报告文件不存在，无法完成复核。",
            }

        log.info("Starting 3-agent review: %s", doi)
        t0 = time.time()
        try:
            result = await run_review_single(
                doi=doi,
                report_json_path=report_path,
                input_dir=input_dir,
                output_dir=str(REVIEW_V2_DIR),
            )
            elapsed = time.time() - t0
            log.info("Done: %s → %s (vote: %s) in %.0fs",
                     doi, result.get("result"), result.get("vote"), elapsed)
            return result
        except Exception as e:
            elapsed = time.time() - t0
            log.error("Error: %s after %.0fs: %s", doi, elapsed, e)
            return {
                "doi": doi,
                "result": "高风险",
                "trigger": "review_error",
                "image_review": f"复核过程中出现错误：{e}",
                "data_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
                "ref_review": "复核过程中出现错误。",
                "verdict": "确认高风险",
                "reason": f"复核过程中出现错误：{e}",
            }


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=0, help="Max papers (0=all)")
    parser.add_argument("--start", type=int, default=0, help="Start index")
    parser.add_argument(
        "--time-budget-min",
        type=float,
        default=DEFAULT_TIME_BUDGET_MIN,
        help="Stop starting new papers after this many minutes (0=disabled)",
    )
    parser.add_argument(
        "--stop-margin-min",
        type=float,
        default=DEFAULT_STOP_MARGIN_MIN,
        help="Do not start a new paper when less than this many minutes remain",
    )
    args = parser.parse_args()

    with open(TMP_DIR / "rereview_all_ordered.json") as f:
        all_dois = json.load(f)

    if args.limit > 0:
        all_dois = all_dois[args.start:args.start + args.limit]
    elif args.start > 0:
        all_dois = all_dois[args.start:]

    done_set, existing_results = load_done()
    remaining = [d for d in all_dois if d not in done_set]

    deadline = None
    if args.time_budget_min and args.time_budget_min > 0:
        deadline = time.monotonic() + args.time_budget_min * 60
    stop_margin_seconds = max(0.0, args.stop_margin_min * 60)

    log.info(
        "Total: %d, already done: %d, remaining: %d, concurrency: %d, time_budget_min: %.1f",
        len(all_dois), len(done_set), len(remaining), args.concurrency,
        args.time_budget_min if deadline is not None else 0.0,
    )

    if not remaining:
        log.info("All papers already reviewed!")
        return

    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    all_results = list(existing_results)
    recent_results: list[dict] = []
    pending: set[asyncio.Task] = set()
    task_to_doi: dict[asyncio.Task, str] = {}
    cursor = 0
    stopped_for_budget = False

    try:
        while cursor < len(remaining) or pending:
            while (
                cursor < len(remaining)
                and len(pending) < max(1, args.concurrency)
                and _can_start_more(deadline, stop_margin_seconds)
            ):
                doi = remaining[cursor]
                cursor += 1
                task = asyncio.create_task(process_one(doi, semaphore))
                pending.add(task)
                task_to_doi[task] = doi
                log.info(
                    "Queued review %d/%d: %s (time left: %s)",
                    cursor, len(remaining), doi,
                    "unlimited" if deadline is None else f"{_seconds_left(deadline) / 60:.1f}min",
                )

            if not pending:
                if cursor < len(remaining):
                    stopped_for_budget = True
                    log.info(
                        "Time budget margin reached; stopping before next paper. Remaining this run: %d",
                        len(remaining) - cursor,
                    )
                break

            timeout = _seconds_left(deadline)
            done, pending = await asyncio.wait(
                pending,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                stopped_for_budget = True
                log.warning("Time budget exhausted; cancelling %d in-flight review(s)", len(pending))
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                pending.clear()
                break

            for task in done:
                doi = task_to_doi.pop(task, "")
                try:
                    result = task.result()
                except asyncio.CancelledError:
                    continue
                except Exception as e:
                    log.error("Unexpected task failure for %s: %s", doi, e)
                    result = {
                        "doi": doi,
                        "result": "高风险",
                        "trigger": "review_error",
                        "image_review": f"复核过程中出现错误：{e}",
                        "data_review": "复核过程中出现错误，按规则取建议高风险，建议人工进一步确认。",
                        "ref_review": "复核过程中出现错误。",
                        "verdict": "确认高风险",
                        "reason": f"复核过程中出现错误：{e}",
                    }

                all_results.append(result)
                recent_results.append(result)
                save_results(all_results)

                if len(recent_results) >= max(1, args.batch_size):
                    generate_reports(recent_results)
                    recent_results = []

                high = sum(1 for r in all_results if r.get("result") == "高风险")
                low = len(all_results) - high
                total = len(remaining) + len(done_set)
                log.info(
                    "Progress: %d/%d (%.1f%%) | High: %d, Low: %d | Risk rate: %.1f%%",
                    len(all_results), total, 100.0 * len(all_results) / total,
                    high, low, 100.0 * high / len(all_results),
                )

            if cursor < len(remaining) and not _can_start_more(deadline, stop_margin_seconds):
                stopped_for_budget = True
                log.info(
                    "Time budget margin reached; no new papers will be started. Waiting for %d running review(s).",
                    len(pending),
                )

    finally:
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if recent_results:
            save_results(all_results)
            generate_reports(recent_results)

    if stopped_for_budget:
        done_after_run = {r.get("doi") for r in all_results}
        next_remaining = sum(1 for doi in all_dois if doi not in done_after_run)
        log.info(
            "=== STOPPED FOR TIME BUDGET: reviewed %d total, %d still remaining for next run ===",
            len(all_results), next_remaining,
        )
    else:
        log.info("=== COMPLETE: %d papers reviewed ===", len(all_results))


if __name__ == "__main__":
    asyncio.run(main())
