#!/usr/bin/env python3
"""Re-score all papers with updated scoring logic. No LLM calls — reuses analysis text from existing PDFs.
Also re-runs data anomaly detection to pick up improved IV keyword filtering."""
import json
import logging
import re
import sys
from pathlib import Path

import fitz
import pymssql

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.chinese_report_generator import (
    _build_full_html, _render_pdf, _make_filename,
    _compute_overall_risk, _compute_dimension_risk, _apply_data_caps,
)
from modules.data_checker import check_data_anomalies
from utils.db import insert_findings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("rescore")

BASE = Path(__file__).resolve().parent
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
    BASE / "data" / "input" / "wiley-to-science",
    BASE / "data" / "input" / "cell-1-extracted",
    BASE / "data" / "input" / "cell-2" / "cell-2全",
]
DB_CONFIG = {
    "server": "10.119.5.44", "user": "yujing",
    "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8",
}

ANALYSIS_START_RE = re.compile(r'^三、')
ANALYSIS_END_RE = re.compile(r'^(六、免责声明|免责声明)')


def _extract_analysis_from_pdf(pdf_path: str) -> str | None:
    """Extract the LLM analysis text (sections 三-五) from an existing Chinese PDF."""
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
    except Exception as e:
        log.warning("Cannot read PDF %s: %s", pdf_path, e)
        return None

    lines = full_text.split("\n")
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if start_idx is None and ANALYSIS_START_RE.match(line.strip()):
            start_idx = i
        elif start_idx is not None and ANALYSIS_END_RE.match(line.strip()):
            end_idx = i
            break

    if start_idx is None or end_idx is None:
        return None

    analysis_lines = lines[start_idx:end_idx]
    return _lines_to_html(analysis_lines)


def _lines_to_html(lines: list[str]) -> str:
    """Convert extracted text lines back into simple HTML for PDF rendering."""
    html_parts = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if re.match(r'^[三四五六七八九十]+、', line):
            html_parts.append(
                f'<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">{line}</h2>'
            )
            i += 1
            continue

        if re.match(r'^（[一二三四五六七八九十]+）', line):
            html_parts.append(
                f'<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:4pt;">{line}</p>'
            )
            i += 1
            continue

        if line.startswith("• ") or line.startswith("· "):
            para = line
            i += 1
            while i < len(lines):
                next_line = lines[i].strip()
                if not next_line or next_line.startswith("• ") or next_line.startswith("· ") or \
                   re.match(r'^[三四五六七八九十]+、', next_line) or \
                   re.match(r'^（[一二三四五六七八九十]+）', next_line):
                    break
                para += next_line
                i += 1
            html_parts.append(
                f'<p style="font-size:9.5pt; color:#333; margin-bottom:3pt; margin-left:12pt;">{para}</p>'
            )
            continue

        para = line
        i += 1
        while i < len(lines):
            next_line = lines[i].strip()
            if not next_line or next_line.startswith("• ") or next_line.startswith("· ") or \
               re.match(r'^[三四五六七八九十]+、', next_line) or \
               re.match(r'^（[一二三四五六七八九十]+）', next_line):
                break
            para += next_line
            i += 1
        html_parts.append(
            f'<p style="font-size:9.5pt; color:#333; margin-bottom:4pt;">{para}</p>'
        )

    return "\n".join(html_parts)


def _find_report(doi: str) -> str | None:
    doi_dir = doi.replace("https://doi.org/", "").replace("/", "__")
    for odir in OUTPUT_DIRS:
        rpath = odir / doi_dir / "report.json"
        if rpath.exists():
            return str(rpath)
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


def _find_html_path(doi: str) -> str | None:
    doi_dir = doi.replace("https://doi.org/", "").replace("/", "__")
    for idir in INPUT_DIRS:
        hp = idir / doi_dir / "article.html"
        if hp.exists():
            return str(hp)
    return None


