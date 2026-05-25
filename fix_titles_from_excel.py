#!/usr/bin/env python3
"""One-time fix: update bad titles/journals in yujing_quanliang from Excel metadata.

Usage:
    python3 fix_titles_from_excel.py           # dry run (show changes)
    python3 fix_titles_from_excel.py --apply   # apply changes to DB
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymssql
from utils.excel_metadata import load_batch_excel

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

EXCEL_FILES = [
    Path("data/input/20260520-649/2026-05-20-数据服务中心交付-数据列表-原始数据fromInCites-649篇.xlsx.xlsx"),
    Path("data/input/20260521/2026-05-21-数据服务中心交付-数据列表-原始数据fromInCites-1601篇.xlsx.xlsx"),
]


def main():
    parser = argparse.ArgumentParser(description="Fix bad titles in yujing_quanliang from Excel")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes (default is dry run)")
    args = parser.parse_args()

    excel_data = {}
    for fp in EXCEL_FILES:
        if fp.exists():
            batch = load_batch_excel(fp)
            excel_data.update(batch)
            print(f"Loaded {len(batch)} entries from {fp.name}")
        else:
            print(f"WARNING: {fp} not found, skipping")

    if not excel_data:
        print("No Excel data loaded, nothing to do.")
        return

    print(f"Total Excel entries: {len(excel_data)}")

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi, title, journal FROM yujing_quanliang WHERE doi IS NOT NULL")
    rows = cursor.fetchall()
    print(f"DB records: {len(rows)}")

    updates = []
    for doi, db_title, db_journal in rows:
        if doi not in excel_data:
            continue
        excel = excel_data[doi]
        new_title = excel.get("title", "").strip()
        new_journal = excel.get("source", "").strip()

        title_changed = new_title and new_title != (db_title or "").strip()
        journal_changed = new_journal and new_journal != (db_journal or "").strip()

        if title_changed or journal_changed:
            updates.append({
                "doi": doi,
                "old_title": db_title,
                "new_title": new_title if title_changed else db_title,
                "old_journal": db_journal,
                "new_journal": new_journal if journal_changed else db_journal,
                "title_changed": title_changed,
                "journal_changed": journal_changed,
            })

    print(f"\nChanges needed: {len(updates)}")
    title_changes = sum(1 for u in updates if u["title_changed"])
    journal_changes = sum(1 for u in updates if u["journal_changed"])
    print(f"  Title changes: {title_changes}")
    print(f"  Journal changes: {journal_changes}")

    if not updates:
        print("Nothing to update.")
        conn.close()
        return

    print(f"\nSample changes (first 10):")
    for u in updates[:10]:
        if u["title_changed"]:
            print(f"  [{u['doi']}]")
            print(f"    title: {(u['old_title'] or '')[:60]} -> {u['new_title'][:60]}")
        if u["journal_changed"]:
            print(f"    journal: {(u['old_journal'] or '')[:40]} -> {u['new_journal'][:40]}")

    if not args.apply:
        print(f"\nDry run complete. Use --apply to write {len(updates)} updates to DB.")
        conn.close()
        return

    print(f"\nApplying {len(updates)} updates...")
    applied = 0
    for u in updates:
        cursor.execute(
            "UPDATE yujing_quanliang SET title=%(title)s, journal=%(journal)s WHERE doi=%(doi)s",
            {"doi": u["doi"], "title": u["new_title"][:500], "journal": u["new_journal"][:200]},
        )
        if cursor.rowcount > 0:
            applied += 1
    conn.commit()
    conn.close()

    print(f"Done. Applied {applied}/{len(updates)} updates.")


if __name__ == "__main__":
    main()
