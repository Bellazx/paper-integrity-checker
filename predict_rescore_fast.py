#!/usr/bin/env python3
"""Fast prediction: simulate rescore using existing report.json data only.
Applies new scoring caps + filters existing anomalies by IV keywords/col_N.
No Excel I/O — runs in seconds."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.chinese_report_generator import _compute_overall_risk, _apply_data_caps
from modules.data_checker import _is_independent_variable, _is_unnamed_column

import pymssql

BASE = Path(__file__).resolve().parent
OUTPUT_DIRS = [
    BASE / "data" / "output" / "0514",
    BASE / "data" / "output" / "Nature-2",
    BASE / "data" / "output" / "nature-3",
    BASE / "data" / "output",
]
DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}

COL_RE = re.compile(r"column '([^']+)'")


def _find_report(doi):
    doi_dir = doi.replace("https://doi.org/", "").replace("/", "__")
    for odir in OUTPUT_DIRS:
        rpath = odir / doi_dir / "report.json"
        if rpath.exists():
            return str(rpath)
    return None


def _extract_col_name(location: str) -> str:
    m = COL_RE.search(location)
    return m.group(1) if m else ""


def _should_filter_anomaly(anomaly: dict) -> bool:
    """Check if this anomaly would be filtered by the new IV/col_N logic."""
    location = anomaly.get("location", "")
    col = _extract_col_name(location)
    if not col:
        return False
    if _is_independent_variable(col):
        return True
    if _is_unnamed_column(col):
        return True
    return False


def main():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi, risk_level, data_score FROM yujing WHERE risk_level = '高风险'")
    papers = cursor.fetchall()
    conn.close()

    print(f"=== 高风险论文预测（快速模式）===", flush=True)
    print(f"当前高风险论文总数: {len(papers)}", flush=True)
    print(flush=True)

    no_report = 0
    no_change = 0
    changes = []
    errors = 0
    filtered_total = 0

    for doi, old_level, old_data_score in papers:
        rpath = _find_report(doi)
        if not rpath:
            no_report += 1
            continue

        try:
            with open(rpath) as f:
                findings = json.load(f)

            old_risk = _compute_overall_risk(findings)

            old_anomalies = findings.get("data_anomalies", [])
            new_anomalies = [a for a in old_anomalies if not _should_filter_anomaly(a)]
            filtered = len(old_anomalies) - len(new_anomalies)
            filtered_total += filtered

            findings_copy = dict(findings)
            findings_copy["data_anomalies"] = new_anomalies

            new_risk = _compute_overall_risk(findings_copy)

            if old_risk["level"] != new_risk["level"]:
                changes.append({
                    "doi": doi,
                    "old_level": old_risk["level"],
                    "old_score": old_risk["score"],
                    "new_level": new_risk["level"],
                    "new_score": new_risk["score"],
                    "old_data": old_risk.get("data_score", old_data_score or 0),
                    "new_data": new_risk.get("data_score", 0),
                    "filtered": filtered,
                    "old_anomaly_count": len(old_anomalies),
                    "new_anomaly_count": len(new_anomalies),
                })
            else:
                no_change += 1

        except Exception as e:
            errors += 1

    print(f"=== 预测结果 ===", flush=True)
    print(f"高风险论文总数: {len(papers)}", flush=True)
    print(f"有report.json: {len(papers) - no_report}", flush=True)
    print(f"无report.json: {no_report}", flush=True)
    print(f"总共过滤异常数: {filtered_total}", flush=True)
    print(f"处理错误: {errors}", flush=True)
    print(flush=True)

    to_mid = [c for c in changes if c["new_level"] == "中风险"]
    to_low = [c for c in changes if c["new_level"] == "低风险"]

    print(f"=== 风险等级变化 ===", flush=True)
    print(f"维持高风险: {no_change}", flush=True)
    print(f"高风险 → 中风险: {len(to_mid)}", flush=True)
    print(f"高风险 → 低风险: {len(to_low)}", flush=True)
    print(f"总计将减少高风险: {len(changes)} 篇", flush=True)
    print(flush=True)

    if to_low:
        print("--- 降为低风险的论文 ---", flush=True)
        for c in sorted(to_low, key=lambda x: x["new_score"]):
            print(f"  {c['doi']}: score {c['old_score']:.1f} → {c['new_score']:.1f} "
                  f"(data: {c['old_data']:.0f} → {c['new_data']:.0f}, "
                  f"异常: {c['old_anomaly_count']} → {c['new_anomaly_count']}, "
                  f"过滤: {c['filtered']}项)", flush=True)
    print(flush=True)
    if to_mid:
        print("--- 降为中风险的论文 ---", flush=True)
        for c in sorted(to_mid, key=lambda x: x["new_score"]):
            print(f"  {c['doi']}: score {c['old_score']:.1f} → {c['new_score']:.1f} "
                  f"(data: {c['old_data']:.0f} → {c['new_data']:.0f}, "
                  f"异常: {c['old_anomaly_count']} → {c['new_anomaly_count']}, "
                  f"过滤: {c['filtered']}项)", flush=True)

    print(flush=True)
    print(f"注意：此为快速预测，仅基于现有report.json中的异常重新评分。", flush=True)
    print(f"实际全量rescore还会重新检测源数据，结果可能略有差异。", flush=True)


if __name__ == "__main__":
    main()
