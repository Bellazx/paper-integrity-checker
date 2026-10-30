#!/usr/bin/env python3
"""Reconcile initial/review state for the 115-paper Aug/Sep 4050 batch.

The operation is intentionally batch-scoped and transactional:

* papers with a confirmed high-risk review are promoted to high initial risk;
* papers with a low-risk review have all review fields cleared;
* status returns to 0 for initial-high papers and NULL for initial-low papers;
* a JSON snapshot is written before the transaction for recovery/audit.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
sys.path.insert(0, str(BASE))

from utils.db import DB_CONFIG


ROUND_DIR = BASE / "data" / "yujing_4050_aug_sep_202609_round_1"
PAPERS_PATH = ROUND_DIR / "papers.json"
TABLE = "yujing_4050_202607"
EXPECTED_TOTAL = 115
EXPECTED_HIGH_REVIEW = 4
EXPECTED_LOW_REVIEW = 111

# Dimension confirmed by the final review report.  This keeps the promoted
# overall risk, score and per-dimension display internally consistent.
HIGH_REVIEW_DIMENSIONS = {
    "10.1016/j.isci.2026.116753": "data",
    "10.1016/j.medj.2026.101188": "ref",
    "10.1016/j.xcrp.2026.103493": "data",
    "10.1126/science.adz7928": "data",
}

SNAPSHOT_COLUMNS = (
    "id", "doi", "total_score", "risk_level",
    "pic_score", "pic_risk_level", "data_score", "data_risk_level",
    "ref_score", "ref_risk_level", "report_url", "generation_time",
    "review_result", "review_report_url", "review_time", "status",
)


def _json_default(value):
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def _score_at_least_61(value) -> str:
    try:
        score = Decimal(str(value or 0))
    except InvalidOperation:
        score = Decimal(0)
    return str(max(Decimal(61), score).quantize(Decimal("1")))


def _load_batch_ids() -> list[int]:
    papers = json.loads(PAPERS_PATH.read_text(encoding="utf-8"))
    ids = [int(paper["id"]) for paper in papers]
    if len(ids) != EXPECTED_TOTAL or len(set(ids)) != EXPECTED_TOTAL:
        raise RuntimeError(f"批次 ID 数量异常: total={len(ids)} unique={len(set(ids))}")
    return ids


def _fetch_rows(cursor, ids: list[int]) -> list[dict]:
    rows: list[dict] = []
    for offset in range(0, len(ids), 50):
        chunk = ids[offset:offset + 50]
        placeholders = ",".join(["%s"] * len(chunk))
        cursor.execute(
            f"SELECT {', '.join(SNAPSHOT_COLUMNS)} FROM {TABLE} "
            f"WHERE id IN ({placeholders})",
            tuple(chunk),
        )
        rows.extend(cursor.fetchall())
    return rows


def _validate(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    if len(rows) != EXPECTED_TOTAL:
        raise RuntimeError(f"数据库批次行数异常: {len(rows)}")
    high = [row for row in rows if str(row["review_result"] or "").strip() == "高风险"]
    low = [row for row in rows if str(row["review_result"] or "").strip() == "低风险"]
    other = [row for row in rows if row not in high and row not in low]
    if len(high) != EXPECTED_HIGH_REVIEW or len(low) != EXPECTED_LOW_REVIEW or other:
        raise RuntimeError(
            f"复审分组与预期不符: high={len(high)} low={len(low)} other={len(other)}"
        )
    high_dois = {str(row["doi"]).strip().lower() for row in high}
    if high_dois != set(HIGH_REVIEW_DIMENSIONS):
        raise RuntimeError(f"复审高风险 DOI 发生变化: {sorted(high_dois)}")
    return high, low


def _write_snapshot(rows: list[dict]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = ROUND_DIR / f"db_reconcile_snapshot_{stamp}.json"
    payload = {
        "table": TABLE,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "promote review-high initial results and clear review-low results",
        "rows": rows,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="commit the validated changes")
    args = parser.parse_args()

    ids = _load_batch_ids()
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor(as_dict=True)
        rows = _fetch_rows(cursor, ids)
        high, low = _validate(rows)
        print(f"validated: total={len(rows)} review_high={len(high)} review_low={len(low)}")
        if not args.apply:
            print("dry-run only; pass --apply to commit")
            return 0

        snapshot = _write_snapshot(rows)
        try:
            for row in high:
                doi = str(row["doi"]).strip().lower()
                dimension = HIGH_REVIEW_DIMENSIONS[doi]
                dimension_score = f"{dimension}_score"
                dimension_risk = f"{dimension}_risk_level"
                cursor.execute(
                    f"""UPDATE {TABLE}
                        SET total_score=%s, risk_level=N'高风险',
                            {dimension_score}=%s, {dimension_risk}=N'高风险',
                            report_url=review_report_url
                        WHERE id=%s AND doi=%s AND review_result=N'高风险'""",
                    (
                        _score_at_least_61(row["total_score"]),
                        _score_at_least_61(row[dimension_score]),
                        row["id"], row["doi"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"高风险同步行数异常: {doi} rows={cursor.rowcount}")

            for row in low:
                next_status = 0 if str(row["risk_level"] or "").strip() == "高风险" else None
                cursor.execute(
                    f"""UPDATE {TABLE}
                        SET review_result=NULL, review_report_url=NULL,
                            review_time=NULL, status=%s
                        WHERE id=%s AND doi=%s AND review_result=N'低风险'""",
                    (next_status, row["id"], row["doi"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"低风险复审清理行数异常: {row['doi']} rows={cursor.rowcount}"
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        print(f"committed; snapshot={snapshot}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
