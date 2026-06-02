#!/usr/bin/env python3
"""
Batch-insert all zhangwanbin report.json files into yujing_wanbin table.
Replicates the logic of utils/db.py insert_findings().
"""

import json
import glob
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymssql
from modules.chinese_report_generator import (
    _compute_dimension_risk,
    _compute_overall_risk,
    _apply_data_caps,
    _make_filename,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

REPORT_BASE_URL = "http://10.119.9.99/chinese_reports"

SJTU_KEYWORDS = [
    "Shanghai Jiao Tong", "Shanghai Jiaotong", "Jiaotong University",
    "上海交通", "交通大学", "SJTU",
]

TABLE = "yujing_wanbin"


def extract_sjtu_authors(paper: dict) -> tuple[list, list, str]:
    """Extract SJTU-affiliated authors and departments from paper metadata."""
    sjtu_authors = []
    sjtu_depts = []
    sjtu_type = paper.get("sjtu_author_type", "")

    if paper.get("sjtu_authors"):
        sjtu_authors = paper["sjtu_authors"]
        sjtu_depts = paper.get("sjtu_departments", [])
        sjtu_type = paper.get("sjtu_author_type", "")
        # Also try to get depts from affiliations if empty
        if not sjtu_depts:
            for aff in paper.get("affiliations", []):
                if any(kw in aff for kw in SJTU_KEYWORDS):
                    clean = re.sub(r'^\d+\.\s*', '', aff.strip())
                    sjtu_depts.append(clean)
        return sjtu_authors, sjtu_depts, sjtu_type

    # Try to extract from affiliations
    affiliations = paper.get("affiliations", [])
    for aff in affiliations:
        if any(kw in aff for kw in SJTU_KEYWORDS):
            clean = re.sub(r'^\d+\.\s*', '', aff.strip())
            sjtu_depts.append(clean)

    # Extract authors by matching affiliation indices
    authors_full = paper.get("authors_full", [])
    if authors_full and affiliations:
        sjtu_aff_indices = set()
        for i, aff in enumerate(affiliations):
            if any(kw in aff for kw in SJTU_KEYWORDS):
                sjtu_aff_indices.add(i + 1)
        if sjtu_aff_indices:
            for author_name in authors_full:
                m = re.findall(r'(\d+)', author_name)
                if m:
                    indices = {int(x) for x in m}
                    if indices & sjtu_aff_indices:
                        clean_name = re.sub(r'\d+[,\s]*', '', author_name).strip().rstrip(',')
                        if clean_name:
                            sjtu_authors.append(clean_name)
            # If no authors matched by index, use the first author
            if not sjtu_authors:
                sjtu_authors = [authors_full[0].strip().rstrip(',')]

    return sjtu_authors, sjtu_depts, sjtu_type


def build_row(findings: dict) -> dict:
    """Build a database row dict from findings, mirroring insert_findings()."""
    paper = findings.get("paper", {})
    summary = findings.get("summary", {})

    image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)

    if overall["level"] == "高风险":
        for dim in overall.get("high_dimensions", []):
            if dim == "data" and data_risk["level"] != "高风险":
                data_risk["level"] = "高风险"
                data_risk["color"] = "#c00"
            elif dim == "image" and image_risk["level"] != "高风险":
                image_risk["level"] = "高风险"
                image_risk["color"] = "#c00"

    sjtu_authors, sjtu_depts, sjtu_type = extract_sjtu_authors(paper)

    authors_full = paper.get("authors_full", [])
    affiliations = paper.get("affiliations", [])

    doi = paper.get("doi", "") or ""
    title = paper.get("title", "") or ""
    filename = _make_filename(doi if doi else "unknown", title if title else "unknown")
    report_url = f"{REPORT_BASE_URL}/{filename}"

    row = {
        "title": title[:500],
        "author": ", ".join(sjtu_authors)[:500] if sjtu_authors else (paper.get("author") or "")[:500],
        "author_type": sjtu_type or "通讯作者",
        "department": "; ".join(sjtu_depts)[:500] if sjtu_depts else "",
        "author_all": ", ".join(authors_full) if authors_full else (paper.get("author") or ""),
        "department_all": "; ".join(affiliations) if affiliations else "",
        "journal": (paper.get("journal") or "")[:200],
        "doi": doi[:200],
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
    return row


def upsert_row(cursor, conn, row: dict, stats: dict):
    """UPSERT a row into yujing_wanbin. Use DOI as key, or title if DOI is empty."""
    doi = row["doi"]
    title = row["title"]

    if doi:
        # Check by DOI
        cursor.execute(f"SELECT id FROM {TABLE} WHERE doi=%(doi)s", {"doi": doi})
    else:
        # Check by title for Chinese papers without DOI
        cursor.execute(f"SELECT id FROM {TABLE} WHERE title=%(title)s", {"title": title})

    existing = cursor.fetchone()

    if existing:
        # UPDATE
        if doi:
            where_clause = "WHERE doi=%(doi)s"
        else:
            where_clause = "WHERE title=%(title)s"

        sql_update = f"""UPDATE {TABLE} SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
            journal=%(journal)s, doi=%(doi)s, page=%(page)s,
            pic_num=%(pic_num)s, pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_num=%(data_num)s, data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_num=%(ref_num)s, ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s
        {where_clause}"""
        cursor.execute(sql_update, row)
        conn.commit()
        stats["updated"] += 1
        log.info("Updated: DOI=%s title=%s risk=%s", doi or "(none)", title[:40], row["risk_level"])
    else:
        # INSERT
        sql_insert = f"""INSERT INTO {TABLE} (
            title, author, author_type, department, author_all, department_all,
            journal, doi, page,
            pic_num, pic_score, pic_risk_level,
            data_num, data_score, data_risk_level,
            ref_num, ref_score, ref_risk_level,
            total_score, risk_level,
            report_url, generation_time
        ) VALUES (
            %(title)s, %(author)s, %(author_type)s, %(department)s, %(author_all)s, %(department_all)s,
            %(journal)s, %(doi)s, %(page)s,
            %(pic_num)s, %(pic_score)s, %(pic_risk_level)s,
            %(data_num)s, %(data_score)s, %(data_risk_level)s,
            %(ref_num)s, %(ref_score)s, %(ref_risk_level)s,
            %(total_score)s, %(risk_level)s,
            %(report_url)s, %(generation_time)s
        )"""
        cursor.execute(sql_insert, row)
        conn.commit()
        stats["inserted"] += 1
        log.info("Inserted: DOI=%s title=%s risk=%s", doi or "(none)", title[:40], row["risk_level"])


def main():
    pattern = "/opt/paper-integrity-checker/data/output/zhangwanbin/*/report.json"
    report_files = sorted(glob.glob(pattern))
    log.info("Found %d report.json files", len(report_files))

    if not report_files:
        log.error("No report.json files found at %s", pattern)
        return

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    stats = {"inserted": 0, "updated": 0, "failed": 0, "total": len(report_files)}
    risk_counts = {}

    for filepath in report_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                findings = json.load(f)
            row = build_row(findings)
            risk_level = row["risk_level"]
            risk_counts[risk_level] = risk_counts.get(risk_level, 0) + 1
            upsert_row(cursor, conn, row, stats)
        except Exception as e:
            stats["failed"] += 1
            log.error("Failed to process %s: %s", filepath, e)
            try:
                conn.rollback()
            except Exception:
                pass

    conn.close()

    log.info("=" * 60)
    log.info("DONE: %d total, %d inserted, %d updated, %d failed",
             stats["total"], stats["inserted"], stats["updated"], stats["failed"])
    log.info("Risk distribution: %s", risk_counts)


if __name__ == "__main__":
    main()
