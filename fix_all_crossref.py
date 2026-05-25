"""Fetch title/authors/affiliations from CrossRef API for ALL papers in DB, then update."""
import re
import time
import logging
import requests
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


def _fetch_crossref(doi: str) -> dict | None:
    try:
        resp = requests.get(f"{CROSSREF_API}/{doi}", headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json()["message"]
    except Exception as e:
        log.warning("CrossRef failed for %s: %s", doi, e)
    return None


def _extract_info(work: dict) -> dict:
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


def main():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    cursor.execute("SELECT doi FROM yujing WHERE doi IS NOT NULL AND doi != '' ORDER BY doi")
    all_dois = [r[0] for r in cursor.fetchall()]
    log.info("Total DOIs in DB: %d", len(all_dois))

    updated = 0
    failed = 0
    skipped = 0

    for i, doi in enumerate(all_dois):
        time.sleep(0.15)
        work = _fetch_crossref(doi)
        if not work:
            failed += 1
            if (i + 1) % 50 == 0:
                log.info("[%d/%d] %d updated, %d failed, %d skipped", i + 1, len(all_dois), updated, failed, skipped)
            continue

        info = _extract_info(work)
        if not info["title"]:
            skipped += 1
            continue

        params = {
            "doi": doi,
            "title": info["title"][:500],
            "author": ", ".join(info["sjtu_authors"])[:500] if info["sjtu_authors"] else ", ".join(info["authors"][:3])[:500],
            "author_all": ", ".join(info["authors"])[:500] if len(", ".join(info["authors"])) <= 500 else ", ".join(info["authors"])[:497] + "...",
            "department": "; ".join(info["sjtu_depts"])[:500] if info["sjtu_depts"] else "",
            "department_all": "; ".join(info["all_affiliations"])[:500] if len("; ".join(info["all_affiliations"])) <= 500 else "; ".join(info["all_affiliations"])[:497] + "...",
        }

        if info["journal"]:
            params["journal"] = info["journal"][:200]
            sql = """UPDATE yujing SET
                title=%(title)s, journal=%(journal)s,
                author=%(author)s, author_all=%(author_all)s,
                department=%(department)s, department_all=%(department_all)s
            WHERE doi=%(doi)s"""
        else:
            sql = """UPDATE yujing SET
                title=%(title)s,
                author=%(author)s, author_all=%(author_all)s,
                department=%(department)s, department_all=%(department_all)s
            WHERE doi=%(doi)s"""

        cursor.execute(sql, params)
        updated += 1

        if (i + 1) % 50 == 0:
            conn.commit()
            log.info("[%d/%d] %d updated, %d failed, %d skipped", i + 1, len(all_dois), updated, failed, skipped)

    conn.commit()
    conn.close()
    log.info("Done: %d updated, %d failed, %d skipped out of %d", updated, failed, skipped, len(all_dois))


if __name__ == "__main__":
    main()
