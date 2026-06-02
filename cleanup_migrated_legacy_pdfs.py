#!/usr/bin/env python3
"""Delete legacy root-level PDFs after namespace migration verification.

Safety gates:
1. source and every migrated destination must exist;
2. every destination must be byte-for-byte identical to the source;
3. the old URL must not be referenced by any URL-like DB column.

Dry-run is the default. Use --execute to unlink verified legacy files.
"""
from __future__ import annotations

import argparse
import filecmp
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pymssql


BASE = Path("/opt/paper-integrity-checker")
AUDIT_DIR = BASE / "data" / "output" / "migration_audit"
OUTPUT_DIR = BASE / "data" / "output"

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

MANIFESTS = [
    AUDIT_DIR / "migrate_yujing_reports_20260602_115028_execute.json",
    AUDIT_DIR / "migrate_yujing_quanliang_reports_20260602_120004_execute.json",
]


def _load_manifest_candidates() -> dict[str, dict]:
    by_src: dict[str, dict] = {}
    for manifest in MANIFESTS:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for item in data.get("plan", []):
            for kind in ("report", "review"):
                entry = item.get(kind)
                if not entry:
                    continue
                src = entry["src"]
                by_src.setdefault(src, {"src": src, "old_urls": set(), "dests": set(), "kinds": set()})
                by_src[src]["old_urls"].add(entry["old_url"])
                by_src[src]["dests"].add(entry["dst"])
                by_src[src]["kinds"].add(kind)

    # Supplemental 5 review URL updates for yujing_quanliang.
    supp = AUDIT_DIR / "migrate_yujing_quanliang_review_url_supplement_20260602_1213.json"
    if supp.exists():
        data = json.loads(supp.read_text(encoding="utf-8"))
        for row in data.get("rows", []):
            new_url = row["review_report_url"]
            filename = new_url.rsplit("/", 1)[-1]
            src = str(OUTPUT_DIR / "review_v2" / filename)
            dst = str(OUTPUT_DIR / "review_v2" / "yujing_quanliang" / filename)
            old_url = f"http://10.119.9.99/review_reports/{filename}"
            by_src.setdefault(src, {"src": src, "old_urls": set(), "dests": set(), "kinds": set()})
            by_src[src]["old_urls"].add(old_url)
            by_src[src]["dests"].add(dst)
            by_src[src]["kinds"].add("review")

    out = {}
    for src, item in by_src.items():
        item["old_urls"] = sorted(item["old_urls"])
        item["dests"] = sorted(item["dests"])
        item["kinds"] = sorted(item["kinds"])
        out[src] = item
    return out


def _url_columns() -> list[tuple[str, str]]:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE COLUMN_NAME IN (
                'report_url', 'review_report_url',
                'chushen_report_url', 'review_report_url'
            )
            ORDER BY TABLE_NAME, COLUMN_NAME
            """
        )
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


def _referenced_old_urls(old_urls: set[str], url_columns: list[tuple[str, str]]) -> dict[str, int]:
    refs: dict[str, int] = defaultdict(int)
    if not old_urls:
        return refs

    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        for table, col in url_columns:
            cur.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} <> ''")
            for row in cur.fetchall():
                url = row[0]
                if url in old_urls:
                    refs[url] += 1
    finally:
        conn.close()
    return dict(refs)


def build_cleanup_plan() -> dict:
    candidates = _load_manifest_candidates()
    url_columns = _url_columns()
    old_urls = {u for item in candidates.values() for u in item["old_urls"]}
    refs = _referenced_old_urls(old_urls, url_columns)

    deletable = []
    blocked = []
    bytes_deletable = 0

    for item in candidates.values():
        src = Path(item["src"])
        reasons = []
        if not src.exists():
            reasons.append("source_missing")
        old_refs = {u: refs[u] for u in item["old_urls"] if refs.get(u)}
        if old_refs:
            reasons.append("old_url_still_referenced")
        for dst_s in item["dests"]:
            dst = Path(dst_s)
            if not dst.exists():
                reasons.append(f"dest_missing:{dst_s}")
            elif src.exists() and not filecmp.cmp(src, dst, shallow=False):
                reasons.append(f"dest_differs:{dst_s}")

        record = {
            **item,
            "old_url_refs": old_refs,
            "size": src.stat().st_size if src.exists() else 0,
        }
        if reasons:
            record["blocked_reasons"] = reasons
            blocked.append(record)
        else:
            deletable.append(record)
            bytes_deletable += record["size"]

    return {
        "created_at": datetime.now().isoformat(),
        "url_columns_checked": [f"{t}.{c}" for t, c in url_columns],
        "summary": {
            "candidates": len(candidates),
            "deletable": len(deletable),
            "blocked": len(blocked),
            "bytes_deletable": bytes_deletable,
            "gb_deletable": round(bytes_deletable / 1024 / 1024 / 1024, 2),
        },
        "deletable": deletable,
        "blocked": blocked,
    }


def write_plan(plan: dict, execute: bool) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = AUDIT_DIR / f"cleanup_migrated_legacy_pdfs_{stamp}_{'execute' if execute else 'dryrun'}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def execute_cleanup(plan: dict) -> dict:
    deleted = 0
    bytes_deleted = 0
    for item in plan["deletable"]:
        src = Path(item["src"])
        if not src.exists():
            continue
        size = src.stat().st_size
        src.unlink()
        deleted += 1
        bytes_deleted += size
    return {
        "deleted": deleted,
        "bytes_deleted": bytes_deleted,
        "gb_deleted": round(bytes_deleted / 1024 / 1024 / 1024, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true", help="Actually delete verified legacy files.")
    args = ap.parse_args()

    plan = build_cleanup_plan()
    path = write_plan(plan, execute=args.execute)
    print(json.dumps(plan["summary"], ensure_ascii=False, indent=2))
    print(f"manifest: {path}")
    if not args.execute:
        print("dry-run only; re-run with --execute to delete verified legacy files")
        return
    print(json.dumps(execute_cleanup(plan), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
