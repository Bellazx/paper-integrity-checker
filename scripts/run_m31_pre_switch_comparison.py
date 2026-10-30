#!/usr/bin/env python3
"""Re-review one pre-switch scholar batch with MiniMax-M3.1-Flash.

This is an isolated comparison run: the formal yujing_scholar rows are read
only, reports are written under a separate namespace, and all copied rows plus
the new result are stored in a temporary comparison table.
"""
from __future__ import annotations

import asyncio
import json
import csv
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("REVIEW_MODEL", "MiniMax-M3.1-Flash")
os.environ.setdefault("REVIEW_FALLBACK_MODEL", "MiniMax-M3.1-Flash")
os.environ.setdefault("REVIEW_EVIDENCE_CONCURRENCY", "8")
os.environ.setdefault("REVIEW_NUMERIC_THREADS", "1")
os.environ.setdefault("REVIEW_PAPER_TIMEOUT_SECONDS", "5400")

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pymssql

from api.services.review import run_review_single
from modules.doi_utils import dir_name_to_doi, normalize_doi
from modules.chinese_report_generator import doi_to_slug
from utils.db import DB_CONFIG

SCHOLAR = "林欣"
SWITCH = datetime(2026, 10, 21)
INPUT_ROOT = BASE / "data/input/scholar" / SCHOLAR
NAMESPACE = "yujing_scholar_m31_compare_20261030"
OUT_ROOT = BASE / "data/output" / NAMESPACE
REPORT_DIR = OUT_ROOT / "review_v2"
TABLE = NAMESPACE
WORKERS = int(os.environ.get("M31_COMPARE_WORKERS", "32"))


