#!/usr/bin/env python3
"""Backfill the reference-extraction fix across already-processed papers.

Background
----------
Publisher PDFs print a running header `Article https://doi.org/<this paper's DOI>`
on every page. PDF text extraction interleaved that header into the reference
section, so the old `_extract_references` picked up the PAPER'S OWN DOI as a
citation's DOI. Those citations were then "verified" against the paper itself and
flagged as high-severity `title_mismatch` (paper title vs. an unrelated cited work).
The `[:20000]` window also overshot the bibliography into trailing methods/equation
text, mis-parsing it as extra references (one fragment even text-matched an unrelated
CrossRef DOI -> a second kind of false high).

The fix lives in modules/reference_checker.py (header/footer scrub + self-DOI guard
+ monotonic-numbering cutoff). This script re-runs ONLY reference checking for the
affected papers, reuses their existing image/data findings, recomputes scores,
regenerates the Chinese PDF, and updates the DB.

Selection
---------
A paper is "affected" if any stored reference issue is a self-reference, i.e. its
details.doi (or details.matched_doi) equals the paper's own DOI. This is exactly the
running-header artifact. (206 papers at time of writing.)

Usage
-----
    python backfill_ref_extraction_fix.py --dry-run              # report diffs, write nothing*
    python backfill_ref_extraction_fix.py --dry-run --limit 5    # first 5 only
    python backfill_ref_extraction_fix.py                        # full backfill (report.json + PDF + DB)
    python backfill_ref_extraction_fix.py --workers 6 --limit 10

    * dry-run still re-runs CrossRef and caches the recomputed reference issues under
      data/backfill_ref_cache/ so a subsequent real run reuses them (no double CrossRef).
      Pass --refresh to ignore the cache.

The DB write path (utils.db.insert_findings) independently honors the 2054 protected
snapshot, so protected DOIs keep their frozen DB row; their report.json + PDF are still
corrected. Those are reported as db_skipped_protected.
"""
import argparse
import copy
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.reference_checker import check_references
from modules.chinese_report_generator import (
    generate_chinese_pdf,
    _compute_dimension_risk,
    _compute_overall_risk,
)
from utils.db import insert_findings
from utils.pdf_utils import extract_full_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet the per-reference CrossRef chatter; we only want batch-level progress.
logging.getLogger("modules.reference_checker").setLevel(logging.WARNING)
log = logging.getLogger("backfill-ref")

OUTPUT_DIR = ROOT / "data" / "output"
CN_REPORTS_DIR = str(OUTPUT_DIR / "chinese_reports")
CACHE_DIR = ROOT / "data" / "backfill_ref_cache"
PROTECTED_SNAPSHOT = ROOT / "data" / "protected_snapshot_2054.json"


def _load_protected() -> set:
    if PROTECTED_SNAPSHOT.exists():
        try:
            return set(json.load(open(PROTECTED_SNAPSHOT)).keys())
        except Exception as e:
            log.warning("Could not read protected snapshot: %s", e)
    return set()


def _self_doi_issues(findings: dict) -> list:
    """Return reference issues that point at the paper's own DOI (the artifact)."""
    pdoi = (findings.get("paper") or {}).get("doi")
    if not pdoi:
        return []
    pdoi_l = pdoi.lower()
    out = []
    for r in findings.get("reference_issues", []):
        det = r.get("details") or {}
        ref_doi = (det.get("doi") or det.get("matched_doi") or "").lower()
        if ref_doi and ref_doi == pdoi_l:
            out.append(r)
    return out


def discover_affected() -> list:
    """Scan output/*/report.json for papers carrying the self-DOI artifact."""
    affected = []
    for rj in OUTPUT_DIR.glob("*/report.json"):
        try:
            findings = json.load(open(rj))
        except Exception:
            continue
        if _self_doi_issues(findings):
            affected.append(rj)
    affected.sort()
    return affected


def _recompute_summary(findings: dict) -> dict:
    """Recompute findings['summary'] the same way core/pipeline.py does."""
    image = findings.get("image_duplicates", [])
    data = findings.get("data_anomalies", [])
    ref = findings.get("reference_issues", [])
    splice = findings.get("image_splicing", [])
    allr = image + data + ref
    return {
        "total_issues": len(image) + len(data) + len(ref) + len(splice),
        "image_issues": len(image),
        "image_splicing_suspects": len(splice),
        "data_issues": len(data),
        "reference_issues": len(ref),
        "high_severity": sum(1 for r in allr if r.get("severity") == "high"),
        # splice findings are always severity 'medium' (conservative pre-screen)
        "medium_severity": sum(1 for r in allr if r.get("severity") == "medium") + len(splice),
        "low_severity": sum(1 for r in allr if r.get("severity") == "low"),
    }


