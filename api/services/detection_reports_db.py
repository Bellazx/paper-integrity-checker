"""Database writer for the detection_reports table (user-facing submit flow).

detection_reports has a different schema from the yujing_* tables — no per-dimension
score columns, a two-phase lifecycle (初审 → 复审) tracked by `status`, and a logical
key of (submission_no, fold_name) rather than `doi`. submission_no == file_id for both
single and batch; in batch, multiple papers share one file_id and are disambiguated by
fold_name (the paper's source folder name). Single-paper submissions store fold_name as
NULL.

This module is the ONLY place that writes detection_reports. It never touches yujing_*.
Bibliographic metadata (title/author/SJTU dept resolution) is built by the shared
utils.db.build_paper_metadata so the SJTU-author backfill logic lives in one place.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pymssql

from api.config import DB_CONFIG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

log = logging.getLogger(__name__)

TABLE = "detection_reports"

# Column widths per the detection_reports DDL. author_all/department_all are NVARCHAR(MAX)
# and are not truncated.
_WIDTHS = {
    "title": 500,
    "doi": 200,
    "author": 200,
    "author_type": 200,
    "department": 500,
    "journal": 200,
    "submission_no": 200,
    "task_id": 100,
    "fold_name": 100,
    "chushen_result": 20,
    "chushen_report_url": 500,
    "review_result": 20,
    "review_report_url": 500,
}


def _trunc(value, width: int) -> str:
    return (value or "")[:width]


def _norm_fold(fold_name: str | None) -> str | None:
    """Empty fold_name (single-paper submissions) is stored as SQL NULL."""
    if fold_name is None:
        return None
    fold_name = fold_name.strip()
    return fold_name[: _WIDTHS["fold_name"]] if fold_name else None


def _key_clause(fold_name: str | None) -> str:
    """WHERE clause matching the (submission_no, fold_name) logical key, treating an
    empty/NULL fold_name with IS NULL so single-paper rows match correctly."""
    if fold_name is None:
        return "submission_no=%(submission_no)s AND fold_name IS NULL"
    return "submission_no=%(submission_no)s AND fold_name=%(fold_name)s"


def upsert_chushen(
    submission_no: str,
    fold_name: str | None,
    task_id: str,
    findings: dict,
    chushen_result: str,
    chushen_report_url: str,
    author_type_override: str = "",
) -> None:
    """Insert or update the 初审 (first-pass detection) record for one paper.

    Keyed on (submission_no, fold_name): an existing row is UPDATEd (and its 复审 fields
    are reset, status→0) so re-submitting the same file_id is idempotent; otherwise a new
    row is INSERTed with status=0 and chushen_time=now().
    """
    from utils.db import build_paper_metadata

    meta = build_paper_metadata(findings)
    fold_name = _norm_fold(fold_name)

    row = {
        "submission_no": _trunc(submission_no, _WIDTHS["submission_no"]),
        "task_id": _trunc(task_id, _WIDTHS["task_id"]),
        "fold_name": fold_name,
        "title": _trunc(meta["title"], _WIDTHS["title"]),
        "doi": _trunc(meta["doi"], _WIDTHS["doi"]),
        "author": _trunc(meta["author"], _WIDTHS["author"]),
        "author_type": _trunc(author_type_override or meta["author_type"], _WIDTHS["author_type"]),
        "department": _trunc(meta["department"], _WIDTHS["department"]),
        "author_all": meta["author_all"],
        "department_all": meta["department_all"],
        "journal": _trunc(meta["journal"], _WIDTHS["journal"]),
        "chushen_result": _trunc(chushen_result, _WIDTHS["chushen_result"]),
        "chushen_report_url": _trunc(chushen_report_url, _WIDTHS["chushen_report_url"]),
        "chushen_time": datetime.now(),
    }

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT id FROM {TABLE} WHERE {_key_clause(fold_name)}", row)
        exists = cursor.fetchone()

        if exists:
            cursor.execute(
                f"""UPDATE {TABLE} SET
                    task_id=%(task_id)s, title=%(title)s, doi=%(doi)s,
                    author=%(author)s, author_type=%(author_type)s, department=%(department)s,
                    author_all=%(author_all)s, department_all=%(department_all)s, journal=%(journal)s,
                    chushen_result=%(chushen_result)s, chushen_report_url=%(chushen_report_url)s,
                    chushen_time=%(chushen_time)s,
                    review_result=NULL, review_report_url=NULL, review_time=NULL,
                    status=0
                WHERE {_key_clause(fold_name)}""",
                row,
            )
        else:
            cursor.execute(
                f"""INSERT INTO {TABLE} (
                    submission_no, task_id, fold_name, title, doi,
                    author, author_type, department, author_all, department_all, journal,
                    chushen_result, chushen_report_url, chushen_time, status
                ) VALUES (
                    %(submission_no)s, %(task_id)s, %(fold_name)s, %(title)s, %(doi)s,
                    %(author)s, %(author_type)s, %(department)s, %(author_all)s, %(department_all)s, %(journal)s,
                    %(chushen_result)s, %(chushen_report_url)s, %(chushen_time)s, 0
                )""",
                row,
            )
        conn.commit()
        log.info("detection_reports 初审 %s submission_no=%s fold=%s -> %s",
                 "updated" if exists else "inserted", row["submission_no"], fold_name, chushen_result)
    finally:
        conn.close()


def update_review(
    submission_no: str,
    fold_name: str | None,
    review_result: str,
    review_report_url: str,
) -> bool:
    """Write the 复审 (AI review) result for one paper and mark it complete (status=2).

    Returns True if a row was matched. Keyed on (submission_no, fold_name).
    """
    fold_name = _norm_fold(fold_name)
    row = {
        "submission_no": _trunc(submission_no, _WIDTHS["submission_no"]),
        "fold_name": fold_name,
        "review_result": _trunc(review_result, _WIDTHS["review_result"]),
        "review_report_url": _trunc(review_report_url, _WIDTHS["review_report_url"]),
        "review_time": datetime.now(),
    }

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""UPDATE {TABLE} SET
                review_result=%(review_result)s,
                review_report_url=%(review_report_url)s,
                review_time=%(review_time)s,
                status=2
            WHERE {_key_clause(fold_name)}""",
            row,
        )
        matched = cursor.rowcount > 0
        conn.commit()
        log.info("detection_reports 复审 %s submission_no=%s fold=%s -> %s",
                 "updated" if matched else "NOT FOUND", row["submission_no"], fold_name, review_result)
        return matched
    finally:
        conn.close()
