from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from api.config import (
    PYTHON, MAIN_PY, DEFAULT_TABLE, DETECT_TIMEOUT_SECONDS,
    DB_CONFIG, OUTPUT_DIR, sanitize_report_namespace,
)

log = logging.getLogger(__name__)


async def run_detection_single(
    input_dir: str,
    output_dir: str,
    skip_refs: bool = False,
    author_type: str = "",
    table_name: str = DEFAULT_TABLE,
    write_db: bool = True,
    doi: str = "",
    report_namespace: str | None = None,
) -> dict:
    # no_db: skip ALL DB writes from the detection subprocess + skip the custom-table
    # insert below. write_db=False (detection_reports flow) forces no_db regardless of
    # table_name, so the yujing tables are never touched.
    no_db = (not write_db) or (table_name != DEFAULT_TABLE)
    output_dir_path = Path(output_dir)
    output_root = output_dir_path.parent
    cmd = [
        str(PYTHON), str(MAIN_PY),
        "--input", input_dir,
        "--output", str(output_root),
    ]
    if skip_refs:
        cmd.append("--skip-refs")
    if author_type:
        cmd.extend(["--author-type", author_type])
    # A user-supplied DOI override is authoritative — pass it into detection so it
    # reaches findings, the report, the reference self-DOI filter, and DB metadata.
    if doi:
        cmd.extend(["--doi", doi])
    namespace = sanitize_report_namespace(report_namespace or table_name)
    cmd.extend(["--report-namespace", namespace])
    if no_db:
        cmd.append("--no-db")
    cmd.append("--force")

    log.info("Detection cmd: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(MAIN_PY.parent),
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=DETECT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"Detection timed out after {DETECT_TIMEOUT_SECONDS}s")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        log.error("Detection failed (rc=%d): %s", proc.returncode, err[-2000:])
        raise RuntimeError(f"Detection failed (rc={proc.returncode}): {err[-500:]}")

    report_json = Path(output_dir) / "report.json"
    if report_json.exists():
        with open(report_json) as f:
            findings = json.load(f)

        # This path runs main.py with --no-db, so main.py's own pdf_generated guard
        # never fires here — honor it before writing a record that would link to a
        # missing PDF.
        if write_db and table_name != DEFAULT_TABLE:
            if findings.get("pdf_generated", True):
                _insert_to_custom_table(findings, table_name, input_dir)
            else:
                log.warning("Skipping custom-table insert: PDF generation failed (DOI=%s)",
                            findings.get("paper", {}).get("doi", ""))

        return findings

    return {"error": "report.json not generated", "stdout": stdout.decode(errors="replace")[-1000:]}


async def run_detection_batch(
    input_root: str,
    output_root: str,
    skip_refs: bool = False,
    max_workers: int = 4,
    author_type: str = "",
    table_name: str = DEFAULT_TABLE,
    write_db: bool = True,
    report_namespace: str | None = None,
) -> dict:
    no_db = (not write_db) or (table_name != DEFAULT_TABLE)
    cmd = [
        str(PYTHON), str(MAIN_PY),
        "--batch", input_root,
        "--output", output_root,
        "--workers", str(max_workers),
    ]
    if skip_refs:
        cmd.append("--skip-refs")
    if author_type:
        cmd.extend(["--author-type", author_type])
    namespace = sanitize_report_namespace(report_namespace or table_name)
    cmd.extend(["--report-namespace", namespace])
    if no_db:
        cmd.append("--no-db")
    cmd.append("--force")

    log.info("Batch detection cmd: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(MAIN_PY.parent),
    )

    timeout = DETECT_TIMEOUT_SECONDS * max(1, max_workers)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"Batch detection timed out after {timeout}s")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"Batch detection failed (rc={proc.returncode}): {err[-500:]}")

    summary_path = Path(output_root) / "batch_summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)

        if write_db and table_name != DEFAULT_TABLE:
            for r in summary.get("results", []):
                if r.get("status") != "success":
                    continue
                doi_slug = r.get("paper", "")
                report_path = Path(output_root) / doi_slug / "report.json"
                if report_path.exists():
                    with open(report_path) as f:
                        findings = json.load(f)
                    _insert_to_custom_table(findings, table_name, "")

        return summary

    return {"stdout": stdout.decode(errors="replace")[-2000:]}


