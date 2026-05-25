"""Fetch title/authors/affiliations from CrossRef API for PNAS papers missing from Excel, then update DB."""
import re
import time
import logging
import requests
import pandas as pd
import pymssql

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

CROSSREF_API = "https://api.crossref.org/works"
HEADERS = {"User-Agent": "PaperIntegrityChecker/1.0 (mailto:paper-check@example.com)"}
SJTU_KEYWORDS = ["Shanghai Jiao Tong", "上海交通"]
EXCEL_PATH = "data/list-4m&pnas.xls"


def _fetch_crossref(doi: str) -> dict | None:
    try:
        resp = requests.get(f"{CROSSREF_API}/{doi}", headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()["message"]
    except Exception as e:
        log.warning("CrossRef lookup failed for %s: %s", doi, e)
    return None


def _extract_info(work: dict) -> dict:
    title = (work.get("title") or [""])[0]

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
    seen_affs = set()
    for _, aff_name in author_affs:
        if aff_name not in seen_affs:
            all_affs.append(aff_name)
            seen_affs.add(aff_name)

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
        "authors": authors,
        "all_affiliations": all_affs,
        "sjtu_authors": sjtu_authors,
        "sjtu_depts": sjtu_depts,
    }


def main():
    df = pd.read_excel(EXCEL_PATH, engine="xlrd")
    pnas = df[df["DOI"].astype(str).str.contains("10.1073", na=False)]
    excel_dois = set(pnas["DOI"].astype(str).str.replace("_", "/", n=1))

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi FROM yujing WHERE doi LIKE '10.1073/pnas%%'")
    db_dois = [r[0] for r in cursor.fetchall()]

    missing = [d for d in db_dois if d not in excel_dois]
    log.info("Need to fetch %d DOIs from CrossRef", len(missing))

    updated = 0
    failed = 0

    for i, doi in enumerate(missing):
        time.sleep(0.2)
        work = _fetch_crossref(doi)
        if not work:
            failed += 1
            log.warning("[%d/%d] FAILED: %s", i + 1, len(missing), doi)
            continue

        info = _extract_info(work)

        if not info["title"]:
            failed += 1
            log.warning("[%d/%d] No title from CrossRef: %s", i + 1, len(missing), doi)
            continue

        params = {
            "doi": doi,
            "title": info["title"][:500],
            "author": ", ".join(info["sjtu_authors"])[:500] if info["sjtu_authors"] else (", ".join(info["authors"][:3]))[:500],
            "author_all": ", ".join(info["authors"])[:500] if len(", ".join(info["authors"])) <= 500 else ", ".join(info["authors"])[:497] + "...",
            "department": "; ".join(info["sjtu_depts"])[:500] if info["sjtu_depts"] else "",
            "department_all": "; ".join(info["all_affiliations"])[:500] if len("; ".join(info["all_affiliations"])) <= 500 else "; ".join(info["all_affiliations"])[:497] + "...",
        }

        sql = """UPDATE yujing SET
            title=%(title)s,
            author=%(author)s,
            author_all=%(author_all)s,
            department=%(department)s,
            department_all=%(department_all)s
        WHERE doi=%(doi)s"""

        cursor.execute(sql, params)
        updated += 1

        if updated % 10 == 0:
            conn.commit()
            log.info("[%d/%d] Progress: %d updated, %d failed", i + 1, len(missing), updated, failed)

    conn.commit()
    conn.close()
    log.info("Done: %d updated, %d failed out of %d", updated, failed, len(missing))


if __name__ == "__main__":
    main()
