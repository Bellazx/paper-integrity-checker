#!/usr/bin/env python3
"""Estimate the TRUE current high-risk rate by re-running the patched detectors on a
sample of papers, comparing data-dimension level old(stored) vs new(current code)."""
import json, glob, os, sys, importlib
sys.path.insert(0, ".")
import modules.data_checker as dc
importlib.reload(dc)
from modules.chinese_report_generator import _compute_dimension_risk, _apply_data_caps

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 30
rjs = sorted(glob.glob("data/output/yujing_4050/*/report.json"))
sample = []
for rj in rjs:
    d = json.load(open(rj))
    doi = d.get("paper", {}).get("doi", "")
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    if not suffix:
        continue
    cands = [x for x in glob.glob("data/input/4050-matched/*" + suffix) if os.path.isdir(x)]
    if not cands:
        continue
    if any(glob.glob(cands[0] + "/**/*" + e, recursive=True) for e in (".xlsx", ".xls", ".csv")):
        sample.append((doi, cands[0], rj))
    if len(sample) >= LIMIT:
        break

old_high = new_high = 0
flips = []
for doi, ddir, rj in sample:
    d = json.load(open(rj))
    old_dr = _compute_dimension_risk(_apply_data_caps(d.get("data_anomalies", [])))
    if old_dr["level"] == "高风险":
        old_high += 1
    try:
        an = dc.check_data_anomalies(ddir)
    except Exception as e:
        print(doi, "ERR", str(e)[:60]); continue
    new_dr = _compute_dimension_risk(_apply_data_caps(an))
    if new_dr["level"] == "高风险":
        new_high += 1
    if old_dr["level"] != new_dr["level"]:
        flips.append((doi, old_dr["level"], new_dr["level"]))

n = len(sample)
print(f"sample (papers with source data): {n}")
print(f"data-dim HIGH  OLD(stored): {old_high} ({100*old_high/max(n,1):.0f}%)  ->  NEW(current code): {new_high} ({100*new_high/max(n,1):.0f}%)")
print(f"flips: {len(flips)}")
for doi, o, nw in flips[:25]:
    print(f"  {doi}: {o} -> {nw}")