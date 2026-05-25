"""Update PNAS paper metadata in DB from WoS export Excel."""
import re
import logging
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

SJTU_KEYWORDS = ["Shanghai Jiao Tong", "上海交通"]

EXCEL_PATH = "data/list-4m&pnas.xls"


def _parse_addresses(addr_str: str) -> list[tuple[list[str], str]]:
    """Parse WoS Addresses field into [(authors, institution), ...]."""
    if not addr_str or pd.isna(addr_str):
        return []
    results = []
    for block in re.finditer(r'\[([^\]]+)\]\s*([^;[]+)', str(addr_str)):
        authors = [a.strip() for a in block.group(1).split(';')]
        institution = block.group(2).strip().rstrip(';').strip()
        results.append((authors, institution))
    return results


def _find_sjtu_info(addr_str: str) -> tuple[list[str], list[str]]:
    """Extract SJTU-affiliated authors and departments from Addresses field."""
    blocks = _parse_addresses(addr_str)
    sjtu_authors = []
    sjtu_depts = []
    for authors, inst in blocks:
        if any(kw.lower() in inst.lower() for kw in SJTU_KEYWORDS):
            for a in authors:
                if a and a not in sjtu_authors:
                    sjtu_authors.append(a)
            if inst not in sjtu_depts:
                sjtu_depts.append(inst)
    return sjtu_authors, sjtu_depts


def main():
    df = pd.read_excel(EXCEL_PATH, engine="xlrd")
    pnas = df[df["DOI"].astype(str).str.contains("10.1073", na=False)].copy()
    pnas["doi_clean"] = pnas["DOI"].astype(str).str.replace("_", "/", n=1)
    log.info("Loaded %d PNAS records from Excel", len(pnas))

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    cursor.execute("SELECT doi FROM yujing WHERE doi LIKE '10.1073/pnas%%'")
    db_dois = {r[0] for r in cursor.fetchall()}
    log.info("Found %d PNAS DOIs in DB", len(db_dois))

    updated = 0
    skipped = 0

    for _, row in pnas.iterrows():
        doi = row["doi_clean"]
        if doi not in db_dois:
            skipped += 1
            continue

        title = str(row.get("Article Title", "") or "")[:500]
        author_full_names = str(row.get("Author Full Names", "") or "")
        affiliations = str(row.get("Affiliations", "") or "")
        addresses = str(row.get("Addresses", "") or "")

        sjtu_authors, sjtu_depts = _find_sjtu_info(addresses)

        params = {
            "doi": doi,
            "title": title,
            "author": ", ".join(sjtu_authors)[:500] if sjtu_authors else author_full_names.split(";")[0].strip()[:500],
            "author_all": author_full_names[:500] if len(author_full_names) <= 500 else author_full_names[:497] + "...",
            "department": "; ".join(sjtu_depts)[:500] if sjtu_depts else "",
            "department_all": affiliations[:500] if len(affiliations) <= 500 else affiliations[:497] + "...",
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

        if updated % 20 == 0:
            conn.commit()
            log.info("Progress: %d updated", updated)

    conn.commit()
    conn.close()
    log.info("Done: %d updated, %d skipped (not in DB)", updated, skipped)


if __name__ == "__main__":
    main()
