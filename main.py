#!/usr/bin/env python3
"""
Paper Integrity Checker — Academic paper fraud detection system.

Usage:
    python main.py --input <paper_dir>                         # single paper
    python main.py --batch <input_root> --output <dir>         # batch mode
    python main.py --batch <input_root> --workers 8            # concurrent batch
    python main.py --recheck <paper_dir> --output <dir>        # re-check single paper
    python main.py --input <paper_dir> --skip-refs             # skip reference check
"""
import argparse
import json
import logging
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import INPUT_DIR, OUTPUT_DIR
from core.pipeline import analyze_paper
from core.nature_adapter import is_nature_crawl, analyze_nature_paper
from utils.db import get_existing_dois, insert_findings
from utils.excel_metadata import find_batch_excel, load_batch_excel, merge_excel_metadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("paper-checker")


def _safe_report_namespace(namespace: str) -> str:
    parts = []
    for part in (namespace or "").strip().split("/"):
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._-")
        if cleaned:
            parts.append(cleaned[:80])
    return "/".join(parts)


def _chinese_reports_dir(output_root: str, report_namespace: str = "") -> str:
    namespace = _safe_report_namespace(report_namespace)
    base = OUTPUT_DIR / "chinese_reports" if namespace else Path(output_root) / "chinese_reports"
    return str(base / namespace) if namespace else str(base)


def _analyze_one(input_dir: str, output_dir: str, skip_refs: bool = False, chinese_reports_dir: str = None, author_type: str = "", doi_override: str = "") -> dict:
    if is_nature_crawl(input_dir):
        return analyze_nature_paper(input_dir, output_dir, skip_refs=skip_refs, chinese_reports_dir=chinese_reports_dir, author_type=author_type, doi_override=doi_override)
    else:
        return analyze_paper(input_dir, output_dir, skip_refs=skip_refs, chinese_reports_dir=chinese_reports_dir, author_type=author_type, doi_override=doi_override)


def _dir_doi(paper_dir: Path) -> str:
    """Extract DOI from a paper directory (Nature format)."""
    doi_file = paper_dir / "doi.txt"
    if doi_file.exists():
        return doi_file.read_text(encoding="utf-8").strip()
    return paper_dir.name.replace("__", "/")


def run_single(input_dir: str, output_dir: str, skip_refs: bool = False, author_type: str = "", no_db: bool = False, doi: str = "", report_namespace: str = ""):
    paper_name = Path(input_dir).name
    out = Path(output_dir) / paper_name
    cn_dir = _chinese_reports_dir(output_dir, report_namespace)
    findings = _analyze_one(input_dir, str(out), skip_refs=skip_refs, chinese_reports_dir=cn_dir, author_type=author_type, doi_override=doi)

    excel_path = find_batch_excel(Path(input_dir).parent)
    if excel_path:
        excel_data = load_batch_excel(excel_path)
        findings = merge_excel_metadata(findings, excel_data)

    if no_db:
        log.info("Skipping DB insert (--no-db)")
    elif not findings.get("pdf_generated", True):
        log.warning("Skipping DB insert: PDF generation failed")
    else:
        html_candidate = Path(input_dir) / "article.html"
        if not html_candidate.exists():
            html_candidate = Path(input_dir) / "html" / "article.html"
        html_path = str(html_candidate) if html_candidate.exists() else None
        try:
            insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path)
        except Exception as e:
            log.error("DB insert failed: %s", e)

    total = findings["summary"]["total_issues"]
    high = findings["summary"]["high_severity"]
    print(f"\n{'='*60}")
    print(f"Results for: {findings['paper'].get('title', findings['paper']['filename'])}")
    print(f"  DOI: {findings['paper'].get('doi', 'N/A')}")
    print(f"  Total issues: {total}")
    print(f"  High severity: {high}")
    print(f"  Medium severity: {findings['summary']['medium_severity']}")
    print(f"  Low severity: {findings['summary']['low_severity']}")
    print(f"{'='*60}\n")
    return findings


def _discover_papers(input_root: Path) -> list[Path]:
    paper_dirs = []
    for d in sorted(input_root.iterdir()):
        if not d.is_dir():
            continue
        has_manifest = (d / "manifest.json").exists()
        has_html = (d / "article.html").exists() or (d / "html" / "article.html").exists()
        if has_manifest and has_html:
            paper_dirs.append(d)
        elif any(d.glob("*.pdf")) or any(d.glob("*.PDF")):
            paper_dirs.append(d)
    return paper_dirs


