import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

import pymssql

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules.doi_utils import normalize_doi

log = logging.getLogger(__name__)

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

REPORT_BASE_URL = "http://202.120.57.100/chinese_reports"

SJTU_KEYWORDS = [
    "Shanghai Jiao Tong", "Shanghai Jiaotong", "Jiaotong University",
    "上海交通", "交通大学", "SJTU",
]

_conn_local = threading.local()


def _select_initial_report_url(
    generated_url: str,
    initial_risk_level: str | None,
    review_result: str | None,
    review_report_url: str | None,
) -> str:
    """Use confirmed review evidence only after initial screening is high itself.

    This deliberately does not change any initial score or risk level.  A low
    initial result stays low even when a later review is high; the detector must
    independently produce the high-risk result before its user-facing link can be
    aligned with the confirmed review report.
    """
    if (
        str(initial_risk_level or "").strip() == "高风险"
        and str(review_result or "").strip() == "高风险"
        and str(review_report_url or "").strip()
    ):
        return str(review_report_url).strip()
    return generated_url


def get_connection():
    if not hasattr(_conn_local, "conn") or _conn_local.conn is None:
        _conn_local.conn = pymssql.connect(**DB_CONFIG)
    return _conn_local.conn


def get_existing_dois() -> set[str]:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT doi FROM yujing_quanliang WHERE doi IS NOT NULL")
        return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        log.warning("Failed to query existing DOIs: %s", e)
        return set()


def _find_sjtu_authors(html_path: str) -> dict:
    """Find SJTU-affiliated authors and their departments from HTML JSON-LD.
    Returns dict with sjtu_authors and sjtu_departments."""
    result = {
        "sjtu_authors": [],
        "sjtu_departments": [],
    }

    try:
        with open(html_path, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return result

    jsonld_blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        content, re.DOTALL,
    )

    sjtu_authors = []
    sjtu_depts = set()

    for block in jsonld_blocks:
        try:
            data = json.loads(block)
            entity = data.get("mainEntity", data)
            if "author" not in entity:
                continue
            for a in entity["author"]:
                name = a.get("name", "")
                for af in a.get("affiliation", []):
                    addr = af.get("address", {})
                    aff_name = addr.get("name") or af.get("name", "")
                    if any(kw in aff_name for kw in SJTU_KEYWORDS):
                        if name and name not in sjtu_authors:
                            sjtu_authors.append(name)
                        if aff_name:
                            sjtu_depts.add(aff_name)
            break
        except (json.JSONDecodeError, AttributeError):
            continue

    result["sjtu_authors"] = sjtu_authors
    result["sjtu_departments"] = sorted(sjtu_depts)
    return result


def _make_report_url(findings: dict, chinese_reports_dir: str = None, namespace: str = "") -> str:
    """Build the public report URL for a paper.

    Precedence:
      1. ``namespace`` (e.g. the table name ``yujing_4050_202606``) is used directly
         — this is the cleanest path for the per-table layout where the URL is
         ``/chinese_reports/<namespace>/<file>``.
      2. Otherwise fall back to walking relative to ``OUTPUT_DIR/chinese_reports``
         so legacy call sites that only pass ``chinese_reports_dir`` keep working.
      3. With nothing else set, return ``/chinese_reports/<file>``.
    """
    from modules.chinese_report_generator import _make_filename
    from config import OUTPUT_DIR
    doi = findings["paper"].get("doi", "unknown")
    title = findings["paper"].get("title", "unknown")
    filename = _make_filename(doi, title)

    if namespace:
        return f"{REPORT_BASE_URL}/{namespace}/{filename}"

    base_url = REPORT_BASE_URL
    if chinese_reports_dir:
        try:
            root = (OUTPUT_DIR / "chinese_reports").resolve()
            rel = Path(chinese_reports_dir).resolve().relative_to(root)
            if str(rel) != ".":
                base_url = f"{REPORT_BASE_URL}/{rel.as_posix()}"
        except Exception:
            pass
    return f"{base_url}/{filename}"


