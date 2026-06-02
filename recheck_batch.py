#!/usr/bin/env python3
"""Re-check ALL papers in DB: apply new data_checker filters and update DB scores."""
import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules.data_checker import _is_independent_variable, _is_stat_column, _is_unnamed_column
from modules.chinese_report_generator import generate_chinese_pdf, _compute_dimension_risk, _compute_overall_risk
from core.nature_adapter import extract_text_from_html
from utils.db import insert_findings
import pymssql

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("recheck")

BASE = Path(__file__).resolve().parent
OUTPUT_DIRS = [BASE / "data" / "output" / "0514", BASE / "data" / "output" / "Nature-2", BASE / "data" / "output" / "nature-3", BASE / "data" / "output"]
DB_CONFIG = {"server": "10.119.5.44", "user": "yujing", "password": "fengxian_YJ514", "database": "lunwenyujing", "charset": "utf8"}


def _apply_filters(data_anomalies: list[dict]) -> list[dict]:
    """Apply the new false-positive filters to existing data anomaly findings."""
    filtered = []
    for a in data_anomalies:
        test = a["test"]
        loc = a["location"]
        details = a.get("details", {})

        col_name = ""
        if "column '" in loc:
            col_name = loc.split("column '")[1].rstrip("'")

        # Filter 1: skip IV columns for arith/geo/cv
        if test in ("arithmetic_sequence", "geometric_sequence", "coefficient_of_variation"):
            if col_name and _is_independent_variable(col_name):
                continue

        # Filter 2: skip perfect non-constant arithmetic sequences (preset params)
        if test == "arithmetic_sequence":
            dev = details.get("max_relative_deviation", -1)
            tp = details.get("type", "")
            if dev == 0 and tp != "constant":
                continue
            # Downgrade constant sequences to medium
            if tp == "constant" and a.get("severity") == "high":
                a = {**a, "severity": "medium"}

        # Filter 3: skip perfect non-constant geometric sequences
        if test == "geometric_sequence":
            dev = details.get("max_relative_deviation", -1)
            cr = details.get("common_ratio", 1)
            if dev == 0 and abs(cr - 1.0) > 1e-10:
                continue

        # Filter 4: skip slope≈1 + intercept≈0 linear dependencies (duplicate columns)
        if test == "linear_dependency":
            slope = details.get("slope", 0)
            intercept = details.get("intercept", 0)
            if abs(slope - 1.0) < 0.01 and abs(intercept) < 0.01:
                continue
            # Also skip if either column is an IV, stat, or unnamed column
            if "columns '" in loc:
                parts = loc.split("columns '")[1]
                col_a = parts.split("'")[0]
                col_b_part = parts.split("' vs '")
                if len(col_b_part) > 1:
                    col_b = col_b_part[1].rstrip("'")
                    if _is_independent_variable(col_a) or _is_independent_variable(col_b):
                        continue
                    if _is_stat_column(col_a) or _is_stat_column(col_b):
                        continue
                    if _is_unnamed_column(col_a) or _is_unnamed_column(col_b):
                        continue

        filtered.append(a)
    return filtered


def _find_report(doi: str) -> str | None:
    doi_dir = doi.replace("https://doi.org/", "").replace("/", "__")
    for odir in OUTPUT_DIRS:
        rpath = odir / doi_dir / "report.json"
        if rpath.exists():
            return str(rpath)
    return None


def main():
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("SELECT doi FROM yujing ORDER BY generation_time ASC")
    dois = [r[0] for r in cursor.fetchall()]
    conn.close()
    log.info("Got %d DOIs to recheck", len(dois))

    found = 0
    updated = 0
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

            old_data = findings.get("data_anomalies", [])
            new_data = _apply_filters(old_data)

            old_overall = _compute_overall_risk(findings)

            findings["data_anomalies"] = new_data

            high = sum(1 for a in new_data if a.get("severity") == "high")
            medium = sum(1 for a in new_data if a.get("severity") == "medium")
            low = sum(1 for a in new_data if a.get("severity") == "low")
            img_issues = len(findings.get("image_duplicates", []))
            ref_issues = len(findings.get("reference_issues", []))
            splice = findings.get("image_splicing", [])
            findings["summary"] = {
                "total_issues": len(new_data) + img_issues + ref_issues + len(splice),
                "high_severity": high + sum(1 for a in findings.get("image_duplicates", []) if a.get("severity") == "high") + sum(1 for a in findings.get("reference_issues", []) if a.get("severity") == "high"),
                # splice findings are always severity 'medium' (conservative pre-screen)
                "medium_severity": medium + sum(1 for a in findings.get("image_duplicates", []) if a.get("severity") == "medium") + sum(1 for a in findings.get("reference_issues", []) if a.get("severity") == "medium") + len(splice),
                "low_severity": low + sum(1 for a in findings.get("image_duplicates", []) if a.get("severity") == "low") + sum(1 for a in findings.get("reference_issues", []) if a.get("severity") == "low"),
                "data_issues": len(new_data),
                "image_issues": img_issues,
                "image_splicing_suspects": len(splice),
                "reference_issues": ref_issues,
            }

            new_overall = _compute_overall_risk(findings)

            # Find html_path for SJTU author extraction
            doi_dir = doi.replace("https://doi.org/", "").replace("/", "__")
            html_path = None
            for input_root in [BASE / "data" / "input" / "Nature0514", BASE / "data" / "input" / "Nature-2", BASE / "data" / "input" / "nature-3"]:
                hp = input_root / doi_dir / "article.html"
                if hp.exists():
                    html_path = str(hp)
                    break

            cn_dir = str(Path(rpath).parent.parent / "chinese_reports")
            insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path)
            updated += 1

            if old_overall["level"] != new_overall["level"]:
                level_changes.append(f'{doi}: {old_overall["level"]}({old_overall["score"]}) -> {new_overall["level"]}({new_overall["score"]})')
                # Regenerate Chinese PDF
                if html_path:
                    try:
                        first_pages_text = extract_text_from_html(html_path)[:6000]
                        saved_authors = findings["paper"].get("authors_full", [])[:]
                        saved_affiliations = findings["paper"].get("affiliations", [])[:]
                        cn_path, _ = generate_chinese_pdf(findings, cn_dir, first_pages_text)
                        findings["paper"]["authors_full"] = saved_authors
                        findings["paper"]["affiliations"] = saved_affiliations
                        if cn_path:
                            log.info("Regenerated PDF: %s", cn_path)
                    except Exception as e:
                        log.error("Failed to regenerate PDF for %s: %s", doi, e)

                # Save updated report.json
                with open(rpath, "w", encoding="utf-8") as f:
                    json.dump(findings, f, ensure_ascii=False, indent=2, default=str)

            if (i + 1) % 50 == 0:
                log.info("Progress: %d/%d processed, %d updated", i + 1, len(dois), updated)

        except Exception as e:
            errors.append(f"{doi}: {e}")
            log.error("Failed %s: %s", doi, e)

    log.info("Done: %d found, %d updated, %d level changes, %d errors", found, updated, len(level_changes), len(errors))
    if level_changes:
        log.info("Level changes:")
        for c in level_changes:
            log.info("  %s", c)
    if errors:
        log.warning("Errors:")
        for e in errors[:10]:
            log.warning("  %s", e)


if __name__ == "__main__":
    main()
