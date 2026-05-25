"""CrossRef API utilities for metadata enrichment and repair."""
import re
import logging
import requests

log = logging.getLogger(__name__)

CROSSREF_API = "https://api.crossref.org/works"
HEADERS = {"User-Agent": "PaperIntegrityChecker/1.0 (mailto:paper-check@example.com)"}

_FRONTIERS_PREFIX_RE = re.compile(
    r'^(fphar|fendo|fimmu|fmed|fbioe|fonc|fgene|fcell|fmicb|fphys|fneur|fnins|fmolb|'
    r'fpsyg|fnut|fcvm|fsurg|fpubh|fmars|feart|fenvs|fevo|fevo|fdata)-\d{4}-\d+\s+\d+\.\.\d+$'
)

SJTU_KEYWORDS = [
    "Shanghai Jiao Tong", "Shanghai Jiaotong", "Jiaotong University",
    "上海交通", "交通大学", "SJTU",
]


def is_bad_title(title: str) -> bool:
    """Detect PDF extraction artifacts masquerading as titles."""
    if not title:
        return True
    t = title.strip()
    if len(t) < 10:
        return True
    if t.startswith("10."):
        return True
    if _FRONTIERS_PREFIX_RE.match(t):
        return True
    if t.startswith("Frontiers _") or t.startswith("Frontiers_"):
        return True
    if ".." in t and len(t) < 40:
        return True
    return False


def fetch_crossref(doi: str) -> dict | None:
    """Fetch work metadata from CrossRef API."""
    try:
        resp = requests.get(f"{CROSSREF_API}/{doi}", headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()["message"]
    except Exception as e:
        log.warning("CrossRef fetch failed for %s: %s", doi, e)
    return None


def extract_crossref_info(work: dict) -> dict:
    """Parse CrossRef work record into normalized metadata dict."""
    title_raw = (work.get("title") or [""])[0]
    title = re.sub(r'<[^>]+>', '', title_raw).strip()

    journal = (work.get("container-title") or [""])[0]

    authors = []
    author_affs = []
    for a in work.get("author", []):
        given = a.get("given", "")
        family = a.get("family", "")
        name = f"{family}, {given}" if family and given else family or given
        if name:
            authors.append(name)
        for aff in a.get("affiliation", []):
            aff_name = aff.get("name", "")
            if aff_name:
                author_affs.append((name, aff_name))

    all_affs = []
    seen = set()
    for _, aff_name in author_affs:
        if aff_name not in seen:
            all_affs.append(aff_name)
            seen.add(aff_name)

    sjtu_authors = []
    sjtu_depts = []
    for name, aff_name in author_affs:
        if any(kw.lower() in aff_name.lower() for kw in SJTU_KEYWORDS):
            if name and name not in sjtu_authors:
                sjtu_authors.append(name)
            if aff_name not in sjtu_depts:
                sjtu_depts.append(aff_name)

    return {
        "title": title,
        "journal": journal,
        "authors": authors,
        "all_affiliations": all_affs,
        "sjtu_authors": sjtu_authors,
        "sjtu_depts": sjtu_depts,
    }


def enrich_metadata(doi: str) -> dict | None:
    """One-call convenience: fetch + extract. Returns None on failure."""
    work = fetch_crossref(doi)
    if not work:
        return None
    info = extract_crossref_info(work)
    if not info["title"]:
        return None
    return info
