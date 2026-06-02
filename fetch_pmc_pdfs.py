#!/usr/bin/env python3
"""Open-access PDF rescue for the 1292 'no_url' papers via NCBI + Europe PMC.

Direct publisher fetch failed for these (paywall / anti-bot: Elsevier landing pages carry
no citation_pdf_url, PNAS/Science/NEJM/JAMA return 403, some Research DOIs 404). But many
are mirrored open-access in PubMed Central. Reliable, ToS-compliant route discovered by
probing:
    DOI --(NCBI ID Converter, batch<=200)--> PMCID
    PMCID --(https://europepmc.org/articles/<PMCID>?pdf=render)--> real PDF (HTTP 200, %PDF)

Europe PMC's ?pdf=render endpoint serves the OA PDF directly over HTTPS with no NCBI
"Preparing to download" interstitial. Papers with no PMCID (pure-subscription: NEJM/JAMA/
Science, dead DOIs) cannot be rescued and are recorded as such.

Writes each PDF into the paper's own dir as <doi_slug>.pdf (so screen_4050.py --prefer-pdf
picks it up). Audit: data/output/yujing_4050_pmc_fetch.json.

Usage
  python fetch_pmc_pdfs.py --dry-run        # resolve PMCIDs only, report coverage
  python fetch_pmc_pdfs.py --limit 50
  python fetch_pmc_pdfs.py --workers 6      # full run over the 1292
"""
import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
INPUT_BASE = BASE / "data" / "input" / "4050-matched"
NO_URL_LIST = BASE / "data" / "output" / "yujing_4050_no_url_list.json"
AUDIT = BASE / "data" / "output" / "yujing_4050_pmc_fetch.json"
EMAIL = "paper.check.4050@gmail.com"   # NCBI/Unpaywall require a real-format address
H = {"User-Agent": f"paper-integrity-checker (mailto:{EMAIL})"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fetch-pmc")


def _dir_of(dirn: str) -> Path:
    return INPUT_BASE / dirn


def _existing_pdf(d: Path):
    p = list(d.glob("*.pdf")) + list(d.glob("*.PDF"))
    return p[0] if p else None


def resolve_pmcids(dois: list) -> dict:
    """Batch DOI->PMCID via NCBI ID Converter (<=200 ids per call)."""
    out = {}
    for i in range(0, len(dois), 200):
        chunk = dois[i:i + 200]
        url = ("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
               f"?tool=pic&email={EMAIL}&ids={','.join(chunk)}&format=json")
        for attempt in range(3):
            try:
                r = requests.get(url, headers=H, timeout=40)
                if r.status_code == 200:
                    for rec in r.json().get("records", []):
                        if rec.get("pmcid"):
                            out[rec.get("doi")] = rec["pmcid"]
                    break
            except Exception as e:
                log.warning("idconv chunk %d attempt %d: %s", i, attempt + 1, e)
            time.sleep(2 * (attempt + 1))
        time.sleep(0.34)  # NCBI: <=3 req/s without an API key
        log.info("  resolved %d/%d ...", min(i + 200, len(dois)), len(dois))
    return out


def download_pdf(pmcid: str, dest: Path) -> str:
    """EuropePMC render endpoint. It rate-limits aggressively under concurrency, so the
    caller drives this SERIALLY with a delay; here we just back off harder on 429/5xx."""
    url = f"https://europepmc.org/articles/{pmcid}?pdf=render"
    last = "?"
    for attempt in range(4):
        try:
            r = requests.get(url, headers=H, timeout=90, allow_redirects=True)
            if r.status_code == 200 and r.content[:8].startswith(b"%PDF"):
                dest.write_bytes(r.content)
                return "ok"
            if r.status_code in (500, 502, 503, 429):
                last = f"http_{r.status_code}"
                time.sleep(5 * (attempt + 1)); continue   # 5s,10s,15s backoff
            return f"http_{r.status_code}"
        except Exception as e:
            last = f"error:{str(e)[:40]}"
            time.sleep(5 * (attempt + 1))
    return last


def download_supplements(pmcid: str, dest_dir: Path) -> str:
    """Fetch the EuropePMC supplementaryFiles zip and extract it into dest_dir/supplementary/.

    Returns 'sNN' (N files extracted), 'none' (no supp / 404), or an error/backoff code.
    Source data tables (.xlsx/.csv) and supplementary PDFs/figures live here.
    """
    import zipfile, io
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles"
    last = "?"
    for attempt in range(4):
        try:
            r = requests.get(url, headers=H, timeout=120, allow_redirects=True)
            if r.status_code == 200 and r.content[:2] == b"PK":
                out = dest_dir / "supplementary"
                out.mkdir(parents=True, exist_ok=True)
                try:
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    z.extractall(out)
                    return f"s{len(z.namelist())}"
                except Exception as e:
                    return f"unzip_err:{str(e)[:25]}"
            if r.status_code == 404 or (r.status_code == 200 and not r.content):
                return "none"   # no supplementary files for this article
            if r.status_code in (500, 502, 503, 429):
                last = f"http_{r.status_code}"
                time.sleep(5 * (attempt + 1)); continue
            return f"http_{r.status_code}"
        except Exception as e:
            last = f"error:{str(e)[:30]}"
            time.sleep(5 * (attempt + 1))
    return last


def main():
    ap = argparse.ArgumentParser(description="Rescue no_url papers via NCBI+EuropePMC OA")
    ap.add_argument("--dry-run", action="store_true", help="Resolve PMCIDs only; download nothing")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=6.0,
                    help="Seconds between EuropePMC requests (serial; it rate-limits concurrency)")
    ap.add_argument("--cooldown", type=int, default=300,
                    help="Long pause (s) when throttling is detected via consecutive failures")
    ap.add_argument("--max-consec-fail", type=int, default=5,
                    help="Trip the cooldown after this many consecutive failed requests")
    ap.add_argument("--no-supplements", action="store_true",
                    help="Only fetch the PDF, skip the supplementaryFiles zip")
    args = ap.parse_args()
    want_supp = not args.no_supplements

    rows = json.load(open(NO_URL_LIST))
    if args.limit:
        rows = rows[:args.limit]
    log.info("Rescuing %d no_url papers (mode=%s, supplements=%s)",
             len(rows), "DRY-RUN" if args.dry_run else "FETCH", want_supp)

    # 1) DOI -> PMCID (batch)
    log.info("Resolving PMCIDs via NCBI ID Converter ...")
    pmcids = resolve_pmcids([r["doi"] for r in rows])
    log.info("PMCID found for %d / %d papers", len(pmcids), len(rows))

    results = []
    if args.dry_run:
        for r in rows:
            results.append({"doi": r["doi"], "publisher": r["publisher"], "dir": r["dir"],
                            "pmcid": pmcids.get(r["doi"]),
                            "status": "has_pmcid" if r["doi"] in pmcids else "no_pmcid"})
    else:
        # SERIAL, deliberately timid. EuropePMC rate-limits hard; an earlier 6-worker burst
        # left our IP penalized. So: big delay between requests, and a CIRCUIT BREAKER — if
        # we see several failures in a row (the signature of being throttled), STOP hitting
        # the service and sleep for a long cooldown before resuming. Resumable: existing PDF
        # + existing supplementary/ dir are skipped, so restarts continue where they left off.
        to_fetch = sum(1 for r in rows if pmcids.get(r["doi"]))
        log.info("Downloading %d OA papers serially (delay=%.1fs, cooldown=%ds on %d consec fails, supplements=%s)",
                 to_fetch, args.delay, args.cooldown, args.max_consec_fail, want_supp)
        TRANSIENT = ("http_500", "http_502", "http_503", "http_429")
        def _is_fail(s):
            return s in TRANSIENT or str(s).startswith("error")
        consec = 0
        done = 0
        for i, r in enumerate(rows, 1):
            doi = r["doi"]; pmcid = pmcids.get(doi)
            base = {"doi": doi, "publisher": r["publisher"], "dir": r["dir"], "pmcid": pmcid}
            if not pmcid:
                results.append({**base, "status": "no_pmcid"}); continue
            d = _dir_of(r["dir"])
            if not d.exists():
                results.append({**base, "status": "dir_missing"}); continue
            # --- PDF ---
            if _existing_pdf(d):
                st = "already"
            else:
                st = download_pdf(pmcid, d / (doi.replace("/", "_") + ".pdf"))
                time.sleep(args.delay)
            # --- supplements (separate EuropePMC call) ---
            supp_st = "skip"
            if want_supp:
                if (d / "supplementary").exists():
                    supp_st = "already"
                else:
                    supp_st = download_supplements(pmcid, d)
                    time.sleep(args.delay)
            results.append({**base, "status": st, "supp": supp_st})
            done += 1
            if i % 25 == 0 or st not in ("ok", "already", "no_pmcid"):
                log.info("[%d/%d] %s -> pdf=%s supp=%s", i, len(rows), doi[:34], st, supp_st)

            # --- circuit breaker: back right off if the service starts pushing back ---
            if _is_fail(st) or _is_fail(supp_st):
                consec += 1
            elif st in ("ok", "already") or supp_st in ("ok",) or str(supp_st).startswith("s"):
                consec = 0
            if consec >= args.max_consec_fail:
                log.warning("!! %d consecutive failures — likely throttled. Cooling down %ds before resuming.",
                            consec, args.cooldown)
                AUDIT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                time.sleep(args.cooldown)
                consec = 0
            # periodic checkpoint so progress/audit survives an interruption
            if done % 25 == 0:
                AUDIT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    AUDIT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    tally = Counter(r["status"] for r in results)
    by_pub_ok = Counter(r["publisher"] for r in results if r["status"] in ("ok", "already"))
    log.info("=" * 60)
    log.info("PMC RESCUE %s", "DRY-RUN" if args.dry_run else "complete")
    for k, v in tally.most_common():
        log.info("  pdf %-12s %d", k, v)
    if not args.dry_run:
        supp_ok = sum(1 for r in results if str(r.get("supp", "")).startswith("s") and r.get("supp") != "skip")
        supp_none = sum(1 for r in results if r.get("supp") == "none")
        log.info("supplements: %d papers got files, %d had none", supp_ok, supp_none)
        log.info("rescued PDF (ok+already) by publisher:")
        for k, v in by_pub_ok.most_common():
            log.info("    %-10s %d", k, v)
    log.info("Audit written to %s", AUDIT)


if __name__ == "__main__":
    main()
