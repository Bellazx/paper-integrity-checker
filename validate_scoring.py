#!/usr/bin/env python3
"""Validate scoring against ground truth: retracted recall, user-labeled accuracy, yujing high-risk rate."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from modules.chinese_report_generator import _compute_overall_risk, _compute_dimension_risk
import pymssql

OUTPUT_DIRS = [
    Path("data/output"),
    Path("data/output/0514"),
    Path("data/output/Nature-2"),
    Path("data/output/nature-3"),
]

DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}


def find_report(doi: str) -> Path | None:
    for slug in [doi.replace("/", "__"), doi.replace("/", "_"), doi.split("/")[-1]]:
        for d in OUTPUT_DIRS:
            p = d / slug / "report.json"
            if p.exists():
                return p
    return None


def check_full_yujing_rate() -> tuple[int, int]:
    """Simulate rescore on all yujing papers with report.json."""
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi FROM yujing WHERE doi IS NOT NULL")
    all_dois = [row[0] for row in cursor.fetchall()]
    conn.close()

    high = 0
    for doi in all_dois:
        rpath = find_report(doi)
        if not rpath:
            continue
        try:
            with open(rpath) as f:
                findings = json.load(f)
            overall = _compute_overall_risk(findings)
            if overall["level"] == "高风险":
                high += 1
        except Exception:
            pass
    return high, len(all_dois)


def main():
    with open("data/test-set/ground_truth.json") as f:
        gt = json.load(f)

    papers = gt["papers"]

    retracted_tp, retracted_total = 0, 0
    user_tp, user_total = 0, 0
    fn_details = []

    for p in papers:
        doi = p["doi"]
        expected = p["expected_risk"]
        source = p.get("source", "")

        if expected != "高风险":
            continue

        rpath = find_report(doi)
        if not rpath:
            fn_details.append({"doi": doi, "source": source, "reason": "NO REPORT"})
            if source == "retracted":
                retracted_total += 1
            if source == "user_labeled":
                user_total += 1
            continue

        with open(rpath) as f:
            findings = json.load(f)
        overall = _compute_overall_risk(findings)
        predicted = overall["level"]

        if source == "retracted":
            retracted_total += 1
            if predicted == "高风险":
                retracted_tp += 1
            else:
                fn_details.append({"doi": doi, "source": source, "score": overall["score"]})
        elif source == "user_labeled":
            user_total += 1
            if predicted == "高风险":
                user_tp += 1
            else:
                fn_details.append({"doi": doi, "source": source, "score": overall["score"]})

    print("=" * 60)
    print("VALIDATION RESULTS")
    print("=" * 60)

    retracted_recall = retracted_tp / retracted_total * 100 if retracted_total else 0
    user_accuracy = user_tp / user_total * 100 if user_total else 0

    print(f"\nChecking full yujing database rate...")
    yujing_high, yujing_total = check_full_yujing_rate()
    yujing_rate = yujing_high / yujing_total * 100 if yujing_total else 0

    status1 = "✓" if retracted_recall > 70 else "✗"
    status2 = "✓" if user_accuracy == 100 else "✗"
    status3 = "✓" if 15.0 <= yujing_rate <= 20.0 else "✗"

    print(f"\n{status1} 1. Retracted recall: {retracted_tp}/{retracted_total} = {retracted_recall:.1f}% (target: >70%)")
    print(f"{status2} 2. User-labeled accuracy: {user_tp}/{user_total} = {user_accuracy:.1f}% (target: 100%)")
    print(f"{status3} 3. Yujing full DB high-risk rate: {yujing_high}/{yujing_total} = {yujing_rate:.1f}% (target: 15%~20%)")

    if fn_details:
        print(f"\n--- False Negatives ({len(fn_details)}) ---")
        for fn in sorted(fn_details, key=lambda x: x.get("score", 0), reverse=True):
            score = fn.get("score", fn.get("reason", "N/A"))
            print(f"  {fn['doi']} source={fn['source']} score={score}")

    all_pass = retracted_recall > 70 and user_accuracy == 100 and 15.0 <= yujing_rate <= 20.0
    print(f"\n{'ALL CONSTRAINTS SATISFIED ✓' if all_pass else 'CONSTRAINTS NOT MET ✗'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
