#!/usr/bin/env python3
"""Export reviewed yujing_scholar rows to the recruitment-review workbook format.

The source table has no dedicated original-school column.  This exporter therefore
derives it from the affiliations stored with each paper, using the first recognizable
university/college/institute name.  The review columns always come from review_result
and review_time, never from the initial screening fields.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

import openpyxl
import pymssql
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

HEADERS = [
    "检测用途", "学者名称", "需求学院", "原学校名称", "论文标题", "DOI",
    "审核结果", "审核时间",
]

# A few affiliations are written as a university plus a campus/city separated by a
# comma.  Keep the complete institution name where it is unambiguous.
KNOWN_SCHOOLS = (
    "University of California, San Diego",
    "University of California, Berkeley",
    "University of California",
    "University of Nottingham",
    "Shanghai Jiao Tong University",
    "Shanghai Ocean University",
    "Zhejiang Ocean University",
    "University of Melbourne",
    "Carnegie Mellon University",
    "Mohamed bin Zayed University of Artificial Intelligence",
    "Shanghai Institute of Organic Chemistry",
    "Shanghai Institute of Materia Medica",
    "Xi'an Jiaotong-Liverpool University",
)

# The same scholar may have papers with different affiliation ordering.  These
# mappings reflect the institution consistently associated with the scholar in the
# supplied paper batch and avoid picking a coauthor's institution from a first slot.
SCHOLAR_SCHOOL = {
    "Phillip": "University of Nottingham",
    "黄碧薇": "University of California, San Diego",
    "戴大伟": "University of California, Berkeley",
    "韩有承": "Shanghai Jiao Tong University",
    "林欣": "Shanghai Institute of Satellite Engineering",
    "邵伟增": "Shanghai Ocean University",
    "方懿": "Shanghai Jiao Tong University",
    "卢彬": "Shanghai Jiao Tong University",
    "卓越": "Shanghai Jiao Tong University",
}


def _clean_affiliations(value: str | None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if text.lower() in {"none", "null", "nan"}:
        return ""
    return text


def _school_from_affiliation(affiliation: str | None) -> str:
    text = _clean_affiliations(affiliation)
    if not text:
        return ""
    for school in KNOWN_SCHOOLS:
        if school.lower() in text.lower():
            return school

    # Prefer the institution-like phrase over a department/laboratory prefix.
    patterns = [
        r"(University of [^;,]+)",
        r"([^;,]+ University)",
        r"([^;,]+大学)",
        r"([^;,]+学院)",
        r"([^;,]+Institute(?: of [^;,]+)?)",
        r"([^;,]+College)",
        r"([^;,]+School of [^;,]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            candidate = re.sub(r"^\s*\d+\.?\s*", "", match.group(1)).strip(" ,")
            if candidate:
                return candidate
    return text.split(";", 1)[0].strip()


def _original_school(scholar: str | None, department_all: str | None) -> str:
    scholar = str(scholar or "").strip()
    if scholar in SCHOLAR_SCHOOL:
        return SCHOLAR_SCHOOL[scholar]
    first_affiliation = _clean_affiliations(department_all).split(";", 1)[0]
    return _school_from_affiliation(first_affiliation)


def fetch_rows(cutoff: datetime) -> list[dict]:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor(as_dict=True)
        cursor.execute(
            """
            SELECT scholar, college, department_all, title, doi,
                   review_result, review_time, id
            FROM yujing_scholar
            WHERE review_time >= %s
            ORDER BY review_time ASC, id ASC
            """,
            (cutoff,),
        )
        return cursor.fetchall()
    finally:
        conn.close()


def infer_scholar_schools(rows: list[dict]) -> dict[str, str]:
    """Choose one stable original-school value for each scholar.

    The screenshot format repeats the original school for a scholar.  Where the
    table contains several papers with different coauthor ordering, use the
    explicit scholar mapping first, then prefer a Shanghai Jiao Tong affiliation
    when present, and finally use the most frequent extracted institution.
    """
    by_scholar: dict[str, list[dict]] = {}
    for row in rows:
        by_scholar.setdefault(str(row.get("scholar") or "").strip(), []).append(row)

    result: dict[str, str] = {}
    for scholar, scholar_rows in by_scholar.items():
        if scholar in SCHOLAR_SCHOOL:
            result[scholar] = SCHOLAR_SCHOOL[scholar]
            continue
        affiliations = [_clean_affiliations(r.get("department_all")) for r in scholar_rows]
        if any("Shanghai Jiao Tong University" in a or "上海交通大学" in a for a in affiliations):
            result[scholar] = "Shanghai Jiao Tong University"
            continue
        candidates = [_school_from_affiliation(a.split(";", 1)[0]) for a in affiliations]
        candidates = [c for c in candidates if c]
        result[scholar] = max(set(candidates), key=candidates.count) if candidates else ""
    return result


def export(rows: list[dict], output: Path, purpose: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "审核结果"
    ws.append(HEADERS)
    school_by_scholar = infer_scholar_schools(rows)

    for row in rows:
        review_time = row.get("review_time")
        ws.append([
            purpose,
            row.get("scholar") or "",
            row.get("college") or "",
            school_by_scholar.get(str(row.get("scholar") or "").strip())
            or _original_school(row.get("scholar"), row.get("department_all")),
            row.get("title") or "",
            row.get("doi") or "",
            row.get("review_result") or "",
            review_time,
        ])

    # Readable layout matching the supplied workbook screenshot.
    header_fill = PatternFill("solid", fgColor="D9EAD3")
    header_font = Font(name="Microsoft YaHei", size=11, bold=True, color="222222")
    body_font = Font(name="Microsoft YaHei", size=10)
    thin_gray = Side(style="thin", color="D9D9D9")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=Side(style="medium", color="70AD47"))
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.font = body_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            cell.border = Border(bottom=thin_gray)
        row_cells[-1].number_format = "yyyy-mm-dd hh:mm:ss"

    widths = [12, 14, 20, 32, 70, 42, 12, 22]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.row_dimensions[1].height = 28
    for idx in range(2, ws.max_row + 1):
        ws.row_dimensions[idx].height = 34

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{ws.max_row}"
    ws.sheet_view.showGridLines = False
    table = Table(displayName="YujingScholarReview", ref=f"A1:H{ws.max_row}")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2", showFirstColumn=False,
        showLastColumn=False, showRowStripes=True, showColumnStripes=False,
    )
    ws.add_table(table)

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", default="2026-09-01", help="Inclusive local date")
    parser.add_argument("--purpose", default="人才引进")
    parser.add_argument(
        "--output", type=Path,
        default=Path("data/output/yujing_scholar_review_after_2026-09-01.xlsx"),
    )
    args = parser.parse_args()
    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d")
    rows = fetch_rows(cutoff)
    export(rows, args.output, args.purpose)
    print(f"exported {len(rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
