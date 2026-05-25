#!/usr/bin/env python3
"""Re-score existing report.json files and update yujing_quanliang with corrected risk levels."""
import json
import sys
import os
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
from modules.chinese_report_generator import (
    _compute_overall_risk, _compute_dimension_risk, _apply_data_caps,
)
import pymssql

DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}

INPUT_DIR = Path("data/input/20260520-649")
OUTPUT_DIR = Path("data/output")


def main():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    dirs = sorted(os.listdir(INPUT_DIR))
    updated = 0
    changed = 0

    for d in dirs:
        rpath = OUTPUT_DIR / d / "report.json"
        if not rpath.exists():
            continue

        with open(rpath) as f:
            findings = json.load(f)

        doi = findings.get("paper", {}).get("doi", "")
        if not doi:
            continue

        image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
        capped_data = _apply_data_caps(findings.get("data_anomalies", []))
        data_risk = _compute_dimension_risk(capped_data)
        ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
        overall = _compute_overall_risk(findings)

        cursor.execute(
            "SELECT risk_level, total_score FROM yujing_quanliang WHERE doi=%s", (doi,)
        )
        row = cursor.fetchone()
        if not row:
            continue

        old_level, old_score = row
        new_level = overall["level"]
        new_score = str(overall["score"])

        cursor.execute(
            """UPDATE yujing_quanliang SET
                total_score=%s, risk_level=%s,
                pic_score=%s, pic_risk_level=%s,
                data_score=%s, data_risk_level=%s,
                ref_score=%s, ref_risk_level=%s
            WHERE doi=%s""",
            (
                new_score, new_level,
                str(image_risk["score"]), image_risk["level"],
                str(data_risk["score"]), data_risk["level"],
                str(ref_risk["score"]), ref_risk["level"],
                doi,
            ),
        )
        updated += 1
        if old_level != new_level:
            changed += 1
            print(f"  {doi}: {old_level}({old_score}) -> {new_level}({new_score})")

    conn.commit()
    conn.close()

    cursor_check = pymssql.connect(**DB_CONFIG).cursor()
    cursor_check.execute(
        "SELECT risk_level, COUNT(*) FROM yujing_quanliang GROUP BY risk_level"
    )
    print(f"\nUpdated: {updated}, Changed level: {changed}")
    print("\nyujing_quanliang distribution:")
    for level, cnt in cursor_check.fetchall():
        print(f"  {level}: {cnt}")


if __name__ == "__main__":
    main()