def build_paper_metadata(findings: dict, html_path: str = None) -> dict:
    """Extract paper bibliographic metadata + SJTU author/department resolution from a
    findings dict. Returns RAW (untruncated) values; callers apply their own column-width
    truncation. Shared by insert_findings (yujing_* tables) and the detection_reports
    writer so the SJTU-author backfill logic lives in exactly one place."""
    paper = findings.get("paper", {})

    sjtu_authors, sjtu_depts, sjtu_type = [], [], ""
    if paper.get("sjtu_authors"):
        sjtu_authors = paper["sjtu_authors"]
        sjtu_depts = paper.get("sjtu_departments", [])
        sjtu_type = paper.get("sjtu_author_type", "")
    elif html_path:
        sjtu_info = _find_sjtu_authors(html_path)
        sjtu_authors = sjtu_info["sjtu_authors"]
        sjtu_depts = sjtu_info["sjtu_departments"]

    if not sjtu_depts:
        affiliations_list = paper.get("affiliations", [])
        for aff in affiliations_list:
            if any(kw in aff for kw in SJTU_KEYWORDS):
                clean = re.sub(r'^\d+\.\s*', '', aff.strip())
                sjtu_depts.append(clean)

    authors_full = paper.get("authors_full", [])
    affiliations = paper.get("affiliations", [])

    if not sjtu_authors and authors_full and affiliations:
        sjtu_aff_indices = set()
        for i, aff in enumerate(affiliations):
            if any(kw in aff for kw in SJTU_KEYWORDS):
                sjtu_aff_indices.add(i + 1)
        if sjtu_aff_indices:
            for author_name in authors_full:
                m = re.findall(r'(\d+)', author_name)
                if m:
                    indices = {int(x) for x in m}
                    if indices & sjtu_aff_indices:
                        clean_name = re.sub(r'\d+[,\s]*', '', author_name).strip().rstrip(',')
                        if clean_name:
                            sjtu_authors.append(clean_name)
            if not sjtu_authors:
                sjtu_authors = [authors_full[0].strip().rstrip(',')]

    return {
        "title": paper.get("title") or "",
        "author": ", ".join(sjtu_authors) if sjtu_authors else (paper.get("author") or ""),
        "author_type": sjtu_type or "通讯作者",
        "department": "; ".join(sjtu_depts) if sjtu_depts else "",
        "author_all": ", ".join(authors_full) if authors_full else (paper.get("author") or ""),
        "department_all": "; ".join(affiliations) if affiliations else "",
        "journal": paper.get("journal") or "",
        "doi": normalize_doi(paper.get("doi") or "") or (paper.get("doi") or ""),
    }


