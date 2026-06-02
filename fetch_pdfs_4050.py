#!/usr/bin/env python3
"""Download the original article PDF for each paper in data/input/4050-matched/.

Why
---
1,333 of the ready dirs are Nature HTML-only: the HTML analysis path
(core/nature_adapter.py) extracts images ONLY from extended_data/ supplements, so the
main-article figures are never checked (verified: a sample paper recorded 0 images via
HTML but its main PDF yields 4). And 2,117 "not-ready" dirs have only a manifest — but
their DOIs still resolve to downloadable PDFs. Fetching the main PDF lets us re-analyze
through the standard PDF pipeline (screen_4050.py --prefer-pdf), gaining main-text figures
and full-text reference extraction.

PDF URL resolution (best source first)
  1. `citation_pdf_url` <meta> in the dir's article.html (present in 100% of sampled Nature HTML).
  2. Publisher pattern by DOI prefix:
       Nature 10.1038 -> https://www.nature.com/articles/<article_id>.pdf
  3. Fallback: resolve https://doi.org/<doi>, then scrape citation_pdf_url from the landing page.

Validates the response is a real PDF (Content-Type + %PDF magic). Writes to
<paper_dir>/<doi_slug>.pdf and skips dirs that already contain a *.pdf. Audit log:
data/output/yujing_4050_pdf_fetch.json.

Usage
  python fetch_pdfs_4050.py --dry-run --limit 20
  python fetch_pdfs_4050.py --limit 50 --publisher nature
  python fetch_pdfs_4050.py --workers 6            # all publishers, full run
"""
import argparse
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
INPUT_BASE = BASE / "data" / "input" / "4050-matched"
AUDIT_PATH = BASE / "data" / "output" / "yujing_4050_pdf_fetch.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/pdf,text/html,*/*"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fetch-pdf-4050")

# Per-host politeness: serialize a tiny delay so we don't hammer one publisher.
_host_locks: dict[str, threading.Lock] = {}
_host_locks_guard = threading.Lock()


def _host_gate(url: str):
    host = re.sub(r"^https?://([^/]+).*", r"\1", url) or "?"
    with _host_locks_guard:
        lk = _host_locks.setdefault(host, threading.Lock())
    return lk


def _doi_of(d: Path) -> str:
    f = d / "doi.txt"
    if f.exists():
        t = f.read_text(encoding="utf-8").strip()
        if t:
            return t
    name = d.name.split("_", 1)[1] if "_" in d.name else d.name
    return name.replace("__", "/")


def _existing_pdf(d: Path):
    pdfs = list(d.glob("*.pdf")) + list(d.glob("*.PDF"))
    return pdfs[0] if pdfs else None


def _citation_pdf_url(d: Path) -> str | None:
    html = d / "article.html"
    if not html.exists():
        html = d / "html" / "article.html"
    if not html.exists():
        return None
    try:
        text = html.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']', text, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']', text, re.I)
    return m.group(1) if m else None


def _publisher_url(doi: str) -> str | None:
    if doi.startswith("10.1038/"):
        return f"https://www.nature.com/articles/{doi.split('/', 1)[1]}.pdf"
    return None


def _scrape_pdf_url(doi: str) -> str | None:
    """Resolve doi.org and scrape citation_pdf_url from the landing page (last resort)."""
    try:
        r = requests.get(f"https://doi.org/{doi}", headers=HEADERS, timeout=30, allow_redirects=True)
        if r.status_code == 200 and "html" in r.headers.get("Content-Type", ""):
            m = re.search(r'<meta[^>]+citation_pdf_url[^>]+content=["\']([^"\']+)["\']', r.text, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _resolve_url(d: Path, doi: str, publisher_only: str | None) -> str | None:
    # Nature-only mode: skip non-Nature DOIs entirely.
    if publisher_only == "nature" and not doi.startswith("10.1038/"):
        return None
    return _citation_pdf_url(d) or _publisher_url(doi) or (None if publisher_only == "nature" else _scrape_pdf_url(doi))


def _download(url: str) -> tuple[bytes | None, str]:
    with _host_gate(url):
        time.sleep(0.3)  # polite per-host spacing
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, allow_redirects=True)
        except Exception as e:
            return None, f"error:{e}"
    if r.status_code != 200:
        return None, f"http_{r.status_code}"
    ct = r.headers.get("Content-Type", "")
    if not r.content.startswith(b"%PDF"):
        return None, f"not_pdf(ct={ct[:40]})"
    return r.content, "ok"


def process_one(d: Path, dry_run: bool, publisher_only: str | None) -> dict:
    doi = _doi_of(d)
    res = {"dir": d.name, "doi": doi, "status": "?"}
    existing = _existing_pdf(d)
    if existing:
        res["status"] = "already"
        res["path"] = str(existing)
        return res
    url = _resolve_url(d, doi, publisher_only)
    res["url"] = url
    if not url:
        res["status"] = "no_url"
        return res
    if dry_run:
        res["status"] = "would_fetch"
        return res
    content, why = _download(url)
    if content is None:
        res["status"] = why
        return res
    out = d / (doi.replace("/", "_") + ".pdf")
    try:
        out.write_bytes(content)
    except Exception as e:
        res["status"] = f"write_error:{e}"
        return res
    res["status"] = "ok"
    res["path"] = str(out)
    res["bytes"] = len(content)
    return res


def main():
    ap = argparse.ArgumentParser(description="Download original PDFs for 4050-matched papers")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--publisher", choices=["nature", "all"], default="all",
                    help="nature = only 10.1038 (highest success); all = attempt every publisher")
    args = ap.parse_args()

    pub = "nature" if args.publisher == "nature" else None
    dirs = sorted([d for d in INPUT_BASE.iterdir() if d.is_dir()])
    if args.limit:
        dirs = dirs[:args.limit]
    log.info("Scanning %d dirs (publisher=%s, mode=%s)", len(dirs), args.publisher,
             "DRY-RUN" if args.dry_run else "FETCH")

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, d, args.dry_run, pub): d for d in dirs}
        for i, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if i % 50 == 0 or r["status"] not in ("already", "ok", "would_fetch"):
                log.info("[%d/%d] %s -> %s", i, len(dirs), r["doi"][:34], r["status"])

    from collections import Counter
    tally = Counter(r["status"] for r in results)
    AUDIT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("=" * 60)
    log.info("PDF FETCH done in %.0fs over %d dirs", time.time() - t0, len(dirs))
    for k, v in tally.most_common():
        log.info("  %-22s %d", k, v)
    log.info("Audit written to %s", AUDIT_PATH)


if __name__ == "__main__":
    main()
