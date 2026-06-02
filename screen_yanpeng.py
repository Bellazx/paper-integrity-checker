#!/usr/bin/env python3
"""Run initial screening on Yan Peng papers. Writes results to yujing_scholar table."""
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.pipeline import analyze_paper
from modules.chinese_report_generator import (
    _compute_overall_risk, _compute_dimension_risk, _apply_data_caps, _make_filename,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("yanpeng-screen")

BASE = Path(__file__).resolve().parent
INPUT_BASE = BASE / "data" / "input" / "yanpeng"
OUTPUT_BASE = BASE / "data" / "output" / "yanpeng"
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}
TABLE = "yujing_scholar"
SCHOLAR = "yanpeng"
REPORT_BASE_URL = "http://10.119.9.99/chinese_reports"


def get_paper_dirs():
    dirs = []
    for d in sorted(INPUT_BASE.iterdir()):
        if d.is_dir() and any(d.glob("*.pdf")):
            dirs.append(d)
    return dirs


def _insert_scholar(findings: dict):
    paper = findings.get("paper", {})
    summary = findings.get("summary", {})

    image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)

    doi = (paper.get("doi") or "")[:200]
    title = (paper.get("title") or "")[:500]
    filename = _make_filename(doi, title)
    report_url = f"{REPORT_BASE_URL}/{filename}"

    row = {
        "title": title,
        "author": (paper.get("author") or "")[:500],
        "author_type": "通讯作者",
        "department": "",
        "author_all": "",
        "department_all": "",
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
        "scholar": SCHOLAR,
    }

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    cursor.execute(f"SELECT doi FROM {TABLE} WHERE doi=%s AND scholar=%s", (doi, SCHOLAR))
    exists = cursor.fetchone()

    if exists:
        sql = f"""UPDATE {TABLE} SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s,
            data_num=%(data_num)s, ref_num=%(ref_num)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s
        WHERE doi=%(doi)s AND scholar=%(scholar)s"""
    else:
        sql = f"""INSERT INTO {TABLE} (
            title, author, author_type, department, author_all, department_all,
            journal, doi, page, pic_num, data_num, ref_num,
            total_score, risk_level, pic_score, pic_risk_level,
            data_score, data_risk_level, ref_score, ref_risk_level,
            report_url, generation_time, scholar
        ) VALUES (
            %(title)s, %(author)s, %(author_type)s, %(department)s, %(author_all)s, %(department_all)s,
            %(journal)s, %(doi)s, %(page)s, %(pic_num)s, %(data_num)s, %(ref_num)s,
            %(total_score)s, %(risk_level)s, %(pic_score)s, %(pic_risk_level)s,
            %(data_score)s, %(data_risk_level)s, %(ref_score)s, %(ref_risk_level)s,
            %(report_url)s, %(generation_time)s, %(scholar)s
        )"""

    cursor.execute(sql, row)
    conn.commit()
    conn.close()
    return overall["level"]


def process_one(paper_dir: Path) -> dict:
    t0 = time.time()
    dirname = paper_dir.name
    out_dir = OUTPUT_BASE / dirname
    cn_dir = str(OUTPUT_BASE / "chinese_reports")

    result = {"dir": dirname, "status": "error"}

    try:
        findings = analyze_paper(
            str(paper_dir), str(out_dir),
            skip_refs=True,
            chinese_reports_dir=cn_dir,
            author_type="通讯作者",
        )

        doi = findings.get("paper", {}).get("doi", "")
        overall = _compute_overall_risk(findings)
        result["doi"] = doi
        result["score"] = overall["score"]
        result["level"] = overall["level"]
        result["data_issues"] = len(findings.get("data_anomalies", []))
        result["image_issues"] = len(findings.get("image_duplicates", []))

        risk_level = _insert_scholar(findings)
        result["status"] = "success"
        result["db_level"] = risk_level

    except Exception as e:
        log.error("Failed %s: %s", dirname, e)
        result["error"] = str(e)

    result["elapsed"] = round(time.time() - t0, 1)
    return result


def main():
    paper_dirs = get_paper_dirs()
    log.info("Found %d papers to screen", len(paper_dirs))

    results = []
    high_risk = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(process_one, d): d for d in paper_dirs}
        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            results.append(r)
            status = "OK" if r["status"] == "success" else "FAIL"
            level = r.get("level", "?")
            score = r.get("score", "?")
            elapsed = r.get("elapsed", 0)
            log.info("[%d/%d] [%s] %s → %s (score=%s, %.0fs)",
                     i, len(paper_dirs), status, r["dir"][:40], level, score, elapsed)
            if level == "高风险":
                high_risk.append(r)

    success = sum(1 for r in results if r["status"] == "success")
    failed = sum(1 for r in results if r["status"] == "error")

    log.info("=" * 60)
    log.info("SCREENING COMPLETE")
    log.info("=" * 60)
    log.info("Total: %d, Success: %d, Failed: %d", len(results), success, failed)
    log.info("High risk: %d, Low risk: %d", len(high_risk), success - len(high_risk))

    if high_risk:
        log.info("\n--- High Risk Papers (%d) ---", len(high_risk))
        for r in sorted(high_risk, key=lambda x: x.get("score", 0), reverse=True):
            log.info("  %s (score=%s, data=%s, image=%s)",
                     r.get("doi", r["dir"]), r["score"], r.get("data_issues", 0), r.get("image_issues", 0))

    output_path = OUTPUT_BASE / "screening_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    log.info("Results saved to %s", output_path)

    return high_risk


if __name__ == "__main__":
    high_risk = main()
    sys.exit(0 if not high_risk else len(high_risk))
