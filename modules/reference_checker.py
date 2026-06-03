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

# Process-wide cap on concurrent CrossRef calls. Kept modest so that several batch
# workers combined stay within CrossRef's polite-pool limits and avoid rate-limit
# (429) responses that would otherwise be misread as missing DOIs.
_crossref_semaphore = threading.Semaphore(8)

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": f"PaperIntegrityChecker/1.0 (mailto:{CROSSREF_MAILTO})",
}


def _scrub_running_headers(text: str, paper_doi: str | None = None) -> str:
    """Strip per-page running headers/footers that PDF extraction interleaves into text.

    Many publisher PDFs (Nature, IJB, eCM, JEM, JCB, ...) print on every page a header
    like 'Article https://doi.org/<this paper's DOI>' and a footer like
    '<Journal> | Volume NN | <date> | <pages>'. When extraction merges one of these into
    the reference section, the DOI regex in _extract_references picks up the PAPER'S OWN
    DOI, the citation is then "verified" against the paper itself, and a spurious
    title_mismatch (paper title vs. a different cited work) is reported. Removing these
    lines/fragments first prevents that at the root.
    """
    line_pats = [
        re.compile(r'^\s*Article\s+https?://doi\.org/\S+', re.IGNORECASE),
        re.compile(r'^\s*https?://doi\.org/\S+\s*$', re.IGNORECASE),
        re.compile(r'.*\|\s*Volume\s+\d+\s*\|', re.IGNORECASE),        # journal | Volume NN | ... footer
        re.compile(r"^\s*Publisher.s note\b", re.IGNORECASE),
        re.compile(r'^\s*Springer Nature (?:remains|or its licensor)', re.IGNORECASE),
        # Cookie / privacy-consent banners that some publisher PDFs (e.g. Springer Cellular
        # Oncology) inject mid-reference, corrupting the citation text they land in.
        re.compile(r'(?i)(?:see our )?privacy policy'),
        re.compile(r'(?i)\buse of your personal data\b'),
        re.compile(r'(?i)\bto change your choices\b'),
        re.compile(r'(?i)\bfor further information and to change\b'),
        re.compile(r'(?i)\bcookie(?:s| settings| policy)\b'),
    ]
    kept = [ln for ln in text.split('\n') if not any(p.search(ln) for p in line_pats)]
    text = '\n'.join(kept)
    # Headers/footers can also be merged mid-line onto a reference; strip those fragments.
    if paper_doi:
        text = re.sub(r'(?i)\bArticle\s+https?://doi\.org/' + re.escape(paper_doi), ' ', text)
    text = re.sub(r'(?i)[A-Z][A-Za-z.&\- ]{2,40}\|\s*Volume\s+\d+\s*\|[^\n]{0,60}', ' ', text)
    # Springer consent-banner phrases injected mid-citation (repeat dozens of times per PDF).
    banner_fragments = [
        r'Your privacy, your choice',
        r'We and our \d+ partners[^.]*\.',
        r'transfers to third parties[^.]*\.',
        r'Some third parties are outside[^.]*\.',
        r'(?:with varying|varying)? ?standards of data protection[^.]*\.',
        r'advertising, personal, ?personalisation of content[^.]*\.',
        r'advertising, personalisation of content[^.]*\.',
        r'usage analysis,? and social[^.]*\.',
        r'See our privacy policy[^.]*\.',
        r'for further information and to change your choices[^.]*\.',
    ]
    for frag in banner_fragments:
        text = re.sub('(?i)' + frag, ' ', text)
    return text


def _extract_references(full_text: str, paper_doi: str | None = None) -> list[dict]:
    """Extract individual references from the reference section of a paper."""
    full_text = _scrub_running_headers(full_text, paper_doi)
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

    # Reference lists are monotonically numbered. The [:20000] window can overshoot the
    # bibliography into trailing matter (methods, equations, "mM MgCl2 ...") whose stray
    # leading digits the pattern then mis-parses as new references — producing garbage
    # entries (e.g. an equation fragment that text-matches an unrelated CrossRef DOI).
    # Track the running max ref number and stop once numbering collapses backward, which
    # marks the end of the genuine reference list.
    max_num = 0
    for m in ref_pattern.finditer(ref_text):
        num = int(m.group(1))
        # Allow tiny non-increases (duplicate/OCR jitter) but bail on a real backward jump.
        if num <= max_num - 3:
            break
        text = m.group(2).strip()
        text = re.sub(r'\s+', ' ', text)
        max_num = max(max_num, num)

        doi = None
        doi_match = re.search(r'(?:doi[:\s]*|https?://doi\.org/)(10\.\d{4,}/[^\s,;]+)', text, re.IGNORECASE)
        if doi_match:
            doi = doi_match.group(1).rstrip(').].,;:')
        # A reference can never legitimately cite the paper it appears in: a self-DOI is a
        # running-header artifact, not the citation's DOI. Drop it so the ref is verified by
        # text search instead of yielding a false title_mismatch against the paper itself.
        if doi and paper_doi and doi.lower() == paper_doi.lower():
            doi = None

        refs.append({
            "number": num,
            "text": text,
            "doi": doi,
        })

    if not refs:
        # Author-first (non-numbered) reference lists — common in Frontiers / eCM / many
        # Elsevier journals. The raw text wraps each citation across several lines, so we
        # must GROUP lines into whole references rather than treat each line as one (the
        # latter shatters every citation into fragments that then mis-verify).
        refs = _group_unnumbered_refs(ref_text, paper_doi)

    log.info("Extracted %d references", len(refs))
    return refs