def run_batch(input_root: str, output_root: str, skip_refs: bool = False, max_workers: int = 4, author_type: str = "", force: bool = False, no_db: bool = False, report_namespace: str = ""):
    input_root = Path(input_root)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    paper_dirs = _discover_papers(input_root)
    if not paper_dirs:
        log.error("No paper directories found in %s", input_root)
        sys.exit(1)

    excel_path = find_batch_excel(input_root)
    excel_data = {}
    if excel_path:
        excel_data = load_batch_excel(excel_path)
        log.info("Loaded %d DOI entries from batch Excel for metadata matching", len(excel_data))

    existing_dois = set() if force else get_existing_dois()
    if existing_dois:
        before = len(paper_dirs)
        paper_dirs = [d for d in paper_dirs if _dir_doi(d) not in existing_dois]
        skipped = before - len(paper_dirs)
        if skipped:
            log.info("Skipped %d papers already in DB, %d remaining", skipped, len(paper_dirs))

    if not paper_dirs:
        log.info("All papers already processed. Nothing to do.")
        return

    log.info("Found %d paper(s) to analyze (workers=%d)", len(paper_dirs), max_workers)

    batch_results = []
    lock = threading.Lock()
    start_time = time.time()

    def process_one(paper_dir: Path) -> dict:
        t0 = time.time()
        try:
            out = output_root / paper_dir.name
            cn_dir = _chinese_reports_dir(str(output_root), report_namespace)
            findings = _analyze_one(str(paper_dir), str(out), skip_refs=skip_refs, chinese_reports_dir=cn_dir, author_type=author_type)
            elapsed = time.time() - t0

            if excel_data:
                findings = merge_excel_metadata(findings, excel_data)

            if no_db:
                pass
            elif not findings.get("pdf_generated", True):
                log.warning("Skipping DB insert for %s: PDF generation failed", paper_dir.name)
            else:
                html_candidate = paper_dir / "article.html"
                if not html_candidate.exists():
                    html_candidate = paper_dir / "html" / "article.html"
                html_path = str(html_candidate) if html_candidate.exists() else None
                try:
                    insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path)
                except Exception as e:
                    log.error("DB insert failed for %s: %s", paper_dir.name, e)

            return {
                "paper": paper_dir.name,
                "doi": findings["paper"].get("doi", ""),
                "title": findings["paper"].get("title", ""),
                "status": "success",
                "total_issues": findings["summary"]["total_issues"],
                "high_severity": findings["summary"]["high_severity"],
                "elapsed_seconds": round(elapsed, 1),
            }
        except Exception as e:
            elapsed = time.time() - t0
            log.error("Failed to analyze %s: %s", paper_dir.name, e, exc_info=True)
            return {
                "paper": paper_dir.name,
                "status": "error",
                "error": str(e),
                "elapsed_seconds": round(elapsed, 1),
            }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_one, d): d for d in paper_dirs}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            with lock:
                batch_results.append(result)
            status = "OK" if result["status"] == "success" else "FAILED"
            issues = result.get("total_issues", "N/A")
            elapsed = result.get("elapsed_seconds", 0)
            log.info("[%d/%d] [%s] %s (%s issues, %.0fs)",
                     i, len(paper_dirs), status, result["paper"], issues, elapsed)

    total_time = time.time() - start_time
    batch_results.sort(key=lambda r: r["paper"])

    success = sum(1 for r in batch_results if r["status"] == "success")
    failed = sum(1 for r in batch_results if r["status"] == "error")

    summary = {
        "total_papers": len(paper_dirs),
        "success": success,
        "failed": failed,
        "total_time_seconds": round(total_time, 1),
        "avg_time_per_paper": round(total_time / len(paper_dirs), 1) if paper_dirs else 0,
        "workers": max_workers,
        "results": batch_results,
    }

    summary_path = output_root / "batch_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"Batch complete: {success} OK, {failed} failed, {total_time:.0f}s total")
    print(f"Average: {total_time / len(paper_dirs):.1f}s/paper ({max_workers} workers)")
    print(f"Summary: {summary_path}")
    print(f"{'='*60}\n")

    if failed > 0:
        failed_items = [r for r in batch_results if r["status"] == "error"]
        failed_path = output_root / "failed_papers.json"
        with open(failed_path, "w", encoding="utf-8") as f:
            json.dump(failed_items, f, ensure_ascii=False, indent=2)

        print(f"{'!'*60}")
        print(f"  WARNING: {failed} paper(s) FAILED processing")
        print(f"  Failed list saved to: {failed_path}")
        print(f"{'!'*60}")
        for r in failed_items:
            print(f"  FAILED: {r['paper']} — {r.get('error', 'unknown error')}")
        print()


def run_recheck(paper_dir: str, output_dir: str, skip_refs: bool = False, author_type: str = ""):
    paper_name = Path(paper_dir).name
    out = Path(output_dir) / paper_name
    if out.exists():
        log.info("Removing old output: %s", out)
        shutil.rmtree(out)
    return run_single(paper_dir, output_dir, skip_refs=skip_refs, author_type=author_type)


def main():
    parser = argparse.ArgumentParser(description="Paper Integrity Checker")
    parser.add_argument("--input", "-i", help="Single paper directory")
    parser.add_argument("--batch", "-b", help="Batch input root directory")
    parser.add_argument("--output", "-o", default=str(OUTPUT_DIR), help="Output directory")
    parser.add_argument("--skip-refs", action="store_true", help="Skip reference verification")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Concurrent workers for batch mode")
    parser.add_argument("--recheck", help="Re-check a single paper (clears old output)")
    parser.add_argument("--author-type", default="", help="SJTU author type for this batch (e.g. 通讯作者, 第一作者)")
    parser.add_argument("--doi", default="", help="Override DOI for a single paper (authoritative; --input only)")
    parser.add_argument("--report-namespace", default="", help="Subdirectory under chinese_reports for generated PDFs")
    parser.add_argument("--no-db", action="store_true", help="Skip database insertion")
    parser.add_argument("--force", action="store_true", help="Force re-analysis even if paper already in DB")
    args = parser.parse_args()

    if not args.input and not args.batch and not args.recheck:
        parser.print_help()
        sys.exit(1)

    if args.recheck:
        run_recheck(args.recheck, args.output, args.skip_refs, author_type=args.author_type)
    elif args.input:
        run_single(args.input, args.output, args.skip_refs, author_type=args.author_type, no_db=args.no_db, doi=args.doi, report_namespace=args.report_namespace)
    elif args.batch:
        run_batch(args.batch, args.output, args.skip_refs, max_workers=args.workers, author_type=args.author_type, force=args.force, no_db=args.no_db, report_namespace=args.report_namespace)


if __name__ == "__main__":
    main()
