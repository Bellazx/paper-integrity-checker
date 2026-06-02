#!/usr/bin/env python3
"""Initial screening for the 4050-matched batch → writes to the NEW yujing_4050 table.

Path isolation
--------------
The same DOI may already have been processed for yujing_quanliang, whose report.json /
images / Chinese PDF live flat under data/output/. To avoid overwriting those, every
artifact for this run is namespaced by the target table name:

    report.json + images : data/output/yujing_4050/<doi_dir>/
    served Chinese PDF   : data/output/chinese_reports/yujing_4050/<doi>_title.pdf
    DB report_url        : http://10.119.9.99/chinese_reports/yujing_4050/<file>

nginx already serves data/output/chinese_reports/ via a trailing-slash `alias`, so the
yujing_4050/ subdir resolves automatically — no nginx change needed.

Input readiness
---------------
data/input/4050-matched/ is heterogeneous: some dirs have a PDF, some the Nature-crawl
article.html, and many have only a manifest.json (content not yet downloaded). We process
PDF + HTML dirs and SKIP (logging) the not-ready ones. DB writes are upserts keyed on doi,
so re-running after more content downloads will fill in the rest without duplicating.

Usage
-----
    python screen_4050.py --dry-run          # list what would be processed, write nothing
    python screen_4050.py --limit 5          # process first 5 ready papers
    python screen_4050.py --workers 8        # full run
"""
import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from core.pipeline import analyze_paper
from core.nature_adapter import is_nature_crawl, analyze_nature_paper
from modules.chinese_report_generator import (
    _compute_overall_risk, _compute_dimension_risk, _apply_data_caps, _make_filename,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("screen-4050")

TABLE = "yujing_4050"
INPUT_BASE = BASE / "data" / "input" / "4050-matched"
# Per-table isolation: artifacts namespaced under the table name.
OUTPUT_BASE = BASE / "data" / "output" / TABLE
CN_DIR = BASE / "data" / "output" / "chinese_reports" / TABLE
REPORT_BASE_URL = f"http://10.119.9.99/chinese_reports/{TABLE}"
RESULTS_PATH = BASE / "data" / "output" / f"{TABLE}_screening_results.json"

DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}


def _doi_from_dir(d: Path) -> str:
    doi_file = d / "doi.txt"
    if doi_file.exists():
        t = doi_file.read_text(encoding="utf-8").strip()
        if t:
            return t
    # fallback: dirname like "1000_10.1038__s41536-023-00341-z"
    name = d.name.split("_", 1)[1] if "_" in d.name else d.name
    return name.replace("__", "/")


def _is_ready(d: Path) -> bool:
    """A dir is processable if it has a PDF or the Nature-crawl article.html."""
    if any(d.glob("*.pdf")) or any(d.glob("*.PDF")):
        return True
    return (d / "article.html").exists() or (d / "html" / "article.html").exists()


def _has_pdf(d: Path) -> bool:
    return bool(list(d.glob("*.pdf")) + list(d.glob("*.PDF")))


def _analyze(paper_dir: str, out_dir: str, prefer_pdf: bool = False) -> dict:
    """Route HTML (Nature crawl) vs PDF dirs, mirroring main.py._analyze_one.
    Always runs reference verification (no skip_refs — needed for the ref gate).

    prefer_pdf: when the dir contains a downloaded main PDF, force the standard
    analyze_paper (PDF) pipeline instead of the HTML adapter — the HTML adapter ignores
    a main PDF (it only scans extended_data/), so this is required to actually use the
    fetched PDF (main-text figures + full-text refs). find_data_dir uses rglob, so the
    PDF path still picks up extended_data/ / source_data/ supplements.
    """
    if prefer_pdf and _has_pdf(Path(paper_dir)):
        return analyze_paper(paper_dir, out_dir, skip_refs=False, chinese_reports_dir=str(CN_DIR))
    if is_nature_crawl(paper_dir):
        return analyze_nature_paper(paper_dir, out_dir, skip_refs=False, chinese_reports_dir=str(CN_DIR))
    return analyze_paper(paper_dir, out_dir, skip_refs=False, chinese_reports_dir=str(CN_DIR))