def _looks_like_ref_start(line: str) -> bool:
    """Heuristic: does this line begin a new author-first citation?

    Author-first citations start with a surname then an initial/comma, e.g.
    'Smith, L. J., ...', 'Wang, Y., Wan, C., ...', 'van der Berg, A. ...'.
    """
    return bool(re.match(r'^[A-Z][A-Za-z\'`-]+,?\s+[A-Z]', line) or
                re.match(r'^(?:van|von|de|del|della|di|da|le|la)\s+[A-Z]', line))


def _ref_looks_complete(buf: str) -> bool:
    """Heuristic: does the accumulated buffer look like a finished citation?

    A citation is 'closeable' once it carries a DOI, or a year plus a page/volume tail.
    """
    if re.search(r'(?:doi[:\s]|https?://doi\.org/)\s*10\.\d{4,}/', buf, re.IGNORECASE):
        return True
    # '(2010). ... 196-204.' or '... 109 (1), 196-204.' style endings with a year present.
    if re.search(r'\(\d{4}\)', buf) and re.search(r'\d+\s*[-–]\s*\d+\.?\s*$', buf):
        return True
    return False


def _group_unnumbered_refs(ref_text: str, paper_doi: str | None) -> list[dict]:
    """Group wrapped lines of an author-first reference list into whole references."""
    lines = [ln.strip() for ln in ref_text.strip().split('\n') if ln.strip()]
    grouped: list[str] = []
    buf = ""
    for line in lines:
        if buf and _looks_like_ref_start(line) and _ref_looks_complete(buf):
            grouped.append(buf)
            buf = line
        else:
            buf = f"{buf} {line}".strip() if buf else line
    if buf:
        grouped.append(buf)

    refs = []
    for i, text in enumerate(grouped):
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) < 20:
            continue
        doi = None
        doi_match = re.search(r'(?:doi[:\s]*|https?://doi\.org/)(10\.\d{4,}/[^\s,;]+)', text, re.IGNORECASE)
        if doi_match:
            doi = doi_match.group(1).rstrip(').].,;:')
        if doi and paper_doi and doi.lower() == paper_doi.lower():
            doi = None
        refs.append({"number": i + 1, "text": text, "doi": doi})
    return refs


def _verify_by_doi(doi: str) -> dict:
    """Verify a reference by DOI lookup in CrossRef.

    Distinguishes three outcomes:
      - doi_valid=True                          : DOI resolved
      - doi_valid=False, error='not_found'      : CrossRef returned 404 (genuine)
      - doi_valid=False, error='lookup_failed'  : transient (timeout / 429 / 5xx) —
                                                  NOT evidence the DOI is fake.
    Retries transient failures with backoff so rate-limiting during batch runs does
    not masquerade as reference fabrication.
    """
    last_status = None
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{CROSSREF_API_BASE}/works/{doi}",
                headers=HEADERS,
                timeout=20,
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
            if resp.status_code == 404:
                return {"doi_valid": False, "error": "not_found"}
            # 429 / 500 / 502 / 503 → transient, retry with backoff
            last_status = resp.status_code
        except Exception as e:
            log.warning("CrossRef DOI lookup error for %s (attempt %d): %s", doi, attempt + 1, e)
            last_status = "exception"
        time.sleep(1.5 * (attempt + 1))
    log.warning("CrossRef DOI lookup failed for %s after retries (last=%s)", doi, last_status)
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
    """Verify a reference by bibliographic text search in CrossRef.

    Retries transient failures (429 / 5xx / timeout) up to 3 times with
    exponential backoff, mirroring _verify_by_doi().
    """
    query = ref_text[:300]
    last_status = None
    for attempt in range(3):
        try:
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
                return {"found": False, "error": "no_match"}
            if resp.status_code == 404:
                return {"found": False, "error": "no_match"}
            last_status = resp.status_code
        except Exception as e:
            log.warning("CrossRef text search error (attempt %d): %s", attempt + 1, e)
            last_status = "exception"
        time.sleep(1.5 * (attempt + 1))
    log.warning("CrossRef text search exhausted retries (last status: %s)", last_status)
    return {"found": False, "error": "lookup_failed"}