def _find_input_dir(doi: str) -> str | None:
    """Find the source data directory for a DOI."""
    doi_clean = doi.replace("https://doi.org/", "")
    candidates = [
        doi_clean.replace("/", "__"),
        doi_clean.replace("/", "_"),
    ]
    parts = doi_clean.split("/", 1)
    if len(parts) == 2:
        candidates.append(parts[0] + parts[1])

    for idir in INPUT_DIRS:
        for dirname in candidates:
            candidate = idir / dirname
            if candidate.exists():
                for sub in ("extended_data", "source_data"):
                    sd = candidate / sub
                    if sd.exists() and any(sd.iterdir()):
                        return str(candidate)
                if any(candidate.rglob("*.xlsx")):
                    return str(candidate)
    return None


def main():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi FROM yujing ORDER BY generation_time ASC")
    dois = [r[0] for r in cursor.fetchall()]
    conn.close()
    log.info("Got %d DOIs to rescore", len(dois))

    found = 0
    updated_db = 0
    updated_pdf = 0
    skipped_pdf = 0
    redetected = 0
    no_input = 0
    level_changes = []
    errors = []

    for i, doi in enumerate(dois):
        rpath = _find_report(doi)
        if not rpath:
            continue
        found += 1

        try:
            with open(rpath) as f:
                findings = json.load(f)

            paper = findings.get("paper", {})
            title = paper.get("title", "unknown")

            old_overall = _compute_overall_risk(findings)

            # --- Step 0: Re-run data anomaly detection if source data exists ---
            input_dir = _find_input_dir(doi)
            if input_dir:
                new_anomalies = check_data_anomalies(input_dir)
                old_count = len(findings.get("data_anomalies", []))
                findings["data_anomalies"] = new_anomalies
                new_count = len(new_anomalies)
                if old_count != new_count:
                    redetected += 1
                    with open(rpath, "w") as f:
                        json.dump(findings, f, ensure_ascii=False, indent=2)
            else:
                no_input += 1

            # --- Step 1: Try to regenerate PDF ---
            pdf_path = _find_chinese_pdf(doi, title)
            if pdf_path:
                analysis_html = _extract_analysis_from_pdf(pdf_path)
                if analysis_html:
                    full_html = _build_full_html(findings, analysis_html)
                    _render_pdf(full_html, pdf_path)
                    updated_pdf += 1
                else:
                    skipped_pdf += 1
                    log.warning("Cannot extract analysis from PDF for %s, skipping PDF regen", doi)
            else:
                skipped_pdf += 1

            # --- Step 2: Update DB ---
            html_path = _find_html_path(doi)
            cn_dir = str(Path(rpath).parent.parent / "chinese_reports")
            insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path)
            updated_db += 1

            new_overall = _compute_overall_risk(findings)
            if old_overall["level"] != new_overall["level"]:
                level_changes.append(
                    f'{doi}: {old_overall["level"]}({old_overall["score"]}) -> {new_overall["level"]}({new_overall["score"]})'
                )

            if (i + 1) % 50 == 0:
                log.info("Progress: %d/%d (db=%d, pdf=%d, pdf_skip=%d, redetect=%d, no_input=%d, level_chg=%d)",
                         i + 1, len(dois), updated_db, updated_pdf, skipped_pdf, redetected, no_input, len(level_changes))

        except Exception as e:
            errors.append(f"{doi}: {e}")
            log.error("Failed %s: %s", doi, e)

    log.info("Done: %d found, %d DB updated, %d PDFs regenerated, %d PDFs skipped, %d redetected, %d no_input, %d level changes, %d errors",
             found, updated_db, updated_pdf, skipped_pdf, redetected, no_input, len(level_changes), len(errors))
    if level_changes:
        log.info("Level changes:")
        for c in level_changes:
            log.info("  %s", c)
    if errors:
        log.warning("Errors (%d):", len(errors))
        for e in errors[:20]:
            log.warning("  %s", e)


if __name__ == "__main__":
    main()
