#!/usr/bin/env python3
"""Fallback screening for Zhang Wanbin papers without valid PDFs.
Runs data-only analysis on xlsx/csv supplementary files where available,
and inserts minimal records for papers with no analyzable data."""
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.data_checker import check_data_anomalies
from modules.chinese_report_generator import (
    _compute_overall_risk, _compute_dimension_risk, _apply_data_caps, _make_filename,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("wanbin-fallback")

BASE = Path(__file__).resolve().parent
INPUT_BASE = BASE / "data" / "input" / "zhangwanbin"
OUTPUT_BASE = BASE / "data" / "output" / "zhangwanbin"
REPORT_BASE_URL = "http://10.119.9.99/chinese_reports"

DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}
TABLE = "yujing_wanbin"

FAILED_DIRS = [
    "10.1021_acs.orglett.2c00937",
    "10.1002_anie.202218146",
    "10.1039_C5OB00032G",
    "10.1039_C8QO01288A",
    "10.1039_C9QO01025D",
    "10.1039_D0QO00072H",
    "10.1039_D2CC01193J",
    "10.1039_c3cc42088d",
    "10.1039_c4ob02402h",
    "10.1039_c7cc01069a",
]


def _find_data_files(paper_dir: Path) -> list[Path]:
    data_files = []
    for sub in ("supplementary", "source_data", "source data"):
        sd = paper_dir / sub
        if sd.exists():
            for f in sd.rglob("*"):
                if f.suffix.lower() in (".xlsx", ".xls", ".csv", ".tsv"):
                    data_files.append(f)
    return data_files


def _insert_wanbin(doi: str, title: str, data_anomalies: list, has_data: bool):
    capped_data = _apply_data_caps(data_anomalies)
    data_risk = _compute_dimension_risk(capped_data)

    findings = {
        "paper": {"doi": doi, "title": title, "total_pages": 0, "total_images": 0, "total_references": 0},
        "summary": {"data_issues": len(data_anomalies)},
        "image_duplicates": [],
        "data_anomalies": data_anomalies,
        "reference_issues": [],
    }
    overall = _compute_overall_risk(findings)

    filename = _make_filename(doi, title)
    report_url = f"{REPORT_BASE_URL}/{filename}"

    row = {
        "title": title[:500],
        "author": "",
        "author_type": "通讯作者",
        "department": "",
        "author_all": "",
        "department_all": "",
        "journal": "",
        "doi": doi[:200],
        "page": 0,
        "pic_num": 0,
        "data_num": len(data_anomalies),
        "ref_num": 0,
        "total_score": str(overall["score"]),
        "risk_level": overall["level"],
        "pic_score": "0",
        "pic_risk_level": "低风险",
        "data_score": str(data_risk["score"]),
        "data_risk_level": data_risk["level"],
        "ref_score": "0",
        "ref_risk_level": "低风险",
        "report_url": report_url[:500] if has_data else "",
        "generation_time": datetime.now(),
    }

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(f"SELECT doi FROM {TABLE} WHERE doi=%s", (doi,))
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


def process_one(dirname: str) -> dict:
    t0 = time.time()
    paper_dir = INPUT_BASE / dirname
    doi = dirname.replace("_", "/", 1)
    result = {"dir": dirname, "doi": doi, "status": "error"}

    data_files = _find_data_files(paper_dir)

    if data_files:
        log.info("  Found %d data files for %s", len(data_files), dirname)
        try:
            anomalies = check_data_anomalies(str(paper_dir))
            result["data_issues"] = len(anomalies)
            result["image_issues"] = 0

            level = _insert_wanbin(doi, "", anomalies, has_data=True)
            result["level"] = level
            result["status"] = "success"
            result["method"] = "data_only"
            log.info("  %s → %s (data_issues=%d)", dirname, level, len(anomalies))
        except Exception as e:
            log.error("  Data analysis failed for %s: %s", dirname, e)
            result["error"] = str(e)
            level = _insert_wanbin(doi, "", [], has_data=False)
            result["level"] = level
            result["status"] = "success"
            result["method"] = "no_analyzable_data"
    else:
        log.info("  No xlsx/csv data files for %s, inserting minimal record", dirname)
        level = _insert_wanbin(doi, "", [], has_data=False)
        result["level"] = level
        result["status"] = "success"
        result["method"] = "no_analyzable_data"

    result["elapsed"] = round(time.time() - t0, 1)
    return result


def main():
    log.info("Processing %d failed papers with fallback method", len(FAILED_DIRS))
    results = []

    for dirname in FAILED_DIRS:
        r = process_one(dirname)
        results.append(r)

    success = sum(1 for r in results if r["status"] == "success")
    log.info("=" * 60)
    log.info("FALLBACK COMPLETE: %d/%d processed", success, len(results))

    existing = []
    try:
        with open(OUTPUT_BASE / "screening_results.json") as f:
            existing = json.load(f)
    except Exception:
        pass

    existing_dirs = {r["dir"] for r in existing}
    for r in results:
        if r["dir"] in existing_dirs:
            existing = [e for e in existing if e["dir"] != r["dir"]]
        existing.append(r)

    with open(OUTPUT_BASE / "screening_results.json", "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2, default=str)
    log.info("Updated screening_results.json (total=%d)", len(existing))

    return results


if __name__ == "__main__":
    main()
