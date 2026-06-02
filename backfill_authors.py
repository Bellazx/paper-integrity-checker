#!/usr/bin/env python3
"""Backfill missing author/department data in yujing_quanliang from report.json files."""
import json
import re
import sys
from pathlib import Path

import pymssql

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

SJTU_KEYWORDS = [
    "Shanghai Jiao Tong", "Shanghai Jiaotong", "Jiaotong University",
    "上海交通", "交通大学", "SJTU",
]

OUTPUT_DIR = Path("/opt/paper-integrity-checker/data/output")


def find_report(doi: str) -> Path | None:
    for slug in [doi.replace("/", "_"), doi.replace("/", "__")]:
        p = OUTPUT_DIR / slug / "report.json"
        if p.exists():
            return p
    return None


def find_sjtu_authors(authors_full: list[str], affiliations: list[str]) -> tuple[list[str], list[str]]:
    sjtu_aff_indices = set()
    sjtu_depts = []
    for i, aff in enumerate(affiliations):
        if any(kw in aff for kw in SJTU_KEYWORDS):
            sjtu_aff_indices.add(i + 1)
            clean = re.sub(r'^\d+\.\s*', '', aff.strip())
            sjtu_depts.append(clean)

    sjtu_authors = []
    if sjtu_aff_indices and authors_full:
        for author_name in authors_full:
            m = re.findall(r'(\d+)', author_name)
            if m:
                indices = {int(x) for x in m}
                if indices & sjtu_aff_indices:
                    clean_name = re.sub(r'\d+[,\s]*', '', author_name).strip().rstrip(',')
                    if clean_name:
                        sjtu_authors.append(clean_name)
        if not sjtu_authors:
            sjtu_authors = [authors_full[0].strip().rstrip(',')]

    return sjtu_authors, sjtu_depts


def main():
    dry_run = "--dry-run" in sys.argv

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi, author, author_all, department, department_all FROM yujing_quanliang")
    rows = cursor.fetchall()

    updates = []
    no_report = 0

    for doi, author, author_all, department, department_all in rows:
        need_author = not author or not author.strip()
        need_author_all = not author_all or not author_all.strip()
        need_dept = not department or not department.strip()
        need_dept_all = not department_all or not department_all.strip()

        if not (need_author or need_author_all or need_dept or need_dept_all):
            continue

        rpath = find_report(doi)
        if not rpath:
            no_report += 1
            continue

        with open(rpath) as f:
            report = json.load(f)
        paper = report.get("paper", {})
        authors_full = paper.get("authors_full", [])
        affiliations = paper.get("affiliations", [])

        if not authors_full and not affiliations:
            continue

        fields = {}

        if need_author_all and authors_full:
            fields["author_all"] = ", ".join(authors_full)[:2000]

        if need_dept_all and affiliations:
            fields["department_all"] = "; ".join(affiliations)[:2000]

        if (need_author or need_dept) and affiliations:
            sjtu_authors, sjtu_depts = find_sjtu_authors(authors_full, affiliations)
            if need_author and sjtu_authors:
                fields["author"] = ", ".join(sjtu_authors)[:500]
            if need_dept and sjtu_depts:
                fields["department"] = "; ".join(sjtu_depts)[:500]

        if fields:
            updates.append((doi, fields))

    print(f"Total papers: {len(rows)}")
    print(f"Need update: {len(updates)}")
    print(f"No report.json: {no_report}")
    print(f"Fields breakdown:")
    for field in ["author", "author_all", "department", "department_all"]:
        count = sum(1 for _, f in updates if field in f)
        print(f"  {field}: {count}")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
        conn.close()
        return

    print(f"\nUpdating {len(updates)} papers...")
    done = 0
    for doi, fields in updates:
        set_clause = ", ".join(f"{k}=%({k})s" for k in fields)
        sql = f"UPDATE yujing_quanliang SET {set_clause} WHERE doi=%(doi)s"
        params = {**fields, "doi": doi}
        cursor.execute(sql, params)
        done += 1
        if done % 500 == 0:
            conn.commit()
            print(f"  {done}/{len(updates)}...")

    conn.commit()
    conn.close()
    print(f"Done. Updated {done} papers.")


if __name__ == "__main__":
    main()
