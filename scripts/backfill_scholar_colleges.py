#!/usr/bin/env python3
"""Backfill yujing_scholar.college from an explicit scholar-to-college JSON map."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymssql

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from utils.db import DB_CONFIG


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mapping", help='JSON object, for example {"张三": "机械与动力工程学院"}')
    parser.add_argument("--clear-missing", action="store_true", help="Clear college for scholars absent from the map")
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only fill NULL/blank college values and preserve existing non-blank values",
    )
    args = parser.parse_args()

    path = Path(args.mapping).expanduser().resolve()
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not mapping or not all(
        isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
        for key, value in mapping.items()
    ):
        raise SystemExit("Mapping must be a non-empty JSON object of scholar: college strings")

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        if args.clear_missing:
            cursor.execute("UPDATE yujing_scholar SET college=NULL")
        for scholar, college in mapping.items():
            sql = "UPDATE yujing_scholar SET college=%s WHERE scholar=%s"
            if args.only_missing:
                sql += " AND LTRIM(RTRIM(ISNULL(college,N'')))=N''"
            cursor.execute(sql, (college.strip()[:200], scholar.strip()[:100]))
            print(f"{scholar}: {cursor.rowcount} row(s) -> {college}")
        conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
