#!/usr/bin/env python3
"""Re-run the CURRENT data_checker on papers that are 'data high-risk' in the stale DB,
to determine whether the 40% high-risk is from OLD code (DB stale) or still reproduces
with the patched detectors."""
import json, glob, os, sys, importlib
from collections import Counter
sys.path.insert(0, ".")
import modules.data_checker as dc
importlib.reload(dc)
from modules.chinese_report_generator import _compute_dimension_risk, _apply_data_caps

targets = ["10.1038/s41467-024-52137-4", "10.1038/s41467-024-51880-y",
           "10.1038/s41467-024-51279-9"]
for doi in targets:
    suffix = doi.split("/", 1)[1]
    cands = [d for d in glob.glob("data/input/4050-matched/*" + suffix) if os.path.isdir(d)]
    if not cands:
        print(doi, "-> input dir not found"); continue
    ddir = cands[0]
    try:
        anomalies = dc.check_data_anomalies(ddir)
    except Exception as e:
        print(doi, "-> error:", str(e)[:100]); continue
    capped = _apply_data_caps(anomalies)
    dr = _compute_dimension_risk(capped)
    highs = [a for a in anomalies if a["severity"] == "high"]
    print(f"\n{doi}")
    print(f"  CURRENT code: {len(anomalies)} anomalies, high={len(highs)} -> data dim={dr['level']} (score={dr['score']})")
    print(f"  high by type: {dict(Counter(a['test'] for a in highs))}")
    coln = sum(1 for a in highs if 'col_' in (str(a.get('location',''))+json.dumps(a.get('details',{}),ensure_ascii=False)))
    print(f"  high involving col_N: {coln}")