"""Conservative content-level citation checks for short scholarly commentaries."""
from __future__ import annotations

from functools import lru_cache
import logging
import re
from pathlib import Path

import fitz
import requests
from bs4 import BeautifulSoup

from modules.reference_checker import _verify_by_text


log = logging.getLogger(__name__)

_SHORT_ARTICLE_MARKERS = re.compile(
    r"(?i)\b(?:correspondence|commentary|perspective|viewpoint|letter)\b"
)
_SUBTYPE_TOKEN = re.compile(r"\b[A-Z]{1,5}\d{1,3}\b")
_ENTITY_PATTERNS = {
    "macrophage": re.compile(r"(?i)\b(?:macrophages?|LAMs?)\b"),
    "adipocyte": re.compile(r"(?i)\b(?:adipocytes?|fat\s+cells?)\b"),
}
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PaperIntegrityChecker/1.0)"}


def _clean(value: str) -> str:
    value = str(value or "").replace("\xad", "")
    return re.sub(r"\s+", " ", value).strip()


def _body_text(document: fitz.Document) -> str:
    blocks = []
    for page in document:
        for block in page.get_text("blocks", sort=True):
            text = block[4]
            if re.search(r"(?i)^\s*references\s*$", text):
                continue
            blocks.append(text)
    return _clean("\n".join(blocks))


