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
import json
import sys
from datetime import datetime
from pathlib import Path

import pymssql

from api.config import DB_CONFIG, TASKS_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

log = logging.getLogger(__name__)

TABLE = "detection_reports"


def claim_submission(submission_no, fold_name, task_id, doi=''):
    """Expose the new task immediately; no prior verdict is a new result."""
    fold_name = _norm_fold(fold_name)
    params = {'submission_no': submission_no, 'fold_name': fold_name,
              'task_id': task_id, 'doi': doi}
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(f'SELECT task_id FROM {TABLE} WITH (UPDLOCK, HOLDLOCK) WHERE {_key_clause(fold_name)}', params)
        old = cursor.fetchone()
        if old:
            _assert_newer_owner(task_id, old[0])
            if old[0] != task_id:
                cursor.execute(f'''UPDATE {TABLE} SET task_id=%(task_id)s,
                    doi=%(doi)s, chushen_result=NULL, chushen_report_url=NULL, chushen_time=NULL,
                    review_result=NULL, review_report_url=NULL, review_time=NULL,
                    chushen_report_en=NULL, review_report_en=NULL, status=0
                    WHERE {_key_clause(fold_name)}''', params)
        else:
            cursor.execute(f'''INSERT INTO {TABLE} (submission_no,fold_name,task_id,doi,status)
                VALUES (%(submission_no)s,%(fold_name)s,%(task_id)s,%(doi)s,0)''', params)
        conn.commit()
    finally:
        conn.close()

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


def _assert_newer_owner(task_id: str, previous: str | None) -> None:
    """Task UUID suffixes are random; compare persisted creation times instead."""
    if not previous or previous == task_id:
        return
    try:
        current = json.loads((TASKS_DIR / task_id / 'status.json').read_text())
        prior = json.loads((TASKS_DIR / previous / 'status.json').read_text())
        newer = datetime.fromisoformat(current['created_at']) > datetime.fromisoformat(prior['created_at'])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError('Cannot verify task ownership; refusing to overwrite another task') from exc
    if not newer:
        raise RuntimeError(f'Stale task {task_id} cannot overwrite {previous}')


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
        cursor.execute(f"SELECT id, task_id FROM {TABLE} WITH (UPDLOCK, HOLDLOCK) WHERE {_key_clause(fold_name)}", row)
        exists = cursor.fetchone()
        if exists:
            _assert_newer_owner(task_id, exists[1])

        if exists:
            cursor.execute(
                f"""UPDATE {TABLE} SET
                    task_id=%(task_id)s, title=%(title)s, doi=%(doi)s,
                    author=%(author)s, author_type=%(author_type)s, department=%(department)s,
                    author_all=%(author_all)s, department_all=%(department_all)s, journal=%(journal)s,
                    chushen_result=%(chushen_result)s, chushen_report_url=%(chushen_report_url)s,
                    chushen_time=%(chushen_time)s,
                    review_result=CASE WHEN task_id=%(task_id)s THEN review_result ELSE NULL END,
                    review_report_url=CASE WHEN task_id=%(task_id)s THEN review_report_url ELSE NULL END,
                    review_time=CASE WHEN task_id=%(task_id)s THEN review_time ELSE NULL END,
                    status=CASE WHEN task_id=%(task_id)s THEN status ELSE 0 END
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


def verify_chushen_write(
    submission_no: str,
    fold_name: str | None,
    chushen_report_url: str,
    expected_status: int = 0,
    task_id: str | None = None,
) -> tuple[bool, str]:
    """Read back the first-pass row after upsert and verify URL + status."""
    fold_name = _norm_fold(fold_name)
    row = {
        "submission_no": _trunc(submission_no, _WIDTHS["submission_no"]),
        "fold_name": fold_name,
        "task_id": task_id,
    }
    expected_url = _trunc(chushen_report_url, _WIDTHS["chushen_report_url"])

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT chushen_report_url, status
                FROM {TABLE}
                WHERE {_key_clause(fold_name)} AND (%(task_id)s IS NULL OR task_id=%(task_id)s)""",
            row,
        )
        db_row = cursor.fetchone()
    finally:
        conn.close()

    if not db_row:
        return False, "initial report row was not found after write"
    actual_url, actual_status = db_row[0], db_row[1]
    if (actual_url or "") != expected_url:
        return False, "initial report URL was not persisted as expected"
    if int(actual_status) != expected_status and not (task_id and int(actual_status) == 2):
        return False, f"initial report status is {actual_status}, expected {expected_status}"
    return True, ""


