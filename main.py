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
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Same limits as paper-api.service, set before numpy/OpenCV load: unlimited,
# each paper thread starts one OpenCV/OpenBLAS thread per CPU and concurrent
# papers spend their time contending for locks.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "OPENCV_FOR_THREADS_NUM"):
    os.environ.setdefault(_name, "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import INPUT_DIR, OUTPUT_DIR
from core.pipeline import initial_pdf_skipped, analyze_paper
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


def _table_root(table_name: str = "") -> Path:
    """Return the per-table output root.

    With --table-name=yujing_4050_202606 set, all outputs (init/<slug>/,
    chinese_reports/, review_v2/, batch_summary.json) live under
    data/output/yujing_4050_202606/. Without it, returns OUTPUT_DIR (legacy).
    """
    if not table_name:
        return OUTPUT_DIR
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", table_name).strip("_-")[:80]
    return OUTPUT_DIR / safe


def _chinese_reports_dir(output_root: str, report_namespace: str = "", table_root: str = "") -> str:
    """Resolve the chinese_reports directory.

    Order of precedence:
      1. table_root + "chinese_reports"  (when --table-name is set; new layout)
      2. OUTPUT_DIR / "chinese_reports" / <namespace>  (legacy --report-namespace)
      3. output_root / "chinese_reports"  (legacy flat)
    """
    namespace = _safe_report_namespace(report_namespace)
    if table_root:
        base = Path(table_root) / "chinese_reports"
        return str(base)
    if namespace:
        return str(OUTPUT_DIR / "chinese_reports" / namespace)
    return str(Path(output_root) / "chinese_reports")


def _analyze_one(input_dir: str, output_dir: str, skip_refs: bool = False, chinese_reports_dir: str = None, author_type: str = "", doi_override: str = "") -> dict:
    from pathlib import Path as _P
    if any(_P(input_dir).glob("*.pdf")) or any(_P(input_dir).glob("*.PDF")):
        return analyze_paper(input_dir, output_dir, skip_refs=skip_refs, chinese_reports_dir=chinese_reports_dir, author_type=author_type, doi_override=doi_override)
    if is_nature_crawl(input_dir):
        return analyze_nature_paper(input_dir, output_dir, skip_refs=skip_refs, chinese_reports_dir=chinese_reports_dir, author_type=author_type, doi_override=doi_override)
    return analyze_paper(input_dir, output_dir, skip_refs=skip_refs, chinese_reports_dir=chinese_reports_dir, author_type=author_type, doi_override=doi_override)


def _dir_doi(paper_dir: Path) -> str:
    """Extract DOI from a paper directory (handles all formats: A, B, OUP, IOP)."""
    from modules.doi_utils import dir_name_to_doi
    return dir_name_to_doi(paper_dir)


def _source_path_for_root(root: Path) -> str:
    """Return a stable data-relative batch path for database provenance."""
    base = Path(__file__).resolve().parent / "data"
    resolved = root.resolve()
    try:
        relative = resolved.relative_to(base.resolve())
        return relative.as_posix().rstrip("/") + "/"
    except ValueError:
        return resolved.as_posix()


def _held_message(paper: str, mismatch: dict) -> str:
    return (f"Not written to the database: {paper} holds a PDF whose DOI is {mismatch['pdf_doi']}, "
            f"not {mismatch['directory_doi']}; check the upload and rerun with --input/--doi")


def run_single(input_dir: str, output_dir: str, skip_refs: bool = False, author_type: str = "", no_db: bool = False, doi: str = "", report_namespace: str = "", table_root: str = "", table_name: str = ""):
    paper_name = Path(input_dir).name
    out = Path(output_dir) / paper_name
    cn_dir = _chinese_reports_dir(output_dir, report_namespace, table_root=table_root)
    findings = _analyze_one(input_dir, str(out), skip_refs=skip_refs, chinese_reports_dir=cn_dir, author_type=author_type, doi_override=doi)

    excel_path = find_batch_excel(Path(input_dir).parent)
    if excel_path:
        excel_data = load_batch_excel(excel_path)
        findings = merge_excel_metadata(findings, excel_data)

    if no_db:
        log.info("Skipping DB insert (--no-db)")
    elif findings.get("doi_mismatch"):
        log.warning(_held_message(paper_name, findings["doi_mismatch"]))
    elif not findings.get("pdf_generated", True) and not initial_pdf_skipped(findings):
        raise RuntimeError("Initial PDF generation failed; result was not inserted into DB")
    else:
        html_candidate = Path(input_dir) / "article.html"
        if not html_candidate.exists():
            html_candidate = Path(input_dir) / "html" / "article.html"
        html_path = str(html_candidate) if html_candidate.exists() else None
        try:
            insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path,
                            namespace=table_name,
                            source_path=_source_path_for_root(Path(input_dir).parent))
        except Exception as e:
            log.error("DB insert failed: %s", e)
            raise RuntimeError(f'Initial report DB write failed: {e}') from e

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


