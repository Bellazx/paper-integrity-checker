#!/usr/bin/env python3
"""Diagnose why yujing_4050 high-risk rate is ~32-40%: break down the data-dimension
high anomalies in high-risk papers by test type, col_N involvement, and big-curve
value_recycling (suspected false positives)."""
import json, glob, sys
from collections import Counter
sys.path.insert(0, ".")
from utils.db import get_connection

c = get_connection().cursor()
c.execute("SELECT doi FROM yujing_4050 WHERE risk_level='高风险'")
high_dois = set(r[0] for r in c.fetchall())

idx = {}
for p in glob.glob("data/output/yujing_4050/*/report.json"):
    try:
        idx[json.load(open(p)).get("paper", {}).get("doi")] = p
    except Exception:
        pass

high_test = Counter()
coln = 0
total = 0
bigcurve_vr = 0       # value_recycling on >=500-point columns (curve/spectrum FPs)
vr_total = 0
for doi in high_dois:
    p = idx.get(doi)
    if not p:
        continue
    d = json.load(open(p))
    for a in d.get("data_anomalies", []):
        if a.get("severity") != "high":
            continue
        total += 1
        t = a["test"]
        high_test[t] += 1
        blob = str(a.get("location", "")) + json.dumps(a.get("details", {}), ensure_ascii=False)
        if "col_" in blob:
            coln += 1
        if t == "value_recycling":
            vr_total += 1
            if a.get("details", {}).get("total_count", 0) >= 500:
                bigcurve_vr += 1

print(f"高风险论文数: {len(high_dois)} | 其中有report.json: {sum(1 for d in high_dois if d in idx)}")
print(f"high-data 异常总数: {total}")
print("\nhigh-data 按检测类型:")
for k, v in high_test.most_common():
    print(f"  {k:<28} {v}  ({100*v/total:.0f}%)")
print(f"\n涉及 col_N(表头解析失败)的 high 异常: {coln} ({100*coln/total:.0f}%)")
print(f"value_recycling 总数: {vr_total} | 其中作用在>=500点的大列(疑似曲线假阳性): {bigcurve_vr} ({100*bigcurve_vr/max(vr_total,1):.0f}%)")