def insert_findings(
    findings: dict,
    chinese_reports_dir: str = None,
    html_path: str = None,
    namespace: str = "",
    source_path: str = "",
):
    """Insert analysis results into SQL Server."""
    from modules.chinese_report_generator import (
        _apply_data_caps, _apply_explainability_dimension_downgrade,
        _compute_dimension_risk, _compute_image_risk, _compute_overall_risk,
        _reconcile_risk_levels,
    )

    paper = findings.get("paper", {})
    summary = findings.get("summary", {})

    image_risk = _compute_image_risk(findings)
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)
    _apply_explainability_dimension_downgrade(findings, image_risk, data_risk, ref_risk)
    _reconcile_risk_levels(findings, overall, image_risk, data_risk, ref_risk)

    meta = build_paper_metadata(findings, html_path=html_path)

    table_name = "yujing_quanliang"
    if namespace:
        if re.match(r"^yujing_[a-z0-9_]{1,50}$", namespace):
            table_name = namespace
        else:
            log.warning("Ignoring unsafe DB namespace=%r; using %s", namespace, table_name)

    # ``source_path`` is currently part of the audit and comprehensive tables.
    # Keep the SQL for scholar/4050 tables unchanged because those schemas do
    # not expose this column yet.
    has_source_path = table_name in {"yujing_quanliang", "yujing_audit"}

    from core.pipeline import initial_pdf_skipped
    report_url = "" if initial_pdf_skipped(findings) else _make_report_url(findings, chinese_reports_dir, namespace=namespace)

    row = {
        "title": meta["title"][:500],
        "author": meta["author"][:500],
        "author_type": meta["author_type"],
        "department": meta["department"][:500],
        "author_all": meta["author_all"],
        "department_all": meta["department_all"],
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
        "report_url": report_url[:500],
        "generation_time": datetime.now(),
    }
    if has_source_path:
        row["source_path"] = (source_path or "")[:500]

    source_update = (
        ",\n            source_path=CASE WHEN NULLIF(LTRIM(RTRIM(%(source_path)s)), '') IS NULL "
        "THEN source_path ELSE %(source_path)s END"
        if has_source_path else ""
    )
    source_insert_column = ", source_path" if has_source_path else ""
    source_insert_value = ", %(source_path)s" if has_source_path else ""

    sql_update_clear_review = f"""UPDATE {table_name} SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
            journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s, data_num=%(data_num)s, ref_num=%(ref_num)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s,
            review_result=NULL, review_report_url=NULL, review_time=NULL{source_update}
        WHERE doi=%(doi)s"""
    sql_update_preserve_review = f"""UPDATE {table_name} SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
            journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s, data_num=%(data_num)s, ref_num=%(ref_num)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s
            {source_update}
        WHERE doi=%(doi)s"""

    sql_insert = f"""INSERT INTO {table_name} (
        title, author, author_type, department, author_all, department_all,
        journal, doi, page, pic_num, data_num, ref_num,
        total_score, risk_level, pic_score, pic_risk_level,
        data_score, data_risk_level, ref_score, ref_risk_level,
        report_url, generation_time{source_insert_column}
    ) VALUES (
        %(title)s, %(author)s, %(author_type)s, %(department)s, %(author_all)s, %(department_all)s,
        %(journal)s, %(doi)s, %(page)s, %(pic_num)s, %(data_num)s, %(ref_num)s,
        %(total_score)s, %(risk_level)s, %(pic_score)s, %(pic_risk_level)s,
        %(data_score)s, %(data_risk_level)s, %(ref_score)s, %(ref_risk_level)s,
        %(report_url)s, %(generation_time)s{source_insert_value}
    )"""

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT risk_level, review_result, review_report_url FROM {table_name} WHERE doi=%(doi)s",
            row,
        )
        existing = cursor.fetchone()
        if existing:
            protected_snapshot = Path(__file__).resolve().parent.parent / "data" / "protected_snapshot_2054.json"
            if protected_snapshot.exists():
                import json as _json
                with open(protected_snapshot) as _f:
                    _protected = _json.load(_f)
                if row["doi"] in _protected:
                    log.info("Skipped protected DOI=%s (risk=%s, review=%s)", row["doi"], existing[0], existing[1])
                    return
            row["report_url"] = _select_initial_report_url(
                row["report_url"], row["risk_level"], existing[1], existing[2]
            )
            sql_update = (
                sql_update_preserve_review
                if row["risk_level"] == "高风险"
                else sql_update_clear_review
            )
            cursor.execute(sql_update, row)
            conn.commit()
            log.info("Updated in DB %s: DOI=%s, score=%s/%s", table_name, row["doi"], row["total_score"], row["risk_level"])
        else:
            cursor.execute(sql_insert, row)
            conn.commit()
            log.info("Inserted into DB %s: DOI=%s, score=%s/%s", table_name, row["doi"], row["total_score"], row["risk_level"])
    except Exception as e:
        log.error("Failed to insert into DB (DOI=%s): %s", row["doi"], e)
        try:
            conn.rollback()
        except Exception:
            pass
        _conn_local.conn = None