def _risk_snapshot(findings: dict) -> dict:
    """Dimension + overall risk for diff reporting."""
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)
    return {
        "ref_high": ref_risk["high"],
        "ref_score": ref_risk["score"],
        "ref_level": ref_risk["level"],
        "overall_level": overall["level"],
        "overall_score": overall["score"],
    }


def _cache_path(doi: str) -> Path:
    return CACHE_DIR / (doi.replace("/", "_") + ".json")


def _new_ref_issues(findings: dict, pdf_path: str, use_cache: bool) -> list:
    """Re-run check_references with the fixed code (or read from cache)."""
    doi = (findings.get("paper") or {}).get("doi") or ""
    cp = _cache_path(doi) if doi else None
    if use_cache and cp and cp.exists():
        try:
            return json.load(open(cp))
        except Exception:
            pass
    issues = check_references(pdf_path, doi)
    if cp:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            json.dump(issues, open(cp, "w"), ensure_ascii=False)
        except Exception as e:
            log.warning("Cache write failed for %s: %s", doi, e)
    return issues


def process_one(report_json: Path, dry_run: bool, use_cache: bool, protected: set) -> dict:
    t0 = time.time()
    try:
        findings = json.load(open(report_json))
    except Exception as e:
        return {"dir": report_json.parent.name, "status": "error", "error": f"load report.json: {e}"}

    paper = findings.get("paper") or {}
    doi = paper.get("doi") or ""
    pdf_path = paper.get("filepath") or ""
    before = _risk_snapshot(findings)
    n_bogus = len(_self_doi_issues(findings))

    if not pdf_path or not Path(pdf_path).exists():
        return {"dir": report_json.parent.name, "doi": doi, "status": "skip",
                "reason": "input PDF not found", "bogus_removed": n_bogus}

    # Re-run reference checking with the fixed extractor (cached across dry/real runs).
    try:
        new_refs = _new_ref_issues(findings, pdf_path, use_cache)
    except Exception as e:
        return {"dir": report_json.parent.name, "doi": doi, "status": "error",
                "error": f"check_references: {e}"}

    candidate = copy.deepcopy(findings)
    candidate["reference_issues"] = new_refs
    after = _risk_snapshot(candidate)

    result = {
        "dir": report_json.parent.name,
        "doi": doi,
        "bogus_removed": n_bogus,
        "ref_issues_before": len(findings.get("reference_issues", [])),
        "ref_issues_after": len(new_refs),
        "ref_high_before": before["ref_high"],
        "ref_high_after": after["ref_high"],
        "ref_level": f'{before["ref_level"]}->{after["ref_level"]}' if before["ref_level"] != after["ref_level"] else before["ref_level"],
        "overall_level": f'{before["overall_level"]}->{after["overall_level"]}' if before["overall_level"] != after["overall_level"] else before["overall_level"],
        "overall_changed": before["overall_level"] != after["overall_level"],
        "elapsed": round(time.time() - t0, 1),
    }

    if dry_run:
        result["status"] = "dry-run"
        return result

    # --- apply ---
    findings["reference_issues"] = new_refs
    findings["summary"] = _recompute_summary(findings)

    # Regenerate the Chinese PDF (LLM analysis + render). Keep existing author/affiliation
    # metadata if the regen returns empty (mirrors rescore_and_regen_pdf.py).
    first_pages_text = ""
    try:
        full = extract_full_text(pdf_path)
        first_pages_text = full[:3000] if full else ""
    except Exception:
        pass
    try:
        _, metadata = generate_chinese_pdf(findings, CN_REPORTS_DIR, first_pages_text)
        if metadata.get("authors_full"):
            findings["paper"]["authors_full"] = metadata["authors_full"]
        if metadata.get("affiliations"):
            findings["paper"]["affiliations"] = metadata["affiliations"]
    except Exception as e:
        return {**result, "status": "error", "error": f"generate_chinese_pdf: {e}"}

    # Update DB. insert_findings honors the 2054 snapshot internally.
    db_status = "updated"
    if doi in protected:
        db_status = "skipped_protected"
    else:
        input_dir = Path(pdf_path).parent
        html_candidate = input_dir / "article.html"
        if not html_candidate.exists():
            html_candidate = input_dir / "html" / "article.html"
        html_path = str(html_candidate) if html_candidate.exists() else None
        try:
            insert_findings(findings, chinese_reports_dir=CN_REPORTS_DIR, html_path=html_path)
        except Exception as e:
            db_status = f"db_error: {e}"

    # Write report.json LAST so an interruption leaves the artifact re-selectable.
    try:
        json.dump(findings, open(report_json, "w"), ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        return {**result, "status": "error", "error": f"write report.json: {e}"}

    result["status"] = "ok"
    result["db"] = db_status
    result["elapsed"] = round(time.time() - t0, 1)
    return result


def main():
    ap = argparse.ArgumentParser(description="Backfill reference-extraction fix across affected papers")
    ap.add_argument("--dry-run", action="store_true", help="Report diffs and cache new ref issues; write no report.json/PDF/DB")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N affected papers")
    ap.add_argument("--workers", type=int, default=6, help="Concurrent workers (default 6)")
    ap.add_argument("--refresh", action="store_true", help="Ignore the CrossRef cache and re-query")
    args = ap.parse_args()

    protected = _load_protected()
    log.info("Discovering affected papers under %s ...", OUTPUT_DIR)
    affected = discover_affected()
    log.info("Found %d affected papers (%d of them in the 2054 protected snapshot)",
             len(affected), sum(1 for p in affected
                                if json.load(open(p)).get("paper", {}).get("doi", "") in protected))
    if args.limit:
        affected = affected[:args.limit]
        log.info("Limited to first %d", len(affected))
    if not affected:
        log.info("Nothing to do.")
        return

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    log.info("Mode: %s | workers=%d | cache=%s", mode, args.workers, "off" if args.refresh else "on")

    results = []
    lock = threading.Lock()
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, p, args.dry_run, not args.refresh, protected): p for p in affected}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            with lock:
                results.append(r)
            flag = "  <-- OVERALL CHANGED" if r.get("overall_changed") else ""
            log.info("[%d/%d] [%s] %s | ref %s->%s (high %s->%s) | refdim %s | overall %s%s",
                     i, len(affected), r.get("status", "?").upper(), r.get("doi", r.get("dir", "?")),
                     r.get("ref_issues_before", "?"), r.get("ref_issues_after", "?"),
                     r.get("ref_high_before", "?"), r.get("ref_high_after", "?"),
                     r.get("ref_level", "?"), r.get("overall_level", "?"), flag)

    elapsed = time.time() - t_start
    ok = sum(1 for r in results if r["status"] == "ok")
    dry = sum(1 for r in results if r["status"] == "dry-run")
    err = sum(1 for r in results if r["status"] == "error")
    skip = sum(1 for r in results if r["status"] == "skip")
    overall_changed = sum(1 for r in results if r.get("overall_changed"))
    refdim_changed = sum(1 for r in results if "->" in str(r.get("ref_level", "")))
    db_protected = sum(1 for r in results if r.get("db") == "skipped_protected")
    total_bogus = sum(r.get("bogus_removed", 0) for r in results)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {
        "mode": mode, "timestamp": stamp, "elapsed_seconds": round(elapsed, 1),
        "papers": len(affected),
        "ok": ok, "dry_run": dry, "errors": err, "skipped": skip,
        "self_doi_issues_removed_total": total_bogus,
        "reference_dimension_level_changed": refdim_changed,
        "overall_level_changed": overall_changed,
        "db_skipped_protected": db_protected,
        "results": sorted(results, key=lambda r: r.get("doi", "")),
    }
    summary_path = ROOT / "data" / f"backfill_ref_fix_{mode.lower()}_{stamp}.json"
    json.dump(summary, open(summary_path, "w"), ensure_ascii=False, indent=2)

    print(f"\n{'='*64}")
    print(f"Backfill {mode} complete in {elapsed:.0f}s")
    print(f"  papers processed         : {len(affected)}")
    print(f"  ok / dry / skip / error  : {ok} / {dry} / {skip} / {err}")
    print(f"  self-DOI issues removed  : {total_bogus}")
    print(f"  ref-dimension level chg  : {refdim_changed}")
    print(f"  overall level changed    : {overall_changed}")
    if not args.dry_run:
        print(f"  DB skipped (protected)   : {db_protected}")
    print(f"  summary written          : {summary_path}")
    print(f"{'='*64}\n")
    if err:
        for r in results:
            if r["status"] == "error":
                print(f"  ERROR {r.get('doi', r.get('dir'))}: {r.get('error')}")


if __name__ == "__main__":
    main()