def _upsert(findings: dict) -> str:
    paper = findings.get("paper", {})
    summary = findings.get("summary", {})

    image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)

    doi = (paper.get("doi") or "")[:200]
    title = (paper.get("title") or "")[:500]
    report_url = f"{REPORT_BASE_URL}/{_make_filename(doi, title)}"

    row = {
        "title": title,
        "author": (paper.get("author") or "")[:500],
        "author_type": "",
        "department": "",
        # Author/department backfill from report.json (never leave empty).
        "author_all": ", ".join(paper.get("authors_full", []))[:2000] if paper.get("authors_full") else "",
        "department_all": "; ".join(paper.get("affiliations", []))[:2000] if paper.get("affiliations") else "",
        "journal": (paper.get("journal") or "")[:200],
        "doi": doi,
        "page": paper.get("total_pages", 0),
        "pic_num": paper.get("total_images", 0),
        "data_num": summary.get("data_issues", 0),
        "ref_num": paper.get("total_references", 0),
        "total_score": str(overall["score"]),
        "risk_level": overall["level"],
        "pic_score": str(image_risk["score"]),
        "pic_risk_level": image_risk["level"],
        "data_score": str(data_risk["score"]),
        "data_risk_level": data_risk["level"],
        "ref_score": str(ref_risk["score"]),
        "ref_risk_level": ref_risk["level"],
        "report_url": report_url[:500],
        "generation_time": datetime.now(),
    }

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(f"SELECT doi FROM {TABLE} WHERE doi=%s", (doi,))
    exists = cursor.fetchone()
    if exists:
        sql = f"""UPDATE {TABLE} SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
            journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s,
            data_num=%(data_num)s, ref_num=%(ref_num)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s
        WHERE doi=%(doi)s"""
    else:
        sql = f"""INSERT INTO {TABLE} (
            title, author, author_type, department, author_all, department_all,
            journal, doi, page, pic_num, data_num, ref_num,
            total_score, risk_level, pic_score, pic_risk_level,
            data_score, data_risk_level, ref_score, ref_risk_level,
            report_url, generation_time
        ) VALUES (
            %(title)s, %(author)s, %(author_type)s, %(department)s, %(author_all)s, %(department_all)s,
            %(journal)s, %(doi)s, %(page)s, %(pic_num)s, %(data_num)s, %(ref_num)s,
            %(total_score)s, %(risk_level)s, %(pic_score)s, %(pic_risk_level)s,
            %(data_score)s, %(data_risk_level)s, %(ref_score)s, %(ref_risk_level)s,
            %(report_url)s, %(generation_time)s
        )"""
    cursor.execute(sql, row)
    conn.commit()
    conn.close()
    return overall["level"]


def process_one(paper_dir: Path, dry_run: bool, prefer_pdf: bool = False) -> dict:
    t0 = time.time()
    dirname = paper_dir.name
    result = {"dir": dirname, "doi": _doi_from_dir(paper_dir), "status": "error"}
    if dry_run:
        result["status"] = "would-process"
        result["route"] = "pdf" if (prefer_pdf and _has_pdf(paper_dir)) else (
            "html" if is_nature_crawl(str(paper_dir)) else "pdf")
        return result
    try:
        out_dir = OUTPUT_BASE / dirname
        findings = _analyze(str(paper_dir), str(out_dir), prefer_pdf=prefer_pdf)
        overall = _compute_overall_risk(findings)
        result.update({
            "doi": findings.get("paper", {}).get("doi", result["doi"]),
            "score": overall["score"], "level": overall["level"],
            "data_issues": len(findings.get("data_anomalies", [])),
            "image_issues": len(findings.get("image_duplicates", [])),
            "route": findings.get("paper", {}).get("source_format", "pdf"),
        })
        if not findings.get("pdf_generated", True):
            result["status"] = "error"; result["error"] = "PDF generation failed"
        else:
            result["db_level"] = _upsert(findings)
            result["status"] = "success"
    except Exception as e:
        log.error("Failed %s: %s", dirname, e)
        result["error"] = str(e)
    result["elapsed"] = round(time.time() - t0, 1)
    return result


