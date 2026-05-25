#!/usr/bin/env python3
"""Predict how many high-risk papers would change risk level after rescore.
Does NOT modify any data — read-only simulation."""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.chinese_report_generator import _compute_overall_risk, _apply_data_caps
from modules.data_checker import check_data_anomalies

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("predict")

import pymssql

BASE = Path(__file__).resolve().parent
OUTPUT_DIRS = [
    BASE / "data" / "output" / "0514",
    BASE / "data" / "output" / "Nature-2",
    BASE / "data" / "output" / "nature-3",
    BASE / "data" / "output",
]
INPUT_DIRS = [
    BASE / "data" / "input" / "Nature0514",
    BASE / "data" / "input" / "Nature-2",
    BASE / "data" / "input" / "nature-3",
    BASE / "data" / "input" / "science99",
    BASE / "data" / "input" / "wiley-to-science",
    BASE / "data" / "input" / "cell-1-extracted",
    BASE / "data" / "input" / "cell-2" / "cell-2全",
]
DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}


def _find_report(doi):
    doi_dir = doi.replace("https://doi.org/", "").replace("/", "__")
    for odir in OUTPUT_DIRS:
        rpath = odir / doi_dir / "report.json"
        if rpath.exists():
            return str(rpath)
    return None


def _find_input_dir(doi):
    doi_clean = doi.replace("https://doi.org/", "")
    candidates = [
        doi_clean.replace("/", "__"),
        doi_clean.replace("/", "_"),
    ]
    parts = doi_clean.split("/", 1)
    if len(parts) == 2:
        candidates.append(parts[0] + parts[1])

    for idir in INPUT_DIRS:
        for dirname in candidates:
            candidate = idir / dirname
            if candidate.exists():
                for sub in ("extended_data", "source_data"):
                    sd = candidate / sub
                    if sd.exists() and any(sd.iterdir()):
                        return str(candidate)
                if any(candidate.rglob("*.xlsx")):
                    return str(candidate)
    return None


def main():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi, risk_level, data_score FROM yujing WHERE risk_level = '高风险'")
    papers = cursor.fetchall()
    conn.close()

    print(f"=== 高风险论文预测 ===")
    print(f"当前高风险论文总数: {len(papers)}")
    print()

    no_report = 0
    no_change = 0
    changes = []
    errors = 0
    redetected = 0
    no_input = 0

    for i, (doi, old_level, old_data_score) in enumerate(papers):
        rpath = _find_report(doi)
        if not rpath:
            no_report += 1
            continue

        try:
            with open(rpath) as f:
                findings = json.load(f)

            old_risk = _compute_overall_risk(findings)

            input_dir = _find_input_dir(doi)
            if input_dir:
                new_anomalies = check_data_anomalies(input_dir)
                old_count = len(findings.get("data_anomalies", []))
                findings_copy = dict(findings)
                findings_copy["data_anomalies"] = new_anomalies
                if old_count != len(new_anomalies):
                    redetected += 1
            else:
                findings_copy = findings
                no_input += 1

            new_risk = _compute_overall_risk(findings_copy)

            if old_risk["level"] != new_risk["level"]:
                changes.append({
                    "doi": doi,
                    "old_level": old_risk["level"],
                    "old_score": old_risk["score"],
                    "new_level": new_risk["level"],
                    "new_score": new_risk["score"],
                    "old_data": old_risk.get("data_score", old_data_score),
                    "new_data": new_risk.get("data_score", 0),
                })
            else:
                no_change += 1

            if (i + 1) % 20 == 0:
                print(f"进度: {i+1}/{len(papers)} (变化={len(changes)}, 不变={no_change}, 重检={redetected})", flush=True)

        except Exception as e:
            errors += 1

    print(f"\n=== 预测结果 ===")
    print(f"高风险论文总数: {len(papers)}")
    print(f"有report.json: {len(papers) - no_report}")
    print(f"无report.json: {no_report}")
    print(f"有源数据可重检: {redetected + (len(papers) - no_report - no_input - errors)}")
    print(f"无源数据: {no_input}")
    print(f"重检后异常数变化: {redetected}")
    print(f"处理错误: {errors}")
    print()

    to_mid = [c for c in changes if c["new_level"] == "中风险"]
    to_low = [c for c in changes if c["new_level"] == "低风险"]

    print(f"=== 风险等级变化 ===")
    print(f"维持高风险: {no_change}")
    print(f"高风险 → 中风险: {len(to_mid)}")
    print(f"高风险 → 低风险: {len(to_low)}")
    print(f"总计将减少高风险: {len(changes)} 篇")
    print()

    if to_low:
        print("--- 降为低风险的论文 ---")
        for c in to_low:
            print(f"  {c['doi']}: score {c['old_score']:.1f} → {c['new_score']:.1f} (data: {c['old_data']:.0f} → {c['new_data']:.0f})")
    if to_mid:
        print("--- 降为中风险的论文 ---")
        for c in to_mid:
            print(f"  {c['doi']}: score {c['old_score']:.1f} → {c['new_score']:.1f} (data: {c['old_data']:.0f} → {c['new_data']:.0f})")


if __name__ == "__main__":
    main()
