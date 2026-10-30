#!/usr/bin/env python3
"""Apply the high-confidence Crossref title fixes to the other yujing tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pymssql

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from modules.chinese_report_generator import _make_filename
from utils.db import DB_CONFIG


SOURCE = Path("/tmp/yujing_other_title_check.json")
AUDIT = BASE / "data/output/audit_other_title_fix_20260930"
PUBLIC_ROOTS = (BASE / "data/output", BASE / "data/migrated_99_reports/output")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def source_rows() -> list[dict]:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    return [r for r in data["rows"] if r.get("category") == "high_confidence_wrong"]


def db_rows(rows: list[dict]) -> dict[tuple[str, int], dict]:
    by_table: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        by_table[row["table"]].append(int(row["id"]))
    conn = pymssql.connect(**DB_CONFIG)
    try:
        result = {}
        cur = conn.cursor(as_dict=True)
        for table, ids in by_table.items():
            placeholders = ",".join("%s" for _ in ids)
            cur.execute(
                f"SELECT id,title,doi,report_url FROM [{table}] WHERE id IN ({placeholders})",
                tuple(ids),
            )
            result.update({(table, int(r["id"])): dict(r) for r in cur.fetchall()})
        return result
    finally:
        conn.close()


def new_url(old_url: str, filename: str) -> str:
    parsed = urlsplit(old_url)
    old_path = parsed.path
    directory = old_path.rsplit("/", 1)[0] if "/" in old_path else ""
    return urlunsplit((parsed.scheme, parsed.netloc, f"{directory}/{filename}", "", ""))


def candidate_paths(url: str) -> list[Path]:
    parsed = urlsplit(url)
    path = parsed.path
    prefix = "/chinese_reports/"
    if not path.startswith(prefix):
        return []
    relative = path[len(prefix):].strip("/")
    parts = relative.split("/")
    if len(parts) > 1:
        namespace, filename = parts[0], parts[-1]
        return [root / namespace / "chinese_reports" / filename for root in PUBLIC_ROOTS]
    return [root / "chinese_reports" / relative for root in PUBLIC_ROOTS]


def build_plan(rows: list[dict], current: dict[tuple[str, int], dict]) -> list[dict]:
    plan = []
    seen_url: dict[str, dict] = {}
    for scan in rows:
        key = (scan["table"], int(scan["id"]))
        row = current.get(key)
        if not row:
            raise RuntimeError(f"Missing DB row: {key}")
        if str(row.get("doi") or "").strip().lower() != str(scan.get("doi") or "").strip().lower():
            raise RuntimeError(f"DOI changed before apply: {key}")
        if (row.get("title") or "") != (scan.get("title") or ""):
            raise RuntimeError(f"Title changed before apply: {key}; rerun audit")
        filename = _make_filename(scan["doi"], scan["crossref_title"])
        old_url = row.get("report_url") or ""
        item = {
            "table": scan["table"],
            "id": int(scan["id"]),
            "doi": scan["doi"],
            "old_title": scan["title"],
            "new_title": scan["crossref_title"],
            "old_url": old_url,
            "new_url": new_url(old_url, filename) if old_url else "",
            "new_filename": filename,
            "old_candidates": [str(p) for p in candidate_paths(old_url)],
        }
        if old_url:
            prior = seen_url.get(old_url)
            if prior and prior["new_title"] != item["new_title"]:
                raise RuntimeError(f"Conflicting titles for shared report URL: {old_url}")
            seen_url[old_url] = item
        plan.append(item)
    return plan


def find_old_path(item: dict) -> Path | None:
    for raw in item["old_candidates"]:
        p = Path(raw)
        if p.is_file():
            return p
    return None


def target_paths(item: dict) -> list[Path]:
    return [Path(raw).parent / item["new_filename"] for raw in item["old_candidates"]]


def apply_files(plan: list[dict], manifest_path: Path) -> None:
    # Work once per URL, because copied tables can point at the same PDF.
    unique: dict[tuple[str, str], dict] = {}
    for item in plan:
        if item["old_url"]:
            unique[(item["old_url"], item["new_filename"])] = item
    manifest = {"started_at": datetime.now().isoformat(), "items": []}
    for item in unique.values():
        old_path = find_old_path(item)
        target = None
        if old_path:
            target = old_path.parent / item["new_filename"]
        else:
            candidates = [Path(p) for p in item["old_candidates"]]
            target = candidates[0].parent / item["new_filename"] if candidates else None
        record = dict(item)
        record.update({"old_path": str(old_path) if old_path else "", "target_path": str(target) if target else "", "file_action": "missing"})
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                record["file_action"] = "target_already_exists"
            elif old_path:
                shutil.move(str(old_path), str(target))
                record["file_action"] = "moved_and_renamed"
            record["target_exists"] = target.is_file()
            record["target_sha256"] = sha256(target) if target.is_file() else ""
        else:
            record["target_exists"] = False
            record["target_sha256"] = ""
        manifest["items"].append(record)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["finished_at"] = datetime.now().isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def update_db(plan: list[dict]) -> None:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        for item in plan:
            report_url = item["old_url"]
            if item["old_url"] and any(path.is_file() for path in target_paths(item)):
                report_url = item["new_url"]
            cur.execute(
                f"""UPDATE [{item['table']}]
                       SET title=%s, report_url=%s
                     WHERE id=%s AND title=%s AND doi=%s""",
                (item["new_title"], report_url, item["id"], item["old_title"], item["doi"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Concurrent DB change or duplicate id: {item['table']}:{item['id']}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = source_rows()
    current = db_rows(rows)
    plan = build_plan(rows, current)
    AUDIT.mkdir(parents=True, exist_ok=True)
    manifest_path = AUDIT / "manifest.json"
    (AUDIT / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.apply:
        print(json.dumps({"rows": len(plan), "unique_report_urls": len({x['old_url'] for x in plan if x['old_url']}), "manifest": str(manifest_path), "apply": False}, ensure_ascii=False))
        return
    apply_files(plan, manifest_path)
    update_db(plan)
    print(json.dumps({"rows": len(plan), "manifest": str(manifest_path), "apply": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
