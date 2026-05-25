import re
import time
import logging
import difflib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import (
    CROSSREF_API_BASE, CROSSREF_MAILTO,
    REF_TITLE_SIMILARITY_THRESHOLD, CROSSREF_RATE_LIMIT_DELAY,
)
from utils.pdf_utils import extract_full_text

_crossref_semaphore = threading.Semaphore(30)

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": f"PaperIntegrityChecker/1.0 (mailto:{CROSSREF_MAILTO})",
}


def _extract_references(full_text: str) -> list[dict]:
    """Extract individual references from the reference section of a paper."""
    ref_section_pattern = re.compile(
        r'(?:^|\n)\s*(?:References|Bibliography|Works\s+Cited|REFERENCES)\s*\n',
        re.IGNORECASE,
    )
    match = ref_section_pattern.search(full_text)
    if not match:
        log.warning("Could not locate reference section in text")
        return []

    ref_text = full_text[match.end():]
    ref_text = ref_text[:20000]

    ref_pattern = re.compile(r'^\s*\[?(\d{1,3})\]?[.\s]+(.+?)(?=\n\s*\[?\d{1,3}\]?[.\s]|\Z)', re.MULTILINE | re.DOTALL)
    refs = []

    for m in ref_pattern.finditer(ref_text):
        num = int(m.group(1))
        text = m.group(2).strip()
        text = re.sub(r'\s+', ' ', text)

        doi = None
        doi_match = re.search(r'(?:doi[:\s]*|https?://doi\.org/)(10\.\d{4,}/[^\s,;]+)', text, re.IGNORECASE)
        if doi_match:
            doi = doi_match.group(1).rstrip('.')

        refs.append({
            "number": num,
            "text": text,
            "doi": doi,
        })

    if not refs:
        lines = ref_text.strip().split('\n')
        for i, line in enumerate(lines):
            line = line.strip()
            if len(line) < 20:
                continue
            doi = None
            doi_match = re.search(r'(?:doi[:\s]*|https?://doi\.org/)(10\.\d{4,}/[^\s,;]+)', line, re.IGNORECASE)
            if doi_match:
                doi = doi_match.group(1).rstrip('.')
            refs.append({"number": i + 1, "text": line, "doi": doi})

    log.info("Extracted %d references", len(refs))
    return refs


