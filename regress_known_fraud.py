import json, glob, sys
from collections import Counter
sys.path.insert(0, ".")
from modules.data_checker import check_data_anomalies
from modules.chinese_report_generator import _compute_dimension_risk, _apply_data_caps

ddir = "data/input/test-set/10.1038__s43018-023-00715-8"
# stored (old) result
old = json.load(open("data/output/10.1038__s43018-023-00715-8/report.json"))
old_da = old.get("data_anomalies", [])
old_dr = _compute_dimension_risk(_apply_data_caps(old_da))
old_high = [a for a in old_da if a["severity"] == "high"]
print("KNOWN-FRAUD s43018-023-00715-8")
print("  OLD(stored): data dim=%s, high=%d, types=%s" % (
    old_dr["level"], len(old_high), dict(Counter(a["test"] for a in old_high))))

# patched (new) result
new_da = check_data_anomalies(ddir)
new_dr = _compute_dimension_risk(_apply_data_caps(new_da))
new_high = [a for a in new_da if a["severity"] == "high"]
print("  NEW(patched): data dim=%s, high=%d, types=%s" % (
    new_dr["level"], len(new_high), dict(Counter(a["test"] for a in new_high))))
print("  VERDICT:", "OK still fires" if new_dr["level"] == "高风险" else "!!! REGRESSION - fraud no longer high")