def _complexity_profile(paper_dir: Path) -> tuple[bool, str]:
    """Identify papers that should run in a disposable analysis process.

    Native PDF/image/spreadsheet libraries can segfault.  A small preflight
    keeps ordinary papers in the fast thread pool while isolating papers whose
    input is likely to create a large native workload.
    """
    data_suffixes = {".xlsx", ".xls", ".csv", ".docx", ".fcs", ".sav", ".zip", ".rar", ".pdb", ".cif"}
    image_suffixes = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif"}
    data_files = []
    image_files = []
    data_bytes = 0
    pdf_bytes = 0
    for path in paper_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if suffix in data_suffixes:
            data_files.append(path)
            data_bytes += size
        elif suffix in image_suffixes:
            image_files.append(path)
        elif suffix == ".pdf":
            pdf_bytes += size

    reasons = []
    if len(data_files) >= 5 or data_bytes >= 8 * 1024 * 1024:
        reasons.append(f"source data {len(data_files)} files/{data_bytes / 1048576:.1f} MiB")
    if len(image_files) >= 120:
        reasons.append(f"standalone images {len(image_files)}")
    if pdf_bytes >= 25 * 1024 * 1024:
        reasons.append(f"PDFs {pdf_bytes / 1048576:.1f} MiB")
    return bool(reasons), "; ".join(reasons)


def _analyze_one_isolated(
    paper_dir: Path,
    output_dir: Path,
    skip_refs: bool,
    author_type: str,
    table_name: str,
) -> dict:
    """Run one complex paper in a disposable child interpreter."""
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path("data/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", paper_dir.name)[:100]
    log_path = tmp_dir / f"isolated_{safe}_{os.getpid()}.log"
    cmd = [sys.executable, "-u", "main.py", "--input", str(paper_dir), "--no-db"]
    if table_name:
        cmd.extend(["--table-name", table_name])
    else:
        cmd.extend(["--output", str(output_dir.parent)])
    if skip_refs:
        cmd.append("--skip-refs")
    if author_type:
        cmd.extend(["--author-type", author_type])

    env = os.environ.copy()
    for key in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
        "OPENCV_FOR_THREADS_NUM",
    ):
        env[key] = "1"
    timeout = int(os.getenv("PAPER_COMPLEX_TIMEOUT", "3600"))
    with log_path.open("a", encoding="utf-8") as log_file:
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(Path(__file__).resolve().parent),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"isolated analysis timed out after {timeout}s; log={log_path}") from exc
    if completed.returncode != 0:
        if completed.returncode < 0:
            reason = f"signal {-completed.returncode}"
        else:
            reason = f"exit code {completed.returncode}"
        raise RuntimeError(f"isolated analysis failed ({reason}); log={log_path}")

    report_path = output_dir / "report.json"
    if not report_path.is_file():
        raise RuntimeError(f"isolated analysis produced no report.json; log={log_path}")
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"isolated report is unreadable: {report_path}; log={log_path}") from exc