def _verify_by_doi(doi: str) -> dict:
    """Verify a reference by DOI lookup in CrossRef."""
    try:
        resp = requests.get(
            f"{CROSSREF_API_BASE}/works/{doi}",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            work = resp.json()["message"]
            return {
                "doi_valid": True,
                "crossref_title": (work.get("title") or [None])[0],
                "crossref_authors": [a.get("family", "") for a in work.get("author", [])],
                "crossref_journal": (work.get("container-title") or [None])[0],
                "crossref_year": work.get("published-print", {}).get("date-parts", [[None]])[0][0]
                                 or work.get("published-online", {}).get("date-parts", [[None]])[0][0],
            }
        elif resp.status_code == 404:
            return {"doi_valid": False, "error": "DOI not found in CrossRef"}
    except Exception as e:
        log.warning("CrossRef DOI lookup failed for %s: %s", doi, e)
    return {"doi_valid": False, "error": "lookup_failed"}


def _is_truncated_doi(doi: str) -> bool:
    """Detect DOIs that appear truncated due to PDF text extraction issues."""
    if doi.endswith('-') or doi.endswith('/') or doi.endswith('.'):
        return True
    suffix = doi.split('/', 1)[-1] if '/' in doi else ''
    # Journal-only prefixes without article ID (e.g., "j.1749-" ending)
    if suffix.endswith('-'):
        return True
    # Ends with incomplete parenthetical like "s0092-8674(01)"
    if re.search(r'\(\d{2,4}\)$', suffix):
        return True
    # Very short suffix that's likely just a journal abbreviation (e.g., "jbmr", "anie")
    if len(suffix) <= 6 and not re.search(r'\d', suffix):
        return True
    # Suffix is purely alphabetic/dot with no article number (e.g., "journal.pbio", "jci.insight")
    if not re.search(r'\d', suffix):
        return True
    # Suffix ends with partial ISSN-like pattern (e.g., "j.1749-" -> "j.1749")
    if re.search(r'\.\d{3,4}$', suffix) and not re.search(r'\.\d{3,4}\.\d', suffix):
        return True
    return False


def _verify_by_text(ref_text: str) -> dict:
    """Verify a reference by bibliographic text search in CrossRef."""
    try:
        query = ref_text[:300]
        resp = requests.get(
            f"{CROSSREF_API_BASE}/works",
            params={"query.bibliographic": query, "rows": 1},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            items = resp.json()["message"]["items"]
            if items:
                best = items[0]
                return {
                    "found": True,
                    "matched_doi": best.get("DOI"),
                    "crossref_title": (best.get("title") or [None])[0],
                    "crossref_score": best.get("score", 0),
                }
    except Exception as e:
        log.warning("CrossRef text search failed: %s", e)
    return {"found": False, "error": "no_match"}


def _compare_titles(ref_text: str, crossref_title: str | None) -> float:
    """Compute title similarity between reference text and CrossRef result."""
    if not crossref_title:
        return 0.0
    cr_clean = re.sub(r'<[^>]+>', '', crossref_title)
    cr_clean = re.sub(r'\s+', ' ', cr_clean).strip()
    ref_lower = ref_text.lower()
    cr_lower = cr_clean.lower()
    if cr_lower in ref_lower:
        return 1.0
    return difflib.SequenceMatcher(None, ref_lower[:200], cr_lower).ratio()


def _is_garbled_text(text: str) -> bool:
    """Detect PDF extraction artifacts: concatenated words without spaces."""
    if len(text) < 20:
        return False
    words = text.split()
    if not words:
        return True
    avg_word_len = sum(len(w) for w in words) / len(words)
    space_ratio = text.count(' ') / len(text)
    long_word_ratio = sum(1 for w in words if len(w) > 15) / len(words)
    return avg_word_len > 20 or space_ratio < 0.04 or long_word_ratio > 0.5


def _verify_one_ref(ref: dict) -> dict | None:
    """Verify a single reference against CrossRef. Returns an issue dict or None."""
    with _crossref_semaphore:
        if ref["doi"]:
            result = _verify_by_doi(ref["doi"])
            if result.get("doi_valid"):
                title_sim = _compare_titles(ref["text"], result.get("crossref_title"))
                if title_sim < REF_TITLE_SIMILARITY_THRESHOLD:
                    if _is_garbled_text(ref["text"]):
                        severity = "low"
                    elif title_sim < 0.5:
                        severity = "high"
                    else:
                        severity = "medium"
                    return {
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "title_mismatch",
                        "severity": severity,
                        "verified": True,
                        "details": {
                            "doi": ref["doi"],
                            "title_similarity": round(title_sim, 3),
                            "crossref_title": result.get("crossref_title"),
                        },
                        "description": f"Reference #{ref['number']}: DOI exists but title similarity is low "
                                       f"({title_sim:.1%})",
                    }
                return {"_ok": True}
            else:
                if _is_truncated_doi(ref["doi"]):
                    return {
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "doi_truncated",
                        "severity": "low",
                        "verified": False,
                        "details": {"doi": ref["doi"], "error": "DOI appears truncated (PDF extraction artifact)"},
                        "description": f"Reference #{ref['number']}: DOI '{ref['doi']}' appears truncated "
                                       f"(likely PDF text extraction issue)",
                    }
                else:
                    return {
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "doi_not_found",
                        "severity": "high",
                        "verified": False,
                        "details": {"doi": ref["doi"], "error": result.get("error")},
                        "description": f"Reference #{ref['number']}: DOI '{ref['doi']}' not found in CrossRef",
                    }
        else:
            result = _verify_by_text(ref["text"])
            if result.get("found"):
                title_sim = _compare_titles(ref["text"], result.get("crossref_title"))
                if result.get("crossref_score", 0) < 50:
                    if _is_garbled_text(ref["text"]):
                        severity = "low"
                    elif title_sim < 0.5 and result.get("crossref_score", 0) < 45:
                        severity = "high"
                    else:
                        severity = "medium"
                    return {
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "low_match_score",
                        "severity": severity,
                        "verified": True,
                        "details": {
                            "matched_doi": result.get("matched_doi"),
                            "crossref_score": result.get("crossref_score"),
                            "title_similarity": round(title_sim, 3),
                        },
                        "description": f"Reference #{ref['number']}: Low confidence match in CrossRef "
                                       f"(score={result.get('crossref_score', 0):.1f})",
                    }
                return {"_ok": True}
            else:
                return {
                    "ref_number": ref["number"],
                    "ref_text": ref["text"][:200],
                    "issue_type": "not_found",
                    "severity": "medium",
                    "verified": False,
                    "details": {},
                    "description": f"Reference #{ref['number']}: Could not verify in CrossRef (no DOI, no text match)",
                }


def check_references(pdf_path: str) -> list[dict]:
    """Extract and verify all references in a paper."""
    full_text = extract_full_text(pdf_path)
    if not full_text.strip():
        log.warning("No text extracted from PDF")
        return []

    refs = _extract_references(full_text)
    if not refs:
        log.warning("No references extracted")
        return []

    issues = []
    verified_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_verify_one_ref, ref): ref for ref in refs}
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            if result.get("_ok"):
                verified_count += 1
                continue
            if result.pop("verified", False):
                verified_count += 1
            else:
                failed_count += 1
            issues.append(result)

    log.info("Reference verification: %d total, %d verified, %d failed, %d issues",
             len(refs), verified_count, failed_count, len(issues))

    return issues
