#!/usr/bin/env python3
"""Build the list of 1292 'no_url' papers (no PDF could be resolved) with rich metadata,
so we can (a) hand the user a clear list and (b) probe whether they are fetchable."""
import json, os, csv
from collections import Counter

BASE = "data/input/4050-matched"
audit = json.load(open("data/output/yujing_4050_pdf_fetch.json"))
nourl = [r for r in audit if r["status"] == "no_url"]

def pub(doi):
    p = doi.split("/")[0]
    return {"10.1038":"Nature","10.1016":"Elsevier","10.1126":"Science","10.1073":"PNAS",
            "10.1002":"Wiley","10.34133":"Research","10.1136":"BMJ","10.1056":"NEJM",
            "10.1001":"JAMA","10.1021":"ACS","10.1186":"BMC","10.1385":"Humana"}.get(p, p)

rows = []
for r in nourl:
    doi, dirn = r["doi"], r["dir"]
    art = title = ""
    has_html = os.path.exists(f"{BASE}/{dirn}/article.html") or os.path.exists(f"{BASE}/{dirn}/html/article.html")
    mpath = f"{BASE}/{dirn}/manifest.json"
    if os.path.exists(mpath):
        try:
            m = json.load(open(mpath))
            art = m.get("article_url") or m.get("canonical_url") or m.get("doi_resolve_url") or ""
            title = (m.get("article_title") or "")[:120]
        except Exception:
            pass
    if not art:
        art = f"https://doi.org/{doi}"
    rows.append({"dir": dirn, "doi": doi, "publisher": pub(doi),
                 "has_html": has_html, "title": title, "article_url": art})

rows.sort(key=lambda x: (x["publisher"], x["doi"]))

# JSON
json.dump(rows, open("data/output/yujing_4050_no_url_list.json", "w"), ensure_ascii=False, indent=2)
# CSV (easy to read/share)
with open("data/output/yujing_4050_no_url_list.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["publisher", "doi", "title", "has_html", "article_url", "dir"])
    w.writeheader()
    for r in rows:
        w.writerow({k: r[k] for k in w.fieldnames})

print("wrote data/output/yujing_4050_no_url_list.{json,csv} —", len(rows), "papers")
print("\nby publisher:")
for k, v in Counter(r["publisher"] for r in rows).most_common():
    print("  %-10s %d" % (k, v))
print("\nhas article.html (HTML present but no PDF link resolved):",
      sum(1 for r in rows if r["has_html"]))
print("manifest/empty only (no HTML at all):", sum(1 for r in rows if not r["has_html"]))
