#!/usr/bin/env python3
"""Repair high-confidence bad titles in yujing_audit.

The Crossref classification is intentionally supplied as an input artifact.  This
script only applies the high-confidence bucket, keeps an audit manifest, writes
reports under the namespaced gateway layout, and updates report_url in the same
database transaction as title.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.chinese_report_generator import _make_filename, doi_to_slug, generate_chinese_pdf
from utils.db import DB_CONFIG


BASE = Path(__file__).resolve().parents[1]
CLASSIFICATION = Path("/tmp/yujing_audit_title_categories.json")
LEGACY_ROOT = BASE / "data/output/chinese_reports/yujing_audit"
TARGET_ROOT = BASE / "data/output/yujing_audit/chinese_reports"
AUDIT_ROOT = BASE / "data/output/audit_title_fix_20260930"
PUBLIC_BASE = "http://202.120.57.100/chinese_reports/yujing_audit"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_plan() -> list[dict]:
    data = json.loads(CLASSIFICATION.read_text(encoding="utf-8"))
    return [r for r in data["rows"] if r.get("category") == "high_confidence_wrong"]


def filename_from_url(url: str) -> str:
    return Path(urlsplit(url or "").path).name


def db_rows(ids: list[int]) -> dict[int, dict]:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        placeholders = ",".join("%s" for _ in ids)
        cur.execute(
            f"SELECT id, title, doi, report_url FROM yujing_audit WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return {int(r["id"]): dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def build_items(rows: list[dict], current: dict[int, dict]) -> list[dict]:
    items = []
    for r in rows:
        row = current.get(int(r["id"]))
        if not row:
            raise RuntimeError(f"DB row disappeared: {r['id']}")
        if (row.get("doi") or "").strip().lower() != (r.get("doi") or "").strip().lower():
            raise RuntimeError(f"DOI changed for id={r['id']}: DB={row.get('doi')!r} scan={r.get('doi')!r}")
        if (row.get("title") or "") != (r.get("title") or ""):
            raise RuntimeError(f"title changed for id={r['id']}; rerun the audit before applying")
        new_filename = _make_filename(r["doi"], r["crossref_title"])
        old_filename = filename_from_url(row.get("report_url") or "")
        items.append(
            {
                "id": int(r["id"]),
                "doi": r["doi"],
                "old_title": r["title"],
                "new_title": r["crossref_title"],
                "old_report_url": row.get("report_url") or "",
                "old_filename": old_filename,
                "new_filename": new_filename,
                "new_report_url": f"{PUBLIC_BASE}/{new_filename}",
                "report_json": str(BASE / "data/output/yujing_audit" / doi_to_slug(r["doi"]) / "report.json"),
            }
        )
    return items


def prepare_audit(items: list[dict]) -> Path:
    AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
    (AUDIT_ROOT / "db_rows_before.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return AUDIT_ROOT / "manifest.json"


def apply_files(items: list[dict], manifest_path: Path, regenerate: bool = True) -> None:
    TARGET_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {"started_at": datetime.now().isoformat(), "items": []}
    for item in items:
        old_path = LEGACY_ROOT / item["old_filename"] if item["old_filename"] else None
        legacy_new_path = LEGACY_ROOT / item["new_filename"]
        target_path = TARGET_ROOT / item["new_filename"]
        report_json = Path(item["report_json"])
        record = dict(item)
        record.update({"old_path": str(old_path) if old_path else "", "target_path": str(target_path), "file_action": "missing"})

        # Regenerate when the original findings JSON exists, so the title inside
        # the PDF is corrected as well as its filename.
        if regenerate and report_json.is_file():
            findings = json.loads(report_json.read_text(encoding="utf-8"))
            findings = copy.deepcopy(findings)
            findings.setdefault("paper", {})["title"] = item["new_title"]
            findings["paper"]["doi"] = item["doi"]
            output, _ = generate_chinese_pdf(findings, str(TARGET_ROOT))
            if not output or Path(output) != target_path or not target_path.is_file():
                raise RuntimeError(f"failed to generate corrected report for id={item['id']}")
            # Keep the analysis artifact consistent with the PDF metadata.
            findings["paper"]["title"] = item["new_title"]
            report_json.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
            record["file_action"] = "regenerated_from_report_json"
        elif target_path.is_file():
            record["file_action"] = "target_already_exists"
        elif legacy_new_path.is_file():
            shutil.move(str(legacy_new_path), str(target_path))
            record["file_action"] = "moved_existing_new_name"
        elif old_path and old_path.is_file():
            shutil.move(str(old_path), str(target_path))
            record["file_action"] = "moved_and_renamed_existing_old_name"

        # Once the target is verified, remove stale legacy copies.  The manifest
        # records the old paths, so the operation remains auditable without
        # leaving duplicate PDFs in the served tree.
        if target_path.is_file():
            for stale in (old_path, legacy_new_path):
                if stale and stale != target_path and stale.is_file():
                    stale.unlink()

        record["target_exists"] = target_path.is_file()
        record["target_sha256"] = sha256(target_path) if target_path.is_file() else ""
        manifest["items"].append(record)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest["finished_at"] = datetime.now().isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def update_db(items: list[dict]) -> None:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        for item in items:
            cur.execute(
                """UPDATE yujing_audit
                   SET title=%s, report_url=%s
                 WHERE id=%s AND title=%s AND doi=%s""",
                (item["new_title"], item["new_report_url"], item["id"], item["old_title"], item["doi"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"concurrent DB change or duplicate id={item['id']}; rolling back")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-regenerate", action="store_true", help="Only move/rename existing PDFs")
    args = parser.parse_args()
    rows = load_plan()
    current = db_rows([int(r["id"]) for r in rows])
    items = build_items(rows, current)
    manifest_path = prepare_audit(items)
    if not args.apply:
        print(json.dumps({"count": len(items), "manifest": str(manifest_path), "apply": False}, ensure_ascii=False))
        return
    apply_files(items, manifest_path, regenerate=not args.no_regenerate)
    update_db(items)
    print(json.dumps({"count": len(items), "manifest": str(manifest_path), "apply": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