def _insert_to_custom_table(findings: dict, table_name: str, input_dir: str):
    import sys
    sys.path.insert(0, str(MAIN_PY.parent))
    from modules.chinese_report_generator import (
        _compute_dimension_risk, _compute_image_risk, _compute_overall_risk, _apply_data_caps,
    )
    from utils.db import _make_report_url, build_paper_metadata

    import pymssql

    paper = findings.get("paper", {})
    summary = findings.get("summary", {})

    # Resolve an HTML path for SJTU-author resolution (Nature crawls); mirrors main.py.
    html_path = None
    if input_dir:
        for cand in (Path(input_dir) / "article.html", Path(input_dir) / "html" / "article.html"):
            if cand.exists():
                html_path = str(cand)
                break

    image_risk = _compute_image_risk(findings)
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)

    if overall["level"] == "高风险":
        for dim in overall.get("high_dimensions", []):
            if dim == "data" and data_risk["level"] != "高风险":
                data_risk["level"] = "高风险"
                data_risk["color"] = "#c00"
            elif dim == "image" and image_risk["level"] != "高风险":
                image_risk["level"] = "高风险"
                image_risk["color"] = "#c00"

    from datetime import datetime

    # Reuse the shared metadata builder so SJTU author/department resolution and the
    # author_all/department_all backfill match the main yujing_quanliang path exactly,
    # instead of hardcoding department="" / author_type="通讯作者".
    meta = build_paper_metadata(findings, html_path=html_path)

    row = {
        "title": meta["title"][:500],
        "author": meta["author"][:500],
        "author_type": meta["author_type"],
        "department": meta["department"][:500],
        "author_all": meta["author_all"][:2000],
        "department_all": meta["department_all"][:2000],
        "journal": meta["journal"][:200],
        "doi": meta["doi"][:200],
        "page": paper.get("total_pages", 0),
        "pic_num": paper.get("total_images", 0),
        "data_num": summary.get("data_issues", 0),
        "ref_num": paper.get("total_references", 0),
        "total_score": str(overall["score"]),
        "risk_level": overall["level"],
        "pic_score": str(image_risk["score"]),
        "pic_risk_level": image_risk["level"],
        "data_score": str(data_risk["score"]),
        "data_risk_level": data_risk["level"],
        "ref_score": str(ref_risk["score"]),
        "ref_risk_level": ref_risk["level"],
        # Use the shared helper so the URL matches the actual PDF filename
        # (_make_filename = "{doi}_{title30}.pdf"), not a doi-slug-only guess.
        "report_url": _make_report_url(
            findings,
            chinese_reports_dir=str(OUTPUT_DIR / "chinese_reports" / sanitize_report_namespace(table_name)),
        )[:500],
        "generation_time": datetime.now(),
    }

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(f"SELECT 1 FROM {table_name} WHERE doi=%(doi)s", {"doi": row["doi"]})
    exists = cursor.fetchone()

    if exists:
        # Respect the locked-paper snapshot, same as utils.db.insert_findings — a
        # protected DOI must not be overwritten by a re-run on a custom table either.
        protected_snapshot = MAIN_PY.parent / "data" / "protected_snapshot_2054.json"
        if protected_snapshot.exists():
            with open(protected_snapshot) as _f:
                _protected = json.load(_f)
            if row["doi"] in _protected:
                log.info("Skipped protected DOI=%s in %s", row["doi"], table_name)
                conn.close()
                return
        set_parts = ", ".join(f"{k}=%({k})s" for k in row if k != "doi")
        cursor.execute(f"UPDATE {table_name} SET {set_parts} WHERE doi=%(doi)s", row)
    else:
        cols = ", ".join(row.keys())
        vals = ", ".join(f"%({k})s" for k in row.keys())
        cursor.execute(f"INSERT INTO {table_name} ({cols}) VALUES ({vals})", row)

    conn.commit()
    conn.close()
    log.info("Inserted/updated DOI=%s in %s", row["doi"], table_name)