def db_rows():
    conn = pymssql.connect(**DB_CONFIG, login_timeout=10, timeout=60)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute('SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=%s', (TABLE,))
        if cur.fetchone():
            cur.execute(f'SELECT * FROM [{TABLE}] ORDER BY review_time,source_id')
            return [{**row, 'id': row['source_id']} for row in cur.fetchall()]
        cur.execute(
            """SELECT * FROM yujing_scholar
               WHERE scholar=%s AND review_time < %s
               ORDER BY review_time, id""",
            (SCHOLAR, SWITCH),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def make_input_map():
    result = {}
    for path in sorted(INPUT_ROOT.iterdir()):
        if not path.is_dir():
            continue
        doi = normalize_doi(dir_name_to_doi(path))
        if doi:
            result[doi.casefold()] = path
    return result


def create_temp_table(rows):
    conn = pymssql.connect(**DB_CONFIG, login_timeout=10, timeout=60)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=%s",
            (TABLE,),
        )
        if cur.fetchone():
            print(f"Reusing existing temporary comparison table: {TABLE}", flush=True)
            return
        cur.execute(f"SELECT TOP 0 * INTO [{TABLE}] FROM yujing_scholar")
        cols = [
            ("source_id", "int"),
            ("comparison_run_id", "nvarchar(100)"),
            ("source_review_result", "nvarchar(50)"),
            ("source_review_time", "datetime"),
            ("source_review_report_url", "nvarchar(500)"),
            ("m31_model", "nvarchar(100)"),
            ("m31_review_result", "nvarchar(50)"),
            ("m31_review_report_url", "nvarchar(500)"),
            ("m31_review_time", "datetime"),
            ("m31_result_json", "nvarchar(max)"),
            ("comparison_status", "nvarchar(50)"),
        ]
        cur.execute(f"ALTER TABLE [{TABLE}] ADD " + ", ".join(f"[{n}] {t} NULL" for n, t in cols))
        base_cols = [
            "title", "author", "author_type", "department", "author_all",
            "department_all", "journal", "doi", "page", "pic_num", "pic_score",
            "pic_risk_level", "data_num", "data_score", "data_risk_level", "ref_num",
            "ref_score", "ref_risk_level", "total_score", "risk_level", "report_url",
            "generation_time", "review_result", "review_report_url", "review_time",
            "status", "scholar", "college",
        ]
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("%s" for _ in ids)
            insert_cols = ["source_id", *base_cols]
            select_cols = ["id", *base_cols]
            cur.execute(
                f"INSERT INTO [{TABLE}] ({','.join('[' + c + ']' for c in insert_cols)}) "
                f"SELECT {','.join('[' + c + ']' for c in select_cols)} FROM yujing_scholar WHERE id IN ({placeholders})",
                tuple(ids),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_temp_table(rows_by_doi, results):
    from scripts.converge_m31_comparison import publish
    return publish(NAMESPACE, results, 'batch')


async def review_all(items):
    semaphore = asyncio.Semaphore(max(1, WORKERS))
    results = []
    lock = asyncio.Lock()
    checkpoint = OUT_ROOT / "m31_review_results.json"
    from scripts.converge_m31_comparison import attach_saved_evidence, valid
    saved = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else []
    saved_by_doi = {row['doi'].casefold(): attach_saved_evidence(OUT_ROOT, row) for row in saved}

    async def one(item):
        async with semaphore:
            started = time.monotonic()
            try:
                cached = saved_by_doi.get(item['doi'].casefold())
                result = cached if cached and valid(cached) else await run_review_single(
                    doi=item["doi"],
                    report_json_path=str(item["report_json"]),
                    input_dir=str(item["input_dir"]),
                    output_dir=str(OUT_ROOT),
                    resume=True,
                )
            except Exception as exc:
                result = {
                    "doi": item["doi"],
                    "trigger": "review_error",
                    "_review_error": str(exc),
                }
            result.setdefault("_comparison_elapsed_seconds", round(time.monotonic() - started, 1))
            result["_report_json"] = str(item["report_json"])
            result["_input_dir"] = str(item["input_dir"])
            async with lock:
                results.append(result)
                checkpoint.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                print(f"[{len(results)}/{len(items)}] {item['doi']} -> {result.get('result', result.get('trigger'))}", flush=True)
            return result

    await asyncio.gather(*(one(item) for item in items))
    results.sort(key=lambda x: str(x.get("doi", "")))
    checkpoint.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return results


def render(results):
    path = OUT_ROOT / "m31_review_results.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    env = os.environ.copy()
    env["PAPER_INTEGRITY_ROOT"] = str(BASE)
    env["REVIEW_COVERAGE_FAILURE_POLICY"] = "warn"
    env["REVIEW_COVERAGE_GAP_POLICY"] = "strict"
    cmd = [
        sys.executable, str(BASE / "scripts/generate_review_report.py"),
        "--results", str(path), "--output", str(REPORT_DIR),
        "--table", TABLE, "--namespace", NAMESPACE, "--scholar", SCHOLAR,
        "--no-db",
    ]
    proc = subprocess.run(cmd, cwd=BASE, env=env, check=False)
    if proc.returncode not in (0, 2):
        raise RuntimeError(f"Report generation failed with return code {proc.returncode}")


def main():
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    rows = db_rows()
    if not (30 <= len(rows) <= 50):
        raise RuntimeError(f"Selected scholar batch has {len(rows)} papers, expected 30-50")
    inputs = make_input_map()
    items = []
    for row in rows:
        doi = normalize_doi(str(row.get("doi") or "")) or str(row.get("doi") or "").strip()
        input_dir = inputs.get(doi.casefold())
        if not input_dir:
            raise RuntimeError(f"No source directory found for DOI {doi}")
        report_json = BASE / "data/output/yujing_scholar/init" / input_dir.name / "report.json"
        if not report_json.is_file():
            raise RuntimeError(f"No initial report JSON found: {report_json}")
        items.append({"id": row["id"], "doi": doi, "input_dir": input_dir, "report_json": report_json})
    print(f"Selected {SCHOLAR}: {len(items)} pre-switch papers; workers={WORKERS}; model=MiniMax-M3.1-Flash", flush=True)
    create_temp_table(rows)
    results = asyncio.run(review_all(items))
    from scripts.converge_m31_comparison import publish
    publish(NAMESPACE, results, 'batch')
    result_by_doi = {
        (normalize_doi(str(r.get("doi") or "")) or str(r.get("doi") or "")).casefold(): r
        for r in results
    }
    comparison_rows = []
    for source in rows:
        doi = normalize_doi(str(source.get("doi") or "")) or str(source.get("doi") or "")
        result = result_by_doi.get(doi.casefold(), {})
        m31_result = result.get("result") if result.get("trigger") != "review_error" else "复核错误"
        comparison_rows.append({
            "doi": doi,
            "title": source.get("title") or "",
            "scholar": source.get("scholar") or SCHOLAR,
            "source_model": "MiniMax-M3",
            "source_result": source.get("review_result") or "",
            "m31_model": "MiniMax-M3.1-Flash",
            "m31_result": m31_result,
            "changed": "是" if m31_result not in {"复核错误", source.get("review_result") or ""} else "否",
            "source_review_time": str(source.get("review_time") or ""),
            "m31_review_time": datetime.now().isoformat(timespec="seconds"),
            "m31_report_url": f"http://202.120.57.100/review_reports/{NAMESPACE}/review_{doi_to_slug(doi)}.pdf" if m31_result in {"高风险", "低风险"} else "",
            "elapsed_seconds": result.get("_comparison_elapsed_seconds", ""),
        })
    with (OUT_ROOT / "comparison.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison_rows[0]))
        writer.writeheader()
        writer.writerows(comparison_rows)
    source_high = sum(row["source_result"] == "高风险" for row in comparison_rows)
    m31_high = sum(row["m31_result"] == "高风险" for row in comparison_rows)
    changed = sum(row["changed"] == "是" for row in comparison_rows)
    summary = {
        "table": TABLE,
        "namespace": NAMESPACE,
        "scholar": SCHOLAR,
        "source_model_assumption": "MiniMax-M3",
        "rerun_model": "MiniMax-M3.1-Flash",
        "papers": len(results),
        "completed": sum(r.get("trigger") != "review_error" and r.get("result") in {"高风险", "低风险"} for r in results),
        "review_errors": sum(r.get("trigger") == "review_error" for r in results),
        "source_high": source_high,
        "source_low": len(comparison_rows) - source_high,
        "m31_high": m31_high,
        "m31_low": sum(row["m31_result"] == "低风险" for row in comparison_rows),
        "changed_conclusions": changed,
        "output_root": str(OUT_ROOT),
    }
    (OUT_ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