def main():
    ap = argparse.ArgumentParser(description="Screen the 4050-matched batch into yujing_4050")
    ap.add_argument("--dry-run", action="store_true", help="List ready/skipped, write nothing")
    ap.add_argument("--limit", type=int, default=0, help="Process only first N ready papers")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--prefer-pdf", action="store_true",
                    help="If a dir has a downloaded main PDF, analyze via the PDF pipeline "
                         "(main-text figures + full-text refs) instead of the HTML adapter.")
    ap.add_argument("--only-pdf-dirs", action="store_true",
                    help="With --prefer-pdf: process ONLY dirs that contain a PDF (re-analysis pass "
                         "over freshly-downloaded PDFs; skips HTML-only papers).")
    ap.add_argument("--skip-done", action="store_true",
                    help="Resume: skip dirs whose DOI is already a row in yujing_4050 "
                         "(safe restart after a crash; upsert makes this idempotent anyway).")
    args = ap.parse_args()

    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    CN_DIR.mkdir(parents=True, exist_ok=True)

    all_dirs = sorted([d for d in INPUT_BASE.iterdir() if d.is_dir()])
    ready = [d for d in all_dirs if _is_ready(d)]
    skipped = [d for d in all_dirs if not _is_ready(d)]
    log.info("4050-matched: %d dirs total | %d ready | %d not-ready (skipped)",
             len(all_dirs), len(ready), len(skipped))
    if args.only_pdf_dirs:
        before = len(ready)
        ready = [d for d in ready if _has_pdf(d)]
        log.info("--only-pdf-dirs: %d of %d ready dirs have a PDF", len(ready), before)
    if args.skip_done:
        before = len(ready)
        done = set()
        try:
            conn = pymssql.connect(**DB_CONFIG)
            cur = conn.cursor()
            cur.execute(f"SELECT doi FROM {TABLE} WHERE doi IS NOT NULL")
            done = {row[0] for row in cur.fetchall()}
            conn.close()
        except Exception as e:
            log.warning("--skip-done: could not query existing DOIs (%s); processing all", e)
        ready = [d for d in ready if _doi_from_dir(d) not in done]
        log.info("--skip-done: %d already in %s, %d remaining", before - len(ready), TABLE, len(ready))
    if args.limit:
        ready = ready[:args.limit]
        log.info("Limited to first %d ready papers", len(ready))

    # Persist the skipped (not-ready) list so it can be re-crawled / re-run later.
    skip_path = BASE / "data" / "output" / f"{TABLE}_not_ready.json"
    json.dump([{"dir": d.name, "doi": _doi_from_dir(d)} for d in skipped],
              open(skip_path, "w"), ensure_ascii=False, indent=2)
    log.info("Not-ready list written to %s", skip_path)

    results, high_risk = [], []
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, d, args.dry_run, args.prefer_pdf): d for d in ready}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            status = {"success": "OK", "error": "FAIL", "would-process": "DRY"}.get(r["status"], "?")
            log.info("[%d/%d] [%s] %s → %s (score=%s, %.0fs)",
                     i, len(ready), status, r["dir"][:42], r.get("level", "?"),
                     r.get("score", "?"), r.get("elapsed", 0))
            if r.get("level") == "高风险":
                high_risk.append(r)

    ok = sum(1 for r in results if r["status"] == "success")
    fail = sum(1 for r in results if r["status"] == "error")
    log.info("=" * 60)
    log.info("4050 SCREENING COMPLETE in %.0fs", time.time() - t_start)
    log.info("ready=%d  ok=%d  fail=%d  high_risk=%d  not_ready_skipped=%d",
             len(ready), ok, fail, len(high_risk), len(skipped))
    json.dump(results, open(RESULTS_PATH, "w"), ensure_ascii=False, indent=2, default=str)
    log.info("Results saved to %s", RESULTS_PATH)
    return high_risk


if __name__ == "__main__":
    main()
