#!/usr/bin/env python3
"""Promote the three retained rereview-high papers in yujing_scholar.

The update is deliberately limited to the confirmed three-paper set.  A
snapshot is written before the transaction so the initial-screening fields,
dimension, score, and public report link can be audited or restored.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.db import DB_CONFIG

BASE = Path("/home/sjtu/paper-integrity-checker")
OUT = BASE / "data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1"
TABLE = "yujing_scholar"

TARGETS = {
    "10.1016/j.cmet.2026.06.020": {
        "dimension": "data",
        "report_url": "http://202.120.57.100/review_reports/yujing_scholar/review_10.1016_j.cmet.2026.06.020.pdf",
    },
    "10.1016/j.fuel.2021.121866": {
        "dimension": "data",
        "report_url": "http://202.120.57.100/review_reports/yujing_scholar/review_10.1016_j.fuel.2021.121866.pdf",
    },
    "10.19657/j.geoscience.1000－8527.2021.173": {
        "dimension": "data",
        "report_url": "http://202.120.57.100/review_reports/yujing_scholar/review_10.19657_j.geoscience.10008527.2021.173.pdf",
    },
}

COLUMNS = (
    "id", "doi", "risk_level", "total_score", "data_score",
    "data_risk_level", "pic_score", "pic_risk_level", "ref_score",
    "ref_risk_level", "report_url", "review_result", "review_report_url",
    "status",
)


def _json_default(value):
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def main() -> int:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        placeholders = ",".join(["%s"] * len(TARGETS))
        cur.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE doi IN ({placeholders})",
            tuple(TARGETS),
        )
        rows = cur.fetchall()
        by_doi = {str(row["doi"]).strip().lower(): row for row in rows}
        if set(by_doi) != {d.lower() for d in TARGETS}:
            raise RuntimeError(f"target rows mismatch: {sorted(by_doi)}")
        for doi, row in by_doi.items():
            if str(row["review_result"] or "").strip() != "高风险":
                raise RuntimeError(f"review result is not high risk: {doi}")

        OUT.mkdir(parents=True, exist_ok=True)
        snapshot = OUT / f"initial_risk_sync_snapshot_{datetime.now():%Y%m%d_%H%M%S}.json"
        snapshot.write_text(
            json.dumps({"table": TABLE, "rows": rows}, ensure_ascii=False,
                       indent=2, default=_json_default), encoding="utf-8"
        )

        for doi, spec in TARGETS.items():
            row = by_doi[doi.lower()]
            dimension = spec["dimension"]
            cur.execute(
                f"""UPDATE {TABLE}
                    SET risk_level=N'高风险',
                        total_score=CASE WHEN TRY_CONVERT(decimal(10,2), total_score) >= 61 THEN total_score ELSE 61 END,
                        {dimension}_score=CASE WHEN TRY_CONVERT(decimal(10,2), {dimension}_score) >= 61 THEN {dimension}_score ELSE 61 END,
                        {dimension}_risk_level=N'高风险',
                        report_url=%s
                    WHERE id=%s AND doi=%s AND review_result=N'高风险'""",
                (spec["report_url"], row["id"], row["doi"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"updated rows mismatch for {doi}: {cur.rowcount}")
        conn.commit()
        print(f"updated {len(TARGETS)} rows; snapshot={snapshot}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
