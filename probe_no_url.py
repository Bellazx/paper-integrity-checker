#!/usr/bin/env python3
"""Probe whether the 1292 no_url papers are fetchable, per publisher.

For a sample from each publisher, try the realistic acquisition routes and report what
actually happens (not just the HTTP code):
  1. Resolve https://doi.org/<doi>  -> where does it land?
  2. On the landing page, is there a citation_pdf_url meta? (the reliable PDF pointer)
  3. Try that PDF url -> real %PDF or an HTML interstitial / paywall?
Also try the OA aggregators as a fallback signal:
  4. Unpaywall (needs email) — report best_oa_location pdf_url if any.
"""
import json, re, sys, time
import requests

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
H = {"User-Agent": UA, "Accept": "text/html,application/pdf,*/*"}

rows = json.load(open("data/output/yujing_4050_no_url_list.json"))
by_pub = {}
for r in rows:
    by_pub.setdefault(r["publisher"], []).append(r)

def probe(doi):
    out = {"doi": doi}
    # 1+2: resolve and scrape citation_pdf_url
    try:
        r = requests.get(f"https://doi.org/{doi}", headers=H, timeout=30, allow_redirects=True)
        out["landed"] = r.url[:70]
        out["land_status"] = r.status_code
        out["land_ct"] = r.headers.get("Content-Type", "")[:30]
        if r.status_code == 200 and "html" in out["land_ct"]:
            m = re.search(r'<meta[^>]+citation_pdf_url[^>]+content=["\']([^"\']+)["\']', r.text, re.I)
            out["citation_pdf_url"] = m.group(1) if m else None
            # 3: try the PDF
            if m:
                try:
                    p = requests.get(m.group(1), headers=H, timeout=40, stream=True, allow_redirects=True)
                    first = p.raw.read(8) if p.status_code == 200 else b""
                    out["pdf_try"] = f"http {p.status_code}, pdf={first.startswith(b'%PDF')}"
                except Exception as e:
                    out["pdf_try"] = f"err:{str(e)[:30]}"
    except Exception as e:
        out["landed"] = f"ERR:{str(e)[:40]}"
    # 4: Unpaywall OA check
    try:
        u = requests.get(f"https://api.unpaywall.org/v2/{doi}?email=paper-check@example.com",
                         headers=H, timeout=25)
        if u.status_code == 200:
            j = u.json()
            loc = j.get("best_oa_location") or {}
            out["unpaywall_oa"] = j.get("is_oa")
            out["unpaywall_pdf"] = (loc.get("url_for_pdf") or "")[:70] if loc else None
        else:
            out["unpaywall_oa"] = f"http {u.status_code}"
    except Exception as e:
        out["unpaywall_oa"] = f"err:{str(e)[:25]}"
    return out

n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
for pub in sorted(by_pub, key=lambda k: -len(by_pub[k])):
    sample = by_pub[pub][:n]
    print(f"\n===== {pub} ({len(by_pub[pub])} papers) — probing {len(sample)} =====")
    for r in sample:
        res = probe(r["doi"])
        print(f"  {r['doi']}")
        print(f"     landed: {res.get('landed')} [{res.get('land_status')}/{res.get('land_ct')}]")
        print(f"     citation_pdf_url: {res.get('citation_pdf_url')}")
        if res.get("pdf_try"):
            print(f"     pdf_try: {res.get('pdf_try')}")
        print(f"     unpaywall: oa={res.get('unpaywall_oa')} pdf={res.get('unpaywall_pdf')}")
        time.sleep(1)