def _layout_references(document: fitz.Document) -> list[dict]:
    """Recover references that continue from one PDF column into the next."""
    chunks = []
    started = False
    for page_number, page in enumerate(document, 1):
        blocks = [block for block in page.get_text("blocks", sort=False) if block[4].strip()]
        header = next(
            (block for block in blocks if re.fullmatch(r"\s*REFERENCES\s*", block[4], re.I)),
            None,
        )
        if header:
            started = True
            hx, hy = header[0], header[1]
            selected = [
                block for block in blocks
                if block[0] >= hx - 5 and (block[0] > hx + 80 or block[1] > hy)
            ]
        elif started:
            selected = [block for block in blocks if block[1] < page.rect.height - 55]
        else:
            continue
        # Reading order for journal columns is column first, then vertical position.
        selected.sort(key=lambda block: (round(block[0] / 120), block[1], block[0]))
        chunks.extend(block[4] for block in selected)

    text = "\n".join(chunks).replace("\xad", "")
    entries = []
    matches = list(re.finditer(r"(?m)^\s*(\d{1,3})\.\s+", text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        citation = _clean(text[match.end():end])
        if len(citation) >= 30:
            entries.append({"number": int(match.group(1)), "text": citation})
    return entries


def _types_near_token(text: str, token: str, radius: int = 130) -> tuple[set[str], str]:
    """Return the dominant entity type attached to a named subtype.

    Taking the union of all entity words in a paragraph is too coarse: an adipose
    paper naturally mentions both adipocytes and macrophages.  Instead each token
    occurrence votes for its nearest entity word, and only a unique winning type is
    returned.  This preserves the actual grammatical attachment (for example
    ``AD5 LAM subtype`` versus ``AD5 adipocytes``).
    """
    votes: dict[str, list[tuple[int, str]]] = {name: [] for name in _ENTITY_PATTERNS}
    for token_match in list(re.finditer(rf"\b{re.escape(token)}\b", text))[:12]:
        start = max(0, token_match.start() - radius)
        end = min(len(text), token_match.end() + radius)
        nearest: tuple[int, str] | None = None
        for name, pattern in _ENTITY_PATTERNS.items():
            for entity_match in pattern.finditer(text, start, end):
                distance = min(
                    abs(entity_match.start() - token_match.end()),
                    abs(token_match.start() - entity_match.end()),
                )
                candidate = (distance, name)
                if nearest is None or candidate < nearest:
                    nearest = candidate
        if nearest is None:
            continue
        excerpt = _clean(text[start:end])
        votes[nearest[1]].append((nearest[0], excerpt))

    ranked = sorted(
        ((len(items), name, items) for name, items in votes.items() if items),
        reverse=True,
    )
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return set(), ""
    _, dominant, evidence = ranked[0]
    evidence.sort(key=lambda item: item[0])
    return {dominant}, evidence[0][1]


@lru_cache(maxsize=256)
def _fetch_cited_article_text(doi: str) -> tuple[str, str]:
    try:
        response = requests.get(
            f"https://doi.org/{doi}", headers=_HEADERS, timeout=10, allow_redirects=True
        )
        if response.status_code != 200 or len(response.text) < 1000:
            return "", response.url
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup(["script", "style", "noscript", "svg"]):
            node.decompose()
        root = soup.find("article") or soup.find("main") or soup.body
        if root is None:
            return "", response.url
        return _clean(" ".join(root.stripped_strings))[:600_000], response.url
    except Exception as exc:
        log.info("Citation content fetch failed for %s: %s", doi, exc)
        return "", ""


def _entity_type_conflicts(citing_text: str, cited_text: str) -> list[dict]:
    tokens = sorted(set(_SUBTYPE_TOKEN.findall(citing_text)))
    conflicts = []
    for token in tokens:
        if token not in cited_text:
            continue
        citing_types, citing_excerpt = _types_near_token(citing_text, token)
        cited_types, cited_excerpt = _types_near_token(cited_text, token)
        if len(citing_types) != 1 or len(cited_types) != 1 or citing_types == cited_types:
            continue
        conflicts.append({
            "token": token,
            "citing_type": next(iter(citing_types)),
            "cited_type": next(iter(cited_types)),
            "citing_excerpt": citing_excerpt[:500],
            "cited_excerpt": cited_excerpt[:500],
        })
    return conflicts


def _candidate_reference_numbers(body: str, tokens: set[str], maximum: int) -> list[int]:
    """Find numeric citations adjacent to subtype claims, ordered by frequency.

    Cell Press extraction commonly turns a superscript citation into ``cells.5``.
    Restricting Crossref lookups to citations next to the claims makes this check
    both quicker and less dependent on external services.
    """
    counts: dict[int, int] = {}
    for token in tokens:
        for match in re.finditer(rf"\b{re.escape(token)}\b", body):
            excerpt = body[max(0, match.start() - 180):min(len(body), match.end() + 220)]
            for citation in re.finditer(r"(?<=[.!?;)\]])(\d{1,2})(?!\d)", excerpt):
                number = int(citation.group(1))
                if 1 <= number <= maximum:
                    counts[number] = counts.get(number, 0) + 1
    return [number for number, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _reference_doi(reference_text: str) -> tuple[str, float]:
    explicit = re.search(r"(?i)(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", reference_text)
    if explicit:
        return explicit.group(1).rstrip(".,;)]").lower(), 100.0
    match = _verify_by_text(reference_text)
    if not match.get("found"):
        return "", 0.0
    return (
        str(match.get("matched_doi") or "").strip().lower(),
        float(match.get("crossref_score") or 0),
    )


def check_citation_content_consistency(pdf_path: str | Path) -> list[dict]:
    """Check short commentaries for systematic subtype/entity misattribution.

    This intentionally runs only for short interpretive articles.  It requires at
    least two independently named subtypes to switch biological entity type, which
    avoids escalating ordinary differences in wording or one ambiguous acronym.
    """
    document = fitz.open(str(pdf_path))
    try:
        if len(document) > 4:
            return []
        first_page = _clean(document[0].get_text("text", sort=True)) if len(document) else ""
        if not _SHORT_ARTICLE_MARKERS.search(first_page):
            return []
        body = _body_text(document)
        references = _layout_references(document)
    finally:
        document.close()
    if not references or len(references) > 20:
        return []

    candidate_tokens = {
        token for token in _SUBTYPE_TOKEN.findall(body)
        if _types_near_token(body, token)[0]
    }
    if len(candidate_tokens) < 2:
        return []

    by_number = {reference["number"]: reference for reference in references}
    ordered_numbers = _candidate_reference_numbers(body, candidate_tokens, max(by_number))
    ordered_numbers.extend(number for number in by_number if number not in ordered_numbers)

    findings = []
    for number in ordered_numbers:
        reference = by_number[number]
        doi, crossref_score = _reference_doi(reference["text"])
        if not doi or crossref_score < 70:
            continue
        cited_text, resolved_url = _fetch_cited_article_text(doi)
        if not cited_text:
            continue
        conflicts = [
            item for item in _entity_type_conflicts(body, cited_text)
            if item["token"] in candidate_tokens
        ]
        if len(conflicts) < 2:
            continue
        token_summary = ", ".join(item["token"] for item in conflicts[:6])
        type_pairs = sorted({f"{item['cited_type']}\u2192{item['citing_type']}" for item in conflicts})
        findings.append({
            "ref_number": reference["number"],
            "ref_text": reference["text"][:300],
            "issue_type": "citation_entity_type_conflict",
            "severity": "high",
            "location": f"参考文献 #{reference['number']} / DOI {doi}",
            "description": (
                f"正文将参考文献 #{reference['number']} 中至少 {len(conflicts)} 个命名亚型"
                f"（{token_summary}）的细胞类型系统性改写为另一类型（{', '.join(type_pairs)}）。"
            ),
            "details": {
                "doi": doi,
                "resolved_url": resolved_url,
                "conflicts": conflicts,
            },
        })
        # The claim-adjacent citation is the primary source for the subtype
        # definitions.  One fully evidenced conflict is decisive; continuing to
        # query unrelated bibliography would only add latency and outage risk.
        break
    return findings
