#!/usr/bin/env python3
"""Migrate legacy yujing report URLs into namespaced report directories.

Copies files instead of moving them, writes an audit manifest, then updates
`yujing.report_url` and `yujing.review_report_url` only for rows whose target file
exists. Dry-run is the default.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import pymssql


BASE = Path("/opt/paper-integrity-checker")
OUTPUT = BASE / "data" / "output"
CHINESE_DIR = OUTPUT / "chinese_reports"
REVIEW_DIR = OUTPUT / "review_v2"
AUDIT_DIR = OUTPUT / "migration_audit"

TABLE = "yujing"
NAMESPACE = "yujing"

CHINESE_BASE = "http://10.119.9.99/chinese_reports"
REVIEW_BASE = "http://10.119.9.99/review_reports"

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}


def _safe_db_name(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe table/namespace name: {name}")
    return name


def _url_rel(url: str, base: str) -> str | None:
    prefix = base.rstrip("/") + "/"
    if not url or not url.startswith(prefix):
        return None
    return unquote(url[len(prefix):])


def _filename_from_url(url: str, base: str) -> str | None:
    rel = _url_rel(url, base)
    if not rel:
        return None
    if rel.startswith(NAMESPACE + "/"):
        return None
    return Path(rel).name


def _planned_report_url(old_url: str) -> dict | None:
    filename = _filename_from_url(old_url, CHINESE_BASE)
    if not filename:
        return None
    return {
        "old_url": old_url,
        "new_url": f"{CHINESE_BASE}/{NAMESPACE}/{filename}",
        "src": str(CHINESE_DIR / filename),
        "dst": str(CHINESE_DIR / NAMESPACE / filename),
    }


def _planned_review_url(old_url: str) -> dict | None:
    filename = _filename_from_url(old_url, REVIEW_BASE)
    src_root = REVIEW_DIR
    if not filename:
        # Two legacy manual review URLs were stored under chinese_reports.
        filename = _filename_from_url(old_url, CHINESE_BASE)
        src_root = CHINESE_DIR
    if not filename:
        return None
    return {
        "old_url": old_url,
        "new_url": f"{REVIEW_BASE}/{NAMESPACE}/{filename}",
        "src": str(src_root / filename),
        "dst": str(REVIEW_DIR / NAMESPACE / filename),
    }


def _connect():
    return pymssql.connect(**DB_CONFIG)


def load_rows(limit: int = 0) -> list[dict]:
    sql = f"""
        SELECT id, doi, report_url, review_report_url
        FROM {TABLE}
        WHERE (report_url IS NOT NULL AND report_url <> '')
           OR (review_report_url IS NOT NULL AND review_report_url <> '')
        ORDER BY id
    """
    if limit:
        sql = sql.replace("SELECT id,", f"SELECT TOP {int(limit)} id,")
    conn = _connect()
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        return list(cur.fetchall())
    finally:
        conn.close()


def build_plan(rows: list[dict]) -> list[dict]:
    plan = []
    for row in rows:
        item = {"id": row["id"], "doi": row.get("doi"), "report": None, "review": None}
        report = _planned_report_url(row.get("report_url") or "")
        review = _planned_review_url(row.get("review_report_url") or "")
        if report:
            report["src_exists"] = Path(report["src"]).exists()
            report["dst_exists"] = Path(report["dst"]).exists()
            item["report"] = report
        if review:
            review["src_exists"] = Path(review["src"]).exists()
            review["dst_exists"] = Path(review["dst"]).exists()
            item["review"] = review
        if report or review:
            plan.append(item)
    return plan


def summarize(plan: list[dict]) -> dict:
    def count(kind: str, pred):
        return sum(1 for item in plan if item.get(kind) and pred(item[kind]))

    return {
        "rows_planned": len(plan),
        "report_planned": sum(1 for item in plan if item.get("report")),
        "report_src_exists": count("report", lambda x: x["src_exists"]),
        "report_missing_src": count("report", lambda x: not x["src_exists"] and not x["dst_exists"]),
        "report_dst_already_exists": count("report", lambda x: x["dst_exists"]),
        "review_planned": sum(1 for item in plan if item.get("review")),
        "review_src_exists": count("review", lambda x: x["src_exists"]),
        "review_missing_src": count("review", lambda x: not x["src_exists"] and not x["dst_exists"]),
        "review_dst_already_exists": count("review", lambda x: x["dst_exists"]),
    }


def write_manifest(plan: list[dict], execute: bool) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = AUDIT_DIR / f"migrate_{TABLE}_reports_{stamp}_{'execute' if execute else 'dryrun'}.json"
    payload = {
        "table": TABLE,
        "namespace": NAMESPACE,
        "execute": execute,
        "created_at": datetime.now().isoformat(),
        "summary": summarize(plan),
        "plan": plan,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _copy_one(entry: dict) -> bool:
    dst = Path(entry["dst"])
    if dst.exists():
        return True
    src = Path(entry["src"])
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def execute_plan(plan: list[dict]) -> dict:
    updates = []
    copied = {"report": 0, "review": 0}
    skipped_missing = {"report": 0, "review": 0}

    for item in plan:
        set_report = None
        set_review = None
        if item.get("report"):
            before = Path(item["report"]["dst"]).exists()
            ok = _copy_one(item["report"])
            if ok:
                set_report = item["report"]["new_url"]
                copied["report"] += 0 if before else 1
            else:
                skipped_missing["report"] += 1
        if item.get("review"):
            before = Path(item["review"]["dst"]).exists()
            ok = _copy_one(item["review"])
            if ok:
                set_review = item["review"]["new_url"]
                copied["review"] += 0 if before else 1
            else:
                skipped_missing["review"] += 1
        if set_report or set_review:
            updates.append({"id": item["id"], "report_url": set_report, "review_report_url": set_review})

    conn = _connect()
    try:
        cur = conn.cursor()
        for update in updates:
            parts = []
            params = {"id": update["id"]}
            if update["report_url"]:
                parts.append("report_url=%(report_url)s")
                params["report_url"] = update["report_url"]
            if update["review_report_url"]:
                parts.append("review_report_url=%(review_report_url)s")
                params["review_report_url"] = update["review_report_url"]
            cur.execute(f"UPDATE {TABLE} SET {', '.join(parts)} WHERE id=%(id)s", params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "db_rows_updated": len(updates),
        "files_copied": copied,
        "skipped_missing": skipped_missing,
    }


def main():
    global TABLE, NAMESPACE

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--table", default=TABLE, help="Target DB table, e.g. yujing or yujing_quanliang")
    ap.add_argument("--namespace", default="", help="Report URL subdirectory; defaults to --table")
    ap.add_argument("--execute", action="store_true", help="Copy files and update DB. Default is dry-run only.")
    ap.add_argument("--limit", type=int, default=0, help="Only inspect/update first N rows, for testing.")
    args = ap.parse_args()

    TABLE = _safe_db_name(args.table)
    NAMESPACE = _safe_db_name(args.namespace or args.table)

    rows = load_rows(limit=args.limit)
    plan = build_plan(rows)
    manifest = write_manifest(plan, execute=args.execute)
    print(json.dumps(summarize(plan), ensure_ascii=False, indent=2))
    print(f"manifest: {manifest}")

    if not args.execute:
        print("dry-run only; re-run with --execute to copy files and update DB")
        return

    result = execute_plan(plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