def run_batch(input_root: str, output_root: str, skip_refs: bool = False, max_workers: int = 4, author_type: str = "", force: bool = False, no_db: bool = False, report_namespace: str = "", table_root: str = "", table_name: str = "", resume_existing_output: bool = False, summary_tag: str = "", isolate_complex: bool = True):
    input_root = Path(input_root)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    # When --table-name is set, the per-paper init outputs live under output_root/init/<slug>/
    # so they don't collide with chinese_reports/ and review_v2/ siblings. The table root is
    # the value of --table-name's resolved path (= output_root when table_root is empty).
    paper_output_root = output_root / "init" if table_root else output_root

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

    if resume_existing_output:
        before = len(paper_dirs)
        paper_dirs = [
            d for d in paper_dirs
            if not (paper_output_root / d.name / "report.json").exists()
            and not (paper_output_root / d.name / "analysis_failed.json").exists()
        ]
        skipped = before - len(paper_dirs)
        if skipped:
            log.info("Skipped %d papers with existing report.json, %d remaining", skipped, len(paper_dirs))

    if not paper_dirs:
        log.info("All papers already processed. Nothing to do.")
        return

    log.info("Found %d paper(s) to analyze (workers=%d)", len(paper_dirs), max_workers)

    batch_results = []
    lock = threading.Lock()
    start_time = time.time()

    def process_one(paper_dir: Path) -> dict:
        t0 = time.time()
        findings = None
        try:
            out = paper_output_root / paper_dir.name
            cn_dir = _chinese_reports_dir(str(output_root), report_namespace, table_root=table_root)
            complex_paper, complexity_reason = _complexity_profile(paper_dir)
            if isolate_complex and complex_paper:
                log.info("Using isolated process for complex paper %s (%s)", paper_dir.name, complexity_reason)
                findings = _analyze_one_isolated(
                    paper_dir, out, skip_refs=skip_refs, author_type=author_type,
                    table_name=table_name,
                )
            else:
                findings = _analyze_one(str(paper_dir), str(out), skip_refs=skip_refs, chinese_reports_dir=cn_dir, author_type=author_type)
            elapsed = time.time() - t0

            if excel_data:
                findings = merge_excel_metadata(findings, excel_data)

            held = None
            if no_db:
                pass
            elif findings.get("doi_mismatch"):
                held = findings["doi_mismatch"]
                log.warning(_held_message(paper_dir.name, held))
            elif not findings.get("pdf_generated", True) and not initial_pdf_skipped(findings):
                raise RuntimeError("Initial PDF generation failed; result was not inserted into DB")
            else:
                html_candidate = paper_dir / "article.html"
                if not html_candidate.exists():
                    html_candidate = paper_dir / "html" / "article.html"
                html_path = str(html_candidate) if html_candidate.exists() else None
                try:
                    insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path,
                                    namespace=table_name,
                                    source_path=_source_path_for_root(Path(input_root)))
                except Exception as e:
                    log.error("DB insert failed for %s: %s", paper_dir.name, e)
                    raise RuntimeError(f'Initial report DB write failed: {e}') from e

            return {
                "paper": paper_dir.name,
                "doi": findings["paper"].get("doi", ""),
                "title": findings["paper"].get("title", ""),
                "status": "held" if held else "success",
                **({"held_reason": "doi_mismatch", "doi_mismatch": held} if held else {}),
                "total_issues": findings["summary"]["total_issues"],
                "high_severity": findings["summary"]["high_severity"],
                "elapsed_seconds": round(elapsed, 1),
            }
        except Exception as e:
            elapsed = time.time() - t0
            log.error("Failed to analyze %s: %s", paper_dir.name, e, exc_info=True)
            if isolate_complex:
                try:
                    out = paper_output_root / paper_dir.name
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "analysis_failed.json").write_text(
                        json.dumps({
                            "paper": paper_dir.name,
                            "error": str(e),
                            "time": time.time(),
                        }, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            return {
                "paper": paper_dir.name,
                "status": "error",
                "error": str(e),
                "elapsed_seconds": round(elapsed, 1),
            }
        finally:
            del findings
            gc.collect()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_one, d): d for d in paper_dirs}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            with lock:
                batch_results.append(result)
            status = {"success": "OK", "held": "HELD"}.get(result["status"], "FAILED")
            issues = result.get("total_issues", "N/A")
            elapsed = result.get("elapsed_seconds", 0)
            log.info("[%d/%d] [%s] %s (%s issues, %.0fs)",
                     i, len(paper_dirs), status, result["paper"], issues, elapsed)

    total_time = time.time() - start_time
    batch_results.sort(key=lambda r: r["paper"])

    success = sum(1 for r in batch_results if r["status"] == "success")
    failed = sum(1 for r in batch_results if r["status"] == "error")
    held = [r for r in batch_results if r["status"] == "held"]

    summary = {
        "total_papers": len(paper_dirs),
        "success": success,
        "failed": failed,
        "held": len(held),
        "total_time_seconds": round(total_time, 1),
        "avg_time_per_paper": round(total_time / len(paper_dirs), 1) if paper_dirs else 0,
        "workers": max_workers,
        "summary_tag": summary_tag,
        "results": batch_results,
    }

    safe_summary_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", summary_tag).strip("._-")[:120]
    summary_name = f"batch_summary_{safe_summary_tag}.json" if safe_summary_tag else "batch_summary.json"
    failed_name = f"failed_papers_{safe_summary_tag}.json" if safe_summary_tag else "failed_papers.json"

    summary_path = output_root / summary_name
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"Batch complete: {success} OK, {failed} failed, {len(held)} held, {total_time:.0f}s total")
    for r in held:
        print(f"  HELD (not in DB): {r['paper']} — PDF DOI {r['doi_mismatch']['pdf_doi']} != directory {r['doi_mismatch']['directory_doi']}")
    print(f"Average: {total_time / len(paper_dirs):.1f}s/paper ({max_workers} workers)")
    print(f"Summary: {summary_path}")
    print(f"{'='*60}\n")

    if failed > 0:
        failed_items = [r for r in batch_results if r["status"] == "error"]
        failed_path = output_root / failed_name
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
    parser.add_argument("--resume-existing-output", action="store_true",
                        help="In batch mode, skip paper directories that already have report.json in the output tree")
    parser.add_argument("--summary-tag", default="",
                        help="Suffix batch summary/failed files to avoid collisions when running sharded batches")
    parser.add_argument("--isolate-complex", dest="isolate_complex", action="store_true", default=True,
                        help="Run complex papers in disposable child processes (default: enabled)")
    parser.add_argument("--no-isolate-complex", dest="isolate_complex", action="store_false",
                        help="Disable automatic child-process isolation for complex papers")
    parser.add_argument("--table-name", default="",
                        help="Namespace ALL outputs under data/output/<table-name>/ (init/<slug>/, chinese_reports/, review_v2/). "
                             "When set, per-paper dir is <table>/init/<doi_slug>/. When unset, legacy flat layout is used.")
    args = parser.parse_args()

    if not args.input and not args.batch and not args.recheck:
        parser.print_help()
        sys.exit(1)

    # Resolve table_root once for downstream use.
    # When --table-name is set, the effective output_root for the batch runner IS the table root
    # (the per-paper init/<slug> segment is added inside run_batch). For --input / --recheck we
    # still keep args.output as the per-paper parent for backwards compat, but pin the chinese_reports
    # location to <table_root>/chinese_reports via table_root.
    table_root = str(_table_root(args.table_name)) if args.table_name else ""

    if args.table_name:
        if args.batch:
            # For batch, redirect output_root to the table root so all per-paper init/<slug>/,
            # chinese_reports/, review_v2/, batch_summary.json live under one tree.
            effective_output_root = table_root
        else:
            # For single-paper, args.output is the parent dir of <doi_slug>/. Prepend init/ segment
            # so the paper lands at <table_root>/init/<doi_slug>/ (matches batch layout).
            effective_output_root = str(Path(table_root) / "init")
    else:
        effective_output_root = args.output

    if args.recheck:
        run_recheck(args.recheck, effective_output_root, args.skip_refs, author_type=args.author_type)
    elif args.input:
        run_single(args.input, effective_output_root, args.skip_refs, author_type=args.author_type,
                   no_db=args.no_db, doi=args.doi, report_namespace=args.report_namespace,
                   table_root=table_root, table_name=args.table_name)
    elif args.batch:
        run_batch(args.batch, effective_output_root, args.skip_refs, max_workers=args.workers,
                  author_type=args.author_type, force=args.force, no_db=args.no_db,
                  report_namespace=args.report_namespace, table_root=table_root,
                  table_name=args.table_name, resume_existing_output=args.resume_existing_output,
                  summary_tag=args.summary_tag, isolate_complex=args.isolate_complex)


if __name__ == "__main__":
    main()