def _normalize_for_match(s: str) -> str:
    """Lowercase, strip HTML tags/entities and punctuation, collapse whitespace."""
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'&[a-z]+;', ' ', s)          # HTML entities (&amp; &lt; ...)
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)       # drop punctuation
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _compare_titles(ref_text: str, crossref_title: str | None) -> float:
    """Compute title similarity between reference text and CrossRef result.

    A reference citation lists authors, title, journal, year — often authors FIRST.
    So the CrossRef title is frequently a substring located in the MIDDLE of ref_text,
    not at its start. A naive SequenceMatcher over the whole ref_text (dominated by the
    author list) therefore scores legitimate citations low. We instead:
      1) substring check on normalized text (handles punctuation/HTML/case), and
      2) token-coverage: what fraction of the title's words appear in ref_text,
         plus a sliding best-window SequenceMatcher.
    Returns the max of these signals so a real (but authors-first) citation scores high.
    """
    if not crossref_title:
        return 0.0
    cr_clean = re.sub(r'<[^>]+>', '', crossref_title)
    cr_clean = re.sub(r'\s+', ' ', cr_clean).strip()
    ref_lower = ref_text.lower()
    cr_lower = cr_clean.lower()
    if cr_lower and cr_lower in ref_lower:
        return 1.0

    cr_norm = _normalize_for_match(cr_clean)
    ref_norm = _normalize_for_match(ref_text)
    if not cr_norm:
        return 0.0

    # 1) Normalized substring (handles punctuation / HTML-entity / spacing differences)
    if cr_norm and cr_norm in ref_norm:
        return 1.0

    # 2) Title-token coverage: fraction of title words present in the reference text.
    cr_tokens = [t for t in cr_norm.split() if len(t) > 2]
    coverage = 0.0
    if cr_tokens:
        ref_token_set = set(ref_norm.split())
        coverage = sum(1 for t in cr_tokens if t in ref_token_set) / len(cr_tokens)

    # 3) Best-window SequenceMatcher: slide a title-length window across ref_text
    #    so the author prefix does not drag the ratio down.
    best_ratio = difflib.SequenceMatcher(None, ref_norm[:200], cr_norm).ratio()
    if cr_norm and len(ref_norm) > len(cr_norm):
        win = len(cr_norm)
        step = max(1, win // 4)
        for i in range(0, min(len(ref_norm) - win, 600) + 1, step):
            r = difflib.SequenceMatcher(None, ref_norm[i:i + win], cr_norm).ratio()
            if r > best_ratio:
                best_ratio = r

    return max(coverage, best_ratio)


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


def _is_non_crossref_doi(doi: str) -> bool:
    """DOIs registered outside CrossRef (so a CrossRef 404 is NOT evidence of fabrication).

    The most common in our corpus is arXiv (10.48550/arXiv.*), registered with DataCite;
    these resolve fine via doi.org but always 404 in CrossRef's /works endpoint. A handful
    of other DataCite/preprint prefixes behave the same.
    """
    d = doi.lower()
    prefixes = (
        "10.48550/",   # arXiv
        "10.5281/",    # Zenodo
        "10.6084/",    # figshare
        "10.5061/",    # Dryad
    )
    return d.startswith(prefixes) or "arxiv" in d


def _is_titleless_citation(text: str) -> bool:
    """Detect Vancouver-style citations that omit the article title entirely.

    Some journals (e.g. Springer Cellular Oncology) cite as
        'Authors. Journal Abbrev. Vol, pages (year). https://doi.org/...'
    with NO article title. When such a DOI resolves, comparing the CrossRef title
    against text that contains no title always scores low — a FALSE title_mismatch.

    Signature: an author list (initials + surnames, comma-separated) runs straight into
    a 'Journal Abbrev. Vol, pages (year)' tail with no intervening title. We strip the
    DOI/URL, the author tokens (both 'X.Y. Surname' and 'Surname, X. Y.' orders), and the
    journal+volume+pages+year tail; a real title leaves a multi-word remainder, a
    title-less citation leaves essentially nothing.
    """
    t = re.sub(r'https?://\S+|doi[:\s]*10\.\S+', ' ', text, flags=re.IGNORECASE)
    # Cut the journal/volume/pages/year tail and everything after it (titles precede it).
    m = re.search(r'\b\d{1,4}\s*,\s*[\dA-Za-z]+\s*[-–]?\s*[\dA-Za-z]*\s*\(\d{4}\)', t)
    if m:
        t = t[:m.start()]
    else:
        t = re.sub(r'\(\d{4}\).*$', ' ', t)
    # Remove author tokens in 'X.Y. Surname' / 'X. Surname' / 'van der Surname' order.
    t = re.sub(r'(?:[A-Z]\.[-\s]*){1,4}\s*(?:van der |von |de |del |della |di |le |la )?[A-Z][A-Za-z\'`-]+', ' ', t)
    # Remove author tokens in 'Surname, X. Y.' order.
    t = re.sub(r"[A-Z][A-Za-z'`-]+,\s*(?:[A-Z]\.[-\s]*){1,4}", ' ', t)
    # Drop leftover initials, journal-abbrev dots, and standalone numbers.
    t = re.sub(r'\b(?:[A-Z]\.)+|\b[A-Z][a-z]{0,3}\.|\b\d+\b', ' ', t)
    # A real article title leaves several content words; a title-less one leaves <=2.
    words = re.findall(r'[A-Za-z]{4,}', t)
    return len(words) <= 2


def _verify_one_ref(ref: dict, paper_doi: str | None = None) -> dict | None:
    """Verify a single reference against CrossRef. Returns an issue dict or None."""
    with _crossref_semaphore:
        if ref["doi"]:
            result = _verify_by_doi(ref["doi"])
            if result.get("doi_valid"):
                title_sim = _compare_titles(ref["text"], result.get("crossref_title"))
                if title_sim < REF_TITLE_SIMILARITY_THRESHOLD:
                    # A resolving DOI on a title-LESS citation (Vancouver style, no article
                    # title) cannot be a title mismatch — there is no title to compare. The
                    # resolved DOI is itself the verification. Treat as OK.
                    if _is_titleless_citation(ref["text"]):
                        return {"_ok": True}
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
                elif result.get("error") == "lookup_failed":
                    # Transient CrossRef failure (timeout / rate-limit) — NOT evidence of
                    # fabrication. Record as low-severity unverified so it never drives risk.
                    return {
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "doi_unverified",
                        "severity": "low",
                        "verified": False,
                        "details": {"doi": ref["doi"], "error": "CrossRef lookup failed (transient)"},
                        "description": f"Reference #{ref['number']}: DOI '{ref['doi']}' could not be "
                                       f"verified (CrossRef temporarily unavailable)",
                    }
                else:
                    if _is_non_crossref_doi(ref["doi"]):
                        # arXiv/Zenodo/etc. are registered outside CrossRef; a 404 here means
                        # "not in CrossRef", NOT a fabricated DOI. Record as low/unverified.
                        return {
                            "ref_number": ref["number"],
                            "ref_text": ref["text"][:200],
                            "issue_type": "doi_unverified",
                            "severity": "low",
                            "verified": False,
                            "details": {"doi": ref["doi"], "error": "DOI registered outside CrossRef (e.g. arXiv/DataCite)"},
                            "description": f"Reference #{ref['number']}: DOI '{ref['doi']}' is a preprint/dataset "
                                           f"DOI not indexed by CrossRef (not verifiable here)",
                        }
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
                # If text search resolves to the paper's OWN DOI, the "reference" text is
                # really the paper's own title/metadata pulled in by extraction (header or
                # title block), not a low-confidence citation. Not an issue.
                matched = (result.get("matched_doi") or "").lower()
                if paper_doi and matched and matched == paper_doi.lower():
                    return {"_ok": True}
                title_sim = _compare_titles(ref["text"], result.get("crossref_title"))
                if result.get("crossref_score", 0) < 50:
                    if _is_garbled_text(ref["text"]):
                        severity = "low"
                    elif _is_titleless_citation(ref["text"]):
                        # No title in the citation → a weak text match can't be a title
                        # mismatch. Keep it visible for review but not high-risk.
                        severity = "medium"
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


def check_references(pdf_path: str, paper_doi: str | None = None) -> list[dict]:
    """Extract and verify all references in a paper.

    paper_doi (the DOI of the paper being checked) is used to discard running-header
    artifacts so a page-header DOI is never mistaken for a citation's DOI.
    """
    full_text = extract_full_text(pdf_path)
    if not full_text.strip():
        log.warning("No text extracted from PDF")
        return []

    refs = _extract_references(full_text, paper_doi)
    if not refs:
        log.warning("No references extracted")
        return []

    issues = []
    verified_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_verify_one_ref, ref, paper_doi): ref for ref in refs}
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
