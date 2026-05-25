#!/usr/bin/env python3
"""Repair metadata for papers with bad titles, empty journals, or missing authors.

Covers three types of bad titles:
  1. DOI used as title: "10.3389_fphar.2023.1085509"
  2. Frontiers PDF filename: "fphar-2021-714390 1..12"
  3. Truncated Frontiers browser tab: "Frontiers _ Luteal-Phase..."

Data sources (in priority order):
  1. Batch Excel file from data/input/<batch>/  (if available)
  2. CrossRef API
  3. PDF text re-extraction (legacy fallback)

Updates yujing_quanliang table only.
"""
import json
import re
import sys
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymssql
from core.pipeline import extract_title_from_text
from utils.crossref import is_bad_title, enrich_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

SJTU_KEYWORDS = [
    "Shanghai Jiao Tong", "Shanghai Jiaotong", "Jiaotong University",
    "上海交通", "交通大学", "SJTU",
]

INPUT_BASE = Path("/opt/paper-integrity-checker/data/input")
OUTPUT_BASE = Path("/opt/paper-integrity-checker/data/output")


def load_excel_metadata(excel_path: str) -> dict:
    """Load DOI -> metadata mapping from batch Excel file."""
    import openpyxl
    wb = openpyxl.load_workbook(excel_path, read_only=True)
    ws = wb.active
    data = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        doi = str(row[5]).strip() if row[5] else ""
        if doi:
            data[doi] = {
                "title": str(row[7]).strip() if row[7] else "",
                "source": str(row[13]).strip() if row[13] else "",
            }
    wb.close()
    return data


def find_batch_excel() -> str | None:
    """Auto-discover the most recent batch Excel file."""
    for batch_dir in sorted(INPUT_BASE.iterdir(), reverse=True):
        if batch_dir.is_dir() and batch_dir.name.startswith("20"):
            for f in batch_dir.glob("*.xlsx"):
                return str(f)
    return None


def find_report_json(doi: str) -> Path | None:
    for slug in [doi.replace("/", "_"), doi.replace("/", "__")]:
        p = OUTPUT_BASE / slug / "report.json"
        if p.exists():
            return p
    return None


def find_pdf(doi: str) -> Path | None:
    slug = doi.replace("/", "_")
    for batch_dir in INPUT_BASE.iterdir():
        d = batch_dir / slug
        if d.exists():
            pdfs = list(d.glob("*.pdf")) + list(d.glob("*.PDF"))
            if pdfs:
                return pdfs[0]
    return None


def find_sjtu_authors(authors_full: list, affiliations: list) -> tuple[list, list]:
    sjtu_depts = []
    sjtu_aff_indices = set()
    for i, aff in enumerate(affiliations):
        if any(kw in aff for kw in SJTU_KEYWORDS):
            sjtu_aff_indices.add(i + 1)
            clean = re.sub(r'^\d+\.\s*', '', aff.strip())
            sjtu_depts.append(clean)

    sjtu_authors = []
    if sjtu_aff_indices and authors_full:
        for author_name in authors_full:
            nums = re.findall(r'(\d+)', author_name)
            if nums:
                indices = {int(x) for x in nums}
                if indices & sjtu_aff_indices:
                    clean_name = re.sub(r'\d+[,\s]*', '', author_name).strip().rstrip(',').strip('*')
                    if clean_name:
                        sjtu_authors.append(clean_name)
        if not sjtu_authors:
            sjtu_authors = [re.sub(r'[\d,\s*]+$', '', authors_full[0]).strip()]

    return sjtu_authors, sjtu_depts


def main():
    excel_path = find_batch_excel()
    excel_data = {}
    if excel_path:
        log.info("Loading Excel: %s", excel_path)
        excel_data = load_excel_metadata(excel_path)
        log.info("Excel entries: %d", len(excel_data))

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT doi, title, author, department, journal FROM yujing_quanliang
        WHERE title LIKE '10.%%'
           OR title LIKE 'f____%%-%%'
           OR title LIKE '%%1..%%'
           OR title LIKE 'Frontiers%%'
           OR author IS NULL OR author = ''
           OR journal IS NULL OR journal = ''
           OR LEN(journal) > 100
    """)
    rows = cursor.fetchall()
    log.info("Found %d records to check", len(rows))

    stats = {"title": 0, "journal": 0, "author": 0, "dept": 0, "errors": 0}

    for doi, old_title, old_author, old_dept, old_journal in rows:
        updates = {}
        need_title = is_bad_title(old_title or "")
        need_journal = not old_journal or len(old_journal) > 100
        need_author = not old_author

        if not (need_title or need_journal or need_author):
            continue

        # Source 1: Excel
        if doi in excel_data:
            e = excel_data[doi]
            if need_title and e["title"]:
                updates["title"] = e["title"][:500]
            if need_journal and e["source"]:
                updates["journal"] = e["source"][:200]

        # Source 2: CrossRef (only if Excel didn't fully solve it)
        if ("title" not in updates and need_title) or ("journal" not in updates and need_journal):
            cr = enrich_metadata(doi)
            time.sleep(0.15)
            if cr:
                if "title" not in updates and need_title and cr["title"]:
                    updates["title"] = cr["title"][:500]
                if "journal" not in updates and need_journal and cr["journal"]:
                    updates["journal"] = cr["journal"][:200]

        # Source 3: PDF re-extraction (title only, legacy fallback)
        if "title" not in updates and need_title:
            pdf_path = find_pdf(doi)
            if pdf_path:
                extracted = extract_title_from_text(str(pdf_path))
                if extracted and len(extracted) > 10 and not is_bad_title(extracted):
                    updates["title"] = extracted[:500]

        # Fix author/dept from report.json
        if need_author:
            report_path = find_report_json(doi)
            if report_path:
                with open(report_path) as f:
                    paper = json.load(f).get("paper", {})
                authors_full = paper.get("authors_full", [])
                affiliations = paper.get("affiliations", [])
                if authors_full and affiliations:
                    sjtu_authors, sjtu_depts = find_sjtu_authors(authors_full, affiliations)
                    if sjtu_authors:
                        updates["author"] = ", ".join(sjtu_authors)[:500]
                    if sjtu_depts and not old_dept:
                        updates["department"] = "; ".join(sjtu_depts)[:500]

        if not updates:
            continue

        set_parts = []
        params = {"doi": doi}
        for field, value in updates.items():
            set_parts.append(f"{field}=%({field})s")
            params[field] = value

        try:
            sql = f"UPDATE yujing_quanliang SET {', '.join(set_parts)} WHERE doi=%(doi)s"
            cursor.execute(sql, params)
            for field in updates:
                stats[field] = stats.get(field, 0) + 1
        except Exception as e:
            log.error("[ERR] %s: %s", doi, e)
            stats["errors"] += 1

    conn.commit()
    conn.close()

    log.info("Done. Fixed: title=%d, journal=%d, author=%d, dept=%d, errors=%d",
             stats["title"], stats["journal"], stats["author"], stats.get("dept", 0), stats["errors"])


if __name__ == "__main__":
    main()
