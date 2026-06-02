#!/usr/bin/env python3
"""Re-run data anomaly detection and rescore all low-risk papers in yujing_quanliang_v2.
Does NOT touch yujing_quanliang. Skips papers already marked as high-risk."""
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import fitz
import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.chinese_report_generator import (
    _build_full_html, _render_pdf, _make_filename,
    _compute_overall_risk, _compute_dimension_risk, _apply_data_caps,
)
from modules.data_checker import check_data_anomalies

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("rescore-v2")

BASE = Path(__file__).resolve().parent
TABLE = "yujing_quanliang_v2"

DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}

OUTPUT_DIRS = [
    BASE / "data" / "output" / "0514",
    BASE / "data" / "output" / "Nature-2",
    BASE / "data" / "output" / "nature-3",
    BASE / "data" / "output",
]

INPUT_DIRS = [
    BASE / "data" / "input" / "Nature0514",
    BASE / "data" / "input" / "Nature-2",
    BASE / "data" / "input" / "nature-3",
    BASE / "data" / "input" / "science99",
    BASE / "data" / "input" / "science_doi_list-001-222",
    BASE / "data" / "input" / "science_doi_list-323-443",
    BASE / "data" / "input" / "wiley-to-science",
    BASE / "data" / "input" / "cell-1",
    BASE / "data" / "input" / "cell-2",
    BASE / "data" / "input" / "cell-99",
    BASE / "data" / "input" / "db-matched",
    BASE / "data" / "input" / "db-unmatched",
    BASE / "data" / "input" / "pnas数据集",
    BASE / "data" / "input" / "医学刊pnas",
    BASE / "data" / "input" / "retracted-ncomms",
    BASE / "data" / "input" / "test-set",
    BASE / "data" / "input" / "20260520-649",
    BASE / "data" / "input" / "20260521",
    BASE / "data" / "input" / "20260522",
    BASE / "data" / "input" / "20260523",
    BASE / "data" / "input" / "0525",
]

ANALYSIS_START_RE = re.compile(r'^三、')
ANALYSIS_END_RE = re.compile(r'^(六、免责声明|免责声明)')

_lock = threading.Lock()
_high_count = 0
_processed = 0
_total_papers = 0


def _find_report(doi: str) -> str | None:
    doi_clean = doi.replace("https://doi.org/", "")
    slugs = [
        doi_clean.replace("/", "__"),
        doi_clean.replace("/", "_"),
    ]
    for slug in slugs:
        for odir in OUTPUT_DIRS:
            rpath = odir / slug / "report.json"
            if rpath.exists():
                return str(rpath)
    return None


def _find_input_dir(doi: str) -> str | None:
    doi_clean = doi.replace("https://doi.org/", "")
    candidates = [
        doi_clean.replace("/", "__"),
        doi_clean.replace("/", "_"),
        doi_clean.replace("/", ""),
    ]
    parts = doi_clean.split("/", 1)
    if len(parts) == 2:
        candidates.append(parts[0] + parts[1])

    for idir in INPUT_DIRS:
        if not idir.exists():
            continue
        for dirname in candidates:
            candidate = idir / dirname
            if candidate.exists():
                for sub in ("extended_data", "source_data"):
                    sd = candidate / sub
                    if sd.exists() and any(sd.iterdir()):
                        return str(candidate)
                if any(candidate.rglob("*.xlsx")) or any(candidate.rglob("*.xls")) or any(candidate.rglob("*.csv")):
                    return str(candidate)

    for idir in INPUT_DIRS:
        if not idir.exists():
            continue
        for subdir in idir.iterdir():
            if not subdir.is_dir():
                continue
            if any(c in subdir.name for c in candidates):
                if any(subdir.rglob("*.xlsx")) or any(subdir.rglob("*.csv")):
                    return str(subdir)
    return None


def _find_chinese_pdf(doi: str, title: str) -> str | None:
    filename = _make_filename(doi, title)
    for odir in OUTPUT_DIRS:
        cn_dir = odir / "chinese_reports"
        pdf_path = cn_dir / filename
        if pdf_path.exists():
            return str(pdf_path)
    cn_dir = BASE / "data" / "output" / "chinese_reports"
    pdf_path = cn_dir / filename
    if pdf_path.exists():
        return str(pdf_path)
    return None


def _extract_analysis_from_pdf(pdf_path: str) -> str | None:
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
    except Exception:
        return None

    lines = full_text.split("\n")
    start_idx = end_idx = None
    for i, line in enumerate(lines):
        if start_idx is None and ANALYSIS_START_RE.match(line.strip()):
            start_idx = i
        elif start_idx is not None and ANALYSIS_END_RE.match(line.strip()):
            end_idx = i
            break

    if start_idx is None or end_idx is None:
        return None

    html_parts = []
    i = start_idx
    while i < end_idx:
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if re.match(r'^[三四五六七八九十]+、', line):
            html_parts.append(f'<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">{line}</h2>')
        elif re.match(r'^（[一二三四五六七八九十]+）', line):
            html_parts.append(f'<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">{line}</p>')
        elif line.startswith("• ") or line.startswith("· "):
            html_parts.append(f'<p style="font-size:9.5pt; color:#333; margin-bottom:3pt; margin-left:12pt;">{line}</p>')
        else:
            html_parts.append(f'<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">{line}</p>')
        i += 1

    return "\n".join(html_parts)


def _update_v2(row: dict):
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    sql = f"""UPDATE {TABLE} SET
        total_score=%(total_score)s, risk_level=%(risk_level)s,
        pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
        data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
        ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
        report_url=%(report_url)s, generation_time=%(generation_time)s,
        data_num=%(data_num)s
    WHERE doi=%(doi)s"""
    cursor.execute(sql, row)
    conn.commit()
    conn.close()


