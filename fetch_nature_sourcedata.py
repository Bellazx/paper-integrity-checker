#!/usr/bin/env python3
"""Fetch Nature source-data Excel/CSV for 4050-matched papers lacking real source data.

Nature publishes "Source Data" as MOESM*_ESM.xlsx on the article page, served from
Springer's static CDN (static-content.springer.com, no anti-bot). Crawled dirs often lack
these. Scrape the article page for MOESM*_ESM.(xlsx|xls|csv) links, download into
<paper_dir>/source_data/. Papers with none -> 'no_source'.

Safety (post EuropePMC-throttling): SERIAL + polite delay + circuit breaker + resumable
(skips non-empty source_data/). Audit -> data/output/nature_sourcedata_fetch.json
"""
import argparse, json, logging, re, time
from collections import Counter
from pathlib import Path
import requests

BASE = Path(__file__).resolve().parent
INP = BASE / "data" / "input" / "4050-matched"
NEED_LIST = BASE / "data" / "4050_status" / "nature_need_sourcedata.json"
AUDIT = BASE / "data" / "output" / "nature_sourcedata_fetch.json"
H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
SRC_RE = re.compile(r'(https://static[^"\']*MOESM\d+_ESM\.(?:xlsx|xls|csv))', re.IGNORECASE)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fetch-nature-sd")


def _get(url, tries=3):
    last = "?"
    for a in range(tries):
        try:
            r = requests.get(url, headers=H, timeout=60, allow_redirects=True)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503):
                last = f"http_{r.status_code}"; time.sleep(5 * (a + 1)); continue
            return f"http_{r.status_code}"
        except Exception as e:
            last = f"error:{str(e)[:30]}"; time.sleep(5 * (a + 1))
    return last


def fetch_one(item, delay) -> dict:
    doi = item["doi"]; dirn = item["dir"]
    d = INP / dirn
    res = {"doi": doi, "dir": dirn}
    if not d.is_dir():
        return {**res, "status": "dir_missing"}
    sd = d / "source_data"
    if sd.exists() and any(sd.iterdir()):
        return {**res, "status": "already"}
    art = doi.split("/", 1)[1]
    page = _get(f"https://www.nature.com/articles/{art}")
    if isinstance(page, str):
        return {**res, "status": page}
    links = sorted(set(SRC_RE.findall(page.text)))
    if not links:
        return {**res, "status": "no_source", "n": 0}
    time.sleep(delay)
    got = failed = 0
    sd.mkdir(parents=True, exist_ok=True)
    for url in links:
        r = _get(url)
        if isinstance(r, str):
            failed += 1; time.sleep(delay); continue
        # valid Excel magic: xlsx/zip = 'PK', legacy .xls (OLE2) = D0 CF 11 E0
        magic = r.content[:4]
        is_excel = magic[:2] == b"PK" or magic == b"\xd0\xcf\x11\xe0"
        if url.lower().endswith((".xlsx", ".xls")) and not is_excel:
            failed += 1; time.sleep(delay); continue
        try:
            (sd / url.split("/")[-1]).write_bytes(r.content); got += 1
        except Exception:
            failed += 1
        time.sleep(delay)
    return {**res, "status": "ok" if got else "fetch_failed", "n": got, "failed": failed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=2.5,
                    help="Per-worker seconds between requests")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--cooldown", type=int, default=300)
    ap.add_argument("--max-consec-fail", type=int, default=8)
    args = ap.parse_args()

    need = json.load(open(NEED_LIST))
    if args.limit:
        need = need[:args.limit]
    log.info("Nature source-data fetch: %d papers (mode=%s, delay=%.1fs)",
             len(need), "DRY-RUN" if args.dry_run else "FETCH", args.delay)

    results = []
    if args.dry_run:
        for item in need:
            sd = INP / item["dir"] / "source_data"
            results.append({**item, "status": "already" if (sd.exists() and any(sd.iterdir())) else "would_fetch"})
    else:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        lock = threading.Lock()
        resume = threading.Event(); resume.set()   # cleared => workers pause (cooldown)
        cooling = threading.Lock()
        state = {"consec": 0, "done": 0}

        def worker(item):
            resume.wait()
            r = fetch_one(item, args.delay)
            st = r["status"]
            with lock:
                results.append(r); state["done"] += 1
                i = state["done"]
                if i % 20 == 0 or st not in ("already", "ok", "no_source"):
                    log.info("[%d/%d] %s -> %s (n=%s)", i, len(need), r["doi"][:32], st, r.get("n", "-"))
                state["consec"] = state["consec"] + 1 if (st.startswith("http_") or st.startswith("error")) else 0
                if i % 25 == 0:
                    AUDIT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                trip = state["consec"] >= args.max_consec_fail
            if trip and cooling.acquire(blocking=False):
                try:
                    resume.clear()
                    log.warning("!! %d consec failures — global cooldown %ds", state["consec"], args.cooldown)
                    AUDIT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                    time.sleep(args.cooldown)
                    with lock:
                        state["consec"] = 0
                    resume.set()
                finally:
                    cooling.release()

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(worker, need))

    AUDIT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tally = Counter(r["status"] for r in results)
    got_files = sum(r.get("n", 0) for r in results if r["status"] == "ok")
    log.info("=" * 56)
    log.info("DONE %s", "DRY-RUN" if args.dry_run else "")
    for k, v in tally.most_common():
        log.info("  %-14s %d", k, v)
    log.info("papers with source data fetched: %d (%d files)",
             tally.get("ok", 0), got_files)
    log.info("Audit -> %s", AUDIT)


if __name__ == "__main__":
    main()