def update_review(
    submission_no: str,
    fold_name: str | None,
    review_result: str,
    review_report_url: str,
    task_id: str,
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
        "task_id": task_id,
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
            WHERE {_key_clause(fold_name)} AND task_id=%(task_id)s""",
            row,
        )
        # pymssql can expose ``rowcount`` as -1 when SQL Server has NOCOUNT
        # enabled (and some drivers return 0 when the assigned values equal the
        # existing values).  Neither means that the logical key was missing.
        # Confirm the row by key before deciding that persistence failed.
        matched = int(cursor.rowcount or 0) > 0
        if not matched:
            cursor.execute(
                f"SELECT 1 FROM {TABLE} WHERE {_key_clause(fold_name)} AND task_id=%(task_id)s",
                row,
            )
            matched = cursor.fetchone() is not None
        conn.commit()
        log.info("detection_reports 复审 %s submission_no=%s fold=%s -> %s",
                 "updated" if matched else "NOT FOUND", row["submission_no"], fold_name, review_result)
        return matched
    finally:
        conn.close()


def update_review_result(
    submission_no: str,
    fold_name: str | None,
    review_result: str,
    task_id: str,
) -> bool:
    """Persist a provisional review result without completing the report.

    The row remains in the initial lifecycle state until the verified PDF URL
    is written by ``update_review``. This lets callers show the model's
    substantive conclusion while report rendering continues asynchronously.
    """
    fold_name = _norm_fold(fold_name)
    row = {
        "submission_no": _trunc(submission_no, _WIDTHS["submission_no"]),
        "fold_name": fold_name,
        "review_result": _trunc(review_result, _WIDTHS["review_result"]),
        "review_time": datetime.now(),
        "task_id": task_id,
    }
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""UPDATE {TABLE} SET
                review_result=%(review_result)s,
                review_time=%(review_time)s
            WHERE {_key_clause(fold_name)} AND task_id=%(task_id)s""",
            row,
        )
        matched = int(cursor.rowcount or 0) > 0
        if not matched:
            cursor.execute(
                f"SELECT 1 FROM {TABLE} WHERE {_key_clause(fold_name)} AND task_id=%(task_id)s",
                row,
            )
            matched = cursor.fetchone() is not None
        conn.commit()
        log.info("detection_reports provisional review %s submission_no=%s fold=%s -> %s",
                 "updated" if matched else "NOT FOUND", row["submission_no"], fold_name, review_result)
        return matched
    finally:
        conn.close()


def verify_review_write(
    submission_no: str,
    fold_name: str | None,
    review_result: str,
    review_report_url: str,
    expected_status: int = 2,
    task_id: str | None = None,
) -> tuple[bool, str]:
    """Read back the review row after update and verify result + URL + status."""
    fold_name = _norm_fold(fold_name)
    row = {
        "submission_no": _trunc(submission_no, _WIDTHS["submission_no"]),
        "fold_name": fold_name,
        "task_id": task_id,
    }
    expected_result = _trunc(review_result, _WIDTHS["review_result"])
    expected_url = _trunc(review_report_url, _WIDTHS["review_report_url"])

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT review_result, review_report_url, status
                FROM {TABLE}
                WHERE {_key_clause(fold_name)} AND (%(task_id)s IS NULL OR task_id=%(task_id)s)""",
            row,
        )
        db_row = cursor.fetchone()
    finally:
        conn.close()

    if not db_row:
        return False, "review row was not found after write"
    actual_result, actual_url, actual_status = db_row[0], db_row[1], db_row[2]
    if (actual_result or "") != expected_result:
        return False, "review result was not persisted as expected"
    if (actual_url or "") != expected_url:
        return False, "review report URL was not persisted as expected"
    if int(actual_status) != expected_status:
        return False, f"review status is {actual_status}, expected {expected_status}"
    return True, ""


def update_english_reports(submission_no, fold_name, task_id, initial_url=None, review_url=None):
    """Persist only verified English URLs, fenced to their current task owner."""
    fold_name = _norm_fold(fold_name)
    params = {'submission_no': submission_no, 'fold_name': fold_name, 'task_id': task_id,
              'initial_url': initial_url, 'review_url': review_url}
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        where = f'{_key_clause(fold_name)} AND task_id=%(task_id)s'
        cursor.execute(f'''UPDATE {TABLE} SET
            chushen_report_en=COALESCE(%(initial_url)s,chushen_report_en),
            review_report_en=COALESCE(%(review_url)s,review_report_en)
            WHERE {where}''', params)
        cursor.execute(f'SELECT chushen_report_en,review_report_en FROM {TABLE} WHERE {where}', params)
        row = cursor.fetchone()
        if (not row or (initial_url and row[0] != initial_url)
                or (review_url and row[1] != review_url)):
            raise RuntimeError('English report URLs did not match the current task row')
        conn.commit()
    finally:
        conn.close()
