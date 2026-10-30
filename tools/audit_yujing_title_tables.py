#!/usr/bin/env python3
"""Crossref-audit title fields in the production yujing tables."""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from collections import Counter

import pymssql
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.db import DB_CONFIG
from utils.crossref import is_bad_title


TABLES = [
    "yujing",
    "yujing_4050",
    "yujing_4050_202606",
    "yujing_4050_202607",
    "yujing_quanliang",
    "yujing_quanliang_v2",
    "yujing_scholar",
    "yujing_wanbin",
]
OUT = Path("/tmp/yujing_other_title_check.json")
META_RE = re.compile(
    r"(?:https?://|doi\s*[:.]|received|accepted|abstract|keywords|copyright|\b\d+\.\.\d+\b|^[A-Z]{2,10}[_-]\d+)",
    re.I,
)
HEADERS = {
    "User-Agent": "PaperIntegrityChecker title audit/1.0 (mailto:paper-check@example.com)"
}


def norm(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "").lower()
    value = value.replace("&amp;", "&")
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def suspicious(row: dict) -> list[str]:
    title = str(row.get("title") or "").strip()
    author = str(row.get("author") or "").strip()
    journal = str(row.get("journal") or "").strip()
    reasons = []
    if not title:
        reasons.append("empty")
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in title):
        reasons.append("control")
    if "\ufffd" in title or "\uf020" in title or "\x00" in title:
        reasons.append("garbled")
    if len(title) < 8:
        reasons.append("short")
    if META_RE.search(title):
        reasons.append("metadata_pattern")
    if title.lower() in {author.lower(), journal.lower()}:
        reasons.append("equals_meta")
    return sorted(set(reasons))


def load_rows() -> list[dict]:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        rows = []
        cur = conn.cursor(as_dict=True)
        for table in TABLES:
            cur.execute(f"SELECT id,title,author,journal,doi,report_url,generation_time FROM [{table}]")
            for row in cur.fetchall():
                row = dict(row)
                row["table"] = table
                row["reasons"] = suspicious(row)
                if row["reasons"] and str(row.get("doi") or "").strip():
                    rows.append(row)
        return rows
    finally:
        conn.close()


def fetch_crossref(doi: str) -> dict:
    for attempt in range(4):
        try:
            resp = requests.get(
                "https://api.crossref.org/works/" + requests.utils.quote(doi, safe=""),
                headers=HEADERS,
                timeout=20,
            )
            if resp.status_code == 200:
                message = resp.json().get("message", {})
                title = re.sub(r"<[^>]+>", " ", (message.get("title") or [""])[0]).strip()
                return {"status": "ok", "crossref_title": title}
            if resp.status_code in {429, 500, 502, 503, 504}:
                time.sleep((attempt + 1) * 1.2)
                continue
            return {"status": f"http_{resp.status_code}", "crossref_title": ""}
        except Exception as exc:
            if attempt < 3:
                time.sleep((attempt + 1) * 1.2)
            else:
                return {"status": "error", "crossref_title": "", "error": str(exc)}
    return {"status": "error", "crossref_title": ""}


def classify(row: dict, metadata: dict) -> dict:
    result = dict(row)
    result.update(metadata)
    if metadata["status"] == "ok" and metadata["crossref_title"]:
        left, right = norm(row["title"]), norm(metadata["crossref_title"])
        ratio = SequenceMatcher(None, left, right).ratio() if left and right else 0
        left_tokens, right_tokens = set(left.split()), set(right.split())
        jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens) if left_tokens | right_tokens else 0
        result["ratio"] = round(ratio, 4)
        result["jaccard"] = round(jaccard, 4)
        if ratio < 0.3 and jaccard < 0.25:
            result["category"] = "high_confidence_wrong"
        elif ratio >= 0.8 or jaccard >= 0.7:
            result["category"] = "likely_valid_or_suffix"
        else:
            result["category"] = "needs_manual_review"
    else:
        result["ratio"] = 0
        result["jaccard"] = 0
        result["category"] = "crossref_unavailable"
    return result


def main() -> None:
    rows = load_rows()
    unique_dois = sorted({str(r["doi"]).strip() for r in rows})
    metadata = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_crossref, doi): doi for doi in unique_dois}
        for index, future in enumerate(as_completed(futures), 1):
            metadata[futures[future]] = future.result()
            if index % 100 == 0:
                print(f"Crossref {index}/{len(futures)}", flush=True)
    results = [classify(r, metadata[str(r["doi"]).strip()]) for r in rows]
    results.sort(key=lambda r: (r["table"], int(r["id"])))
    summary = Counter(r["category"] for r in results)
    output = {"tables": TABLES, "candidate_rows": len(rows), "unique_dois": len(unique_dois), "summary": dict(summary), "rows": results}
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"candidate_rows": len(rows), "unique_dois": len(unique_dois), "summary": dict(summary), "output": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
