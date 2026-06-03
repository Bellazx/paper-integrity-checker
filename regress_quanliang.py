import json, glob, os, sys
from collections import Counter
sys.path.insert(0, ".")
from modules.data_checker import check_data_anomalies
from modules.chinese_report_generator import _compute_dimension_risk, _apply_data_caps

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 60
# quanliang report.json live flat in data/output/<doi>/report.json (not under a table subdir)
rjs = [p for p in glob.glob("data/output/*/report.json")]
old_high = new_high = compared = 0
flips_down = []   # high -> low (FP removed: good, but watch for over-cut)
flips_up = []
for rj in sorted(rjs):
    try:
        d = json.load(open(rj))
    except Exception:
        continue
    paper = d.get("paper", {})
    fp = paper.get("filepath", "")
    # need source data dir; derive paper dir from filepath
    if not fp:
        continue
    pdir = os.path.dirname(fp)
    if not os.path.isdir(pdir):
        continue
    if not any(glob.glob(pdir + "/**/*" + e, recursive=True) for e in (".xlsx", ".xls", ".csv")):
        continue
    old_da = d.get("data_anomalies", [])
    if not old_da:
        continue
    old_dr = _compute_dimension_risk(_apply_data_caps(old_da))
    try:
        new_da = check_data_anomalies(pdir)
    except Exception:
        continue
    new_dr = _compute_dimension_risk(_apply_data_caps(new_da))
    compared += 1
    o, n = old_dr["level"], new_dr["level"]
    if o == "高风险":
        old_high += 1
    if n == "高风险":
        new_high += 1
    if o == "高风险" and n != "高风险":
        flips_down.append(paper.get("doi", rj))
    if o != "高风险" and n == "高风险":
        flips_up.append(paper.get("doi", rj))
    if compared >= LIMIT:
        break

print("quanliang regression: compared=%d (papers with source data)" % compared)
print("  data-dim HIGH  OLD=%d (%.0f%%)  NEW=%d (%.0f%%)" % (
    old_high, 100*old_high/max(compared,1), new_high, 100*new_high/max(compared,1)))
print("  flips HIGH->low (FP removed): %d" % len(flips_down))
print("  flips low->HIGH (new): %d" % len(flips_up))
for x in flips_down[:15]:
    print("    down:", x)
