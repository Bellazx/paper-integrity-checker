import glob, os, sys
from collections import Counter
sys.path.insert(0, ".")
from modules.data_checker import check_data_anomalies
from modules.chinese_report_generator import _compute_dimension_risk, _apply_data_caps

for suffix in ["s41467-024-52137-4", "s41467-024-51880-y", "s41467-024-51279-9"]:
    d = [x for x in glob.glob("data/input/4050-matched/*" + suffix) if os.path.isdir(x)]
    if not d:
        print(suffix, "no dir"); continue
    an = check_data_anomalies(d[0])
    dr = _compute_dimension_risk(_apply_data_caps(an))
    highs = [a for a in an if a["severity"] == "high"]
    print("%s -> data dim=%s (score=%d) | high=%d %s" % (
        suffix, dr["level"], dr["score"], len(highs), dict(Counter(a["test"] for a in highs))))
