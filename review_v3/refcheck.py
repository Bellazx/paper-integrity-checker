"""Deterministic reference-list verification against CrossRef.

Run once per paper in the background while the review sessions start; the
reference shard then adjudicates only the flagged entries instead of looping
over CrossRef itself (the measured cost of the old flow and of early v3 runs).
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

USER_AGENT = "paper-integrity-review/3 (mailto:paper-check@example.com)"
ENTRY_RE = re.compile(r"^\s*\[?(\d{1,3})\]?[.)]?\s+(?=\S)")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s;,<>\"]+", re.I)
SECTION_END = re.compile(r"^\s*(Supplementary|Supporting Information|Acknowledg|Funding|Conflicts? of interest|"
                         r"Author contributions|Abbreviations|Figure legends|Appendix|Data availability)", re.I)
STOP = {"the", "of", "and", "in", "a", "an", "for", "on", "with", "to", "by", "from", "at", "as", "via", "is"}


def parse(text: str) -> list[dict]:
    """Split a reference section into numbered entries (numbers must ascend)."""
    entries, current, expected = [], None, 1
    for line in text.splitlines():
        line = line.replace("\t", " ")
        if re.match(r"^\s*\[第\d+页\]\s*$", line) or re.match(r"^\s*(References|REFERENCES|参考文献)\s*$", line):
            continue
        if SECTION_END.match(line):
            break
        match = ENTRY_RE.match(line)
        if match and int(match.group(1)) == expected:
            current = {"num": expected, "text": line[match.end():].strip()}
            entries.append(current)
            expected += 1
        elif current is not None and line.strip() and len(current["text"]) < 1200:
            joiner = "" if current["text"].endswith("-") else " "
            current["text"] = (current["text"][:-1] if joiner == "" else current["text"]) + joiner + line.strip()
    return entries[:400]


def _words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return {w for w in re.findall(r"[a-z0-9]+", text) if w not in STOP and len(w) > 1}


def similarity(title: str, entry: str) -> float:
    title_words = _words(title)
    if not title_words:
        return 0.0
    return len(title_words & _words(entry)) / len(title_words)


def _get(url: str, cache_dir: Path) -> tuple[dict | None, str]:
    key = cache_dir / (re.sub(r"[^A-Za-z0-9]+", "_", url)[-120:] + ".json")
    if key.is_file():
        cached = json.loads(key.read_text(encoding="utf-8"))
        return cached.get("data"), cached.get("status", "ok")
    status, data = "ok", None
    for attempt in range(2):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8")).get("message")
            status = "ok"
            break
        except urllib.error.HTTPError as exc:
            status = "not_found" if exc.code == 404 else f"http_{exc.code}"
            if exc.code == 404:
                break
        except Exception as exc:  # timeouts, resets: not evidence of anything
            status = f"lookup_failed:{type(exc).__name__}"
        time.sleep(1 + attempt)
    if status in ("ok", "not_found"):
        key.write_text(json.dumps({"status": status, "data": data}, ensure_ascii=False), encoding="utf-8")
    return data, status


def _describe(work: dict) -> dict:
    authors = [a.get("family", "") for a in (work.get("author") or [])[:3]]
    issued = ((work.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return {"doi": work.get("DOI"), "title": (work.get("title") or [""])[0][:200],
            "journal": (work.get("container-title") or [""])[0][:100], "year": issued,
            "authors": authors, "volume": work.get("volume"), "page": work.get("page")}


WEB_RE = re.compile(r"\[n\.?\s*d\.?\]|Retrieved\b|Accessed\b|accessed on|访问|(?:^|\s)URL\b", re.I)
PREPRINT_RE = re.compile(r"arXiv|CoRR|preprint|bioRxiv|medRxiv|ChemRxiv|SSRN|openreview|Research Square", re.I)
NON_JOURNAL_RE = re.compile(r"\bIn\s+(?:Proceedings|Proc\.|Advances|International|Conference|Workshop|IEEE|ACM)|"
                            r"Proceedings of|Conference on|Symposium|Workshop|\bPress\b|\bed(?:s|itors?)?\.|"
                            r"Thesis|Dissertation|Technical Report|Patent|Standard|标准|专利", re.I)


def entry_type(text: str) -> str:
    if PREPRINT_RE.search(text):
        return "preprint"
    if WEB_RE.search(text) or (re.search(r"https?://", text) and not DOI_RE.search(text)):
        return "web"
    if NON_JOURNAL_RE.search(text):
        return "conference_or_book"
    return "journal"


def check_entry(entry: dict, cache_dir: Path) -> dict:
    text = entry["text"]
    kind = entry_type(text)
    out = {"num": entry["num"], "text": text[:400], "type": kind}
    if kind in ("web", "preprint") and not DOI_RE.search(text):
        # Not reliably indexed by CrossRef; a miss here says nothing about the citation.
        out["status"] = "not_indexed_type"
        return out
    doi_match = DOI_RE.search(text)
    if doi_match:
        doi = doi_match.group(0).rstrip(".,;)]")
        out["doi"] = doi
        work, status = _get("https://api.crossref.org/works/" + urllib.parse.quote(doi), cache_dir)
        tail = re.match(r"\s+([.\d][\w.\-/()]*\d)", text[doi_match.end():])
        if status == "not_found" and tail:
            # A DOI broken across lines continues in the next token.
            joined = (doi_match.group(0) + tail.group(1)).rstrip(".,;)]")
            work, status = _get("https://api.crossref.org/works/" + urllib.parse.quote(joined), cache_dir)
            if work:
                out["doi"] = doi = joined
                out["note"] = "DOI 跨行断开，拼接后可解析"
        if status == "not_found":
            # A DOI split across lines often swallows trailing page digits.
            trimmed = re.sub(r"\d{1,4}$", "", doi)
            if trimmed != doi and len(trimmed) > 10:
                work, status = _get("https://api.crossref.org/works/" + urllib.parse.quote(trimmed), cache_dir)
                if work:
                    out["note"] = f"DOI 尾部疑似粘连页码，{trimmed} 可解析"
        if status != "ok":
            out["status"] = "doi_not_found" if status == "not_found" else "lookup_failed"
            return out
        record = _describe(work)
        score = similarity(record["title"], text)
        out.update(record=record, title_similarity=round(score, 2),
                   status="ok" if score >= 0.6 else "doi_mismatch")
        return out
    query = "https://api.crossref.org/works?rows=3&query.bibliographic=" + urllib.parse.quote(text[:300])
    data, status = _get(query, cache_dir)
    if status != "ok" or not data:
        out["status"] = "lookup_failed"
        return out
    best, best_score = None, 0.0
    for work in data.get("items") or []:
        record = _describe(work)
        score = similarity(record["title"], text)
        if score > best_score:
            best, best_score = record, score
    status = "ok" if best_score >= 0.8 else ("weak_match" if best_score >= 0.5 else "no_match")
    if status != "ok" and kind == "conference_or_book":
        status = "unmatched_non_journal"  # low priority: proceedings/books are indexed unevenly
    out.update(record=best, title_similarity=round(best_score, 2), status=status)
    return out


def run(references_text: str, out_path: Path, workers: int = 6) -> dict:
    entries = parse(references_text)
    cache_dir = out_path.parent / "crossref"
    cache_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda e: check_entry(e, cache_dir), entries))
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    report = {"entries": len(entries), "counts": counts, "seconds": round(time.time() - started, 1),
              "results": results}
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    return report