def _check_high_risk_ratio():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(f"SELECT risk_level, COUNT(*) FROM {TABLE} GROUP BY risk_level")
    counts = {r[0]: r[1] for r in cursor.fetchall()}
    conn.close()
    total = sum(counts.values())
    high = counts.get("高风险", 0)
    ratio = high / total * 100 if total else 0
    return high, total, ratio


def process_one(doi: str) -> dict:
    global _high_count, _processed
    t0 = time.time()
    result = {"doi": doi, "status": "skipped", "old_level": "低风险", "new_level": "低风险"}

    rpath = _find_report(doi)
    if not rpath:
        result["status"] = "no_report"
        return result

    try:
        with open(rpath) as f:
            findings = json.load(f)
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result

    paper = findings.get("paper", {})
    title = paper.get("title", "unknown")

    old_overall = _compute_overall_risk(findings)
    result["old_score"] = old_overall["score"]

    input_dir = _find_input_dir(doi)
    if input_dir:
        try:
            new_anomalies = check_data_anomalies(input_dir)
            findings["data_anomalies"] = new_anomalies
            with open(rpath, "w") as f:
                json.dump(findings, f, ensure_ascii=False, indent=2)
            result["redetected"] = True
            result["anomaly_count"] = len(new_anomalies)
        except Exception as e:
            log.warning("Data redetection failed for %s: %s", doi, e)
            result["redetect_error"] = str(e)

    new_overall = _compute_overall_risk(findings)
    result["new_score"] = new_overall["score"]
    result["new_level"] = new_overall["level"]

    image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))

    report_url = f"http://10.119.9.99/chinese_reports/{_make_filename(doi, title)}"

    db_row = {
        "doi": doi,
        "total_score": str(new_overall["score"]),
        "risk_level": new_overall["level"],
        "pic_score": str(image_risk["score"]),
        "pic_risk_level": image_risk["level"],
        "data_score": str(data_risk["score"]),
        "data_risk_level": data_risk["level"],
        "ref_score": str(ref_risk["score"]),
        "ref_risk_level": ref_risk["level"],
        "report_url": report_url,
        "generation_time": datetime.now(),
        "data_num": len(findings.get("data_anomalies", [])),
    }

    try:
        _update_v2(db_row)
        result["status"] = "updated"
    except Exception as e:
        result["status"] = "db_error"
        result["error"] = str(e)
        return result

    pdf_path = _find_chinese_pdf(doi, title)
    if pdf_path:
        analysis_html = _extract_analysis_from_pdf(pdf_path)
        if analysis_html:
            try:
                full_html = _build_full_html(findings, analysis_html)
                _render_pdf(full_html, pdf_path)
                result["pdf_updated"] = True
            except Exception:
                pass

    if new_overall["level"] != old_overall["level"]:
        result["level_changed"] = True

    with _lock:
        _processed += 1
        if new_overall["level"] == "高风险":
            _high_count += 1

    result["elapsed"] = round(time.time() - t0, 1)
    return result


def main():
    global _high_count, _processed, _total_papers

    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute(f"SELECT doi FROM {TABLE} WHERE risk_level=N'低风险' AND doi IS NOT NULL")
    dois = [r[0] for r in cursor.fetchall()]
    cursor.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE risk_level=N'高风险'")
    existing_high = cursor.fetchone()[0]
    conn.close()

    _total_papers = len(dois) + existing_high
    log.info("Papers to rescore: %d low-risk (+ %d existing high-risk = %d total)", len(dois), existing_high, _total_papers)

    high, total, ratio = _check_high_risk_ratio()
    log.info("Current ratio: %d/%d = %.1f%%", high, total, ratio)

    max_workers = 8
    level_changes = []
    errors = []
    no_report = 0
    redetected = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_one, doi): doi for doi in dois}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            doi = result["doi"]

            if result["status"] == "no_report":
                no_report += 1
            elif result["status"] in ("error", "db_error"):
                errors.append(f"{doi}: {result.get('error', 'unknown')}")
            elif result.get("level_changed"):
                level_changes.append(f"{doi}: 低风险({result['old_score']}) -> {result['new_level']}({result['new_score']})")

            if result.get("redetected"):
                redetected += 1

            if i % 100 == 0:
                high, total, ratio = _check_high_risk_ratio()
                log.info(
                    "[%d/%d] updated=%d redetected=%d no_report=%d upgrades=%d errors=%d | RATIO: %d/%d = %.1f%%",
                    i, len(dois), _processed, redetected, no_report, len(level_changes), len(errors),
                    high, total, ratio,
                )
                if ratio > 20.0:
                    log.warning("HIGH-RISK RATIO EXCEEDED 20%%! Current: %.1f%% — stopping.", ratio)
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

    high, total, ratio = _check_high_risk_ratio()

    log.info("=" * 60)
    log.info("FINAL RESULTS")
    log.info("=" * 60)
    log.info("Processed: %d/%d", _processed, len(dois))
    log.info("Redetected data: %d", redetected)
    log.info("No report.json: %d", no_report)
    log.info("Level changes (低->高): %d", len(level_changes))
    log.info("Errors: %d", len(errors))
    log.info("FINAL RATIO: %d/%d = %.1f%%", high, total, ratio)

    status = "PASS" if ratio <= 20.0 else "FAIL"
    log.info("Constraint check (<=20%%): %s", status)

    if level_changes:
        log.info("--- Level upgrades (%d) ---", len(level_changes))
        for c in level_changes:
            log.info("  %s", c)

    if errors:
        log.warning("--- Errors (%d) ---", len(errors))
        for e in errors[:30]:
            log.warning("  %s", e)

    return 0 if ratio <= 20.0 else 1


if __name__ == "__main__":
    sys.exit(main())
