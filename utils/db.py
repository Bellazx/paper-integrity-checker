import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

import pymssql

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger(__name__)

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

REPORT_BASE_URL = "http://10.119.9.99/chinese_reports"

SJTU_KEYWORDS = [
    "Shanghai Jiao Tong", "Shanghai Jiaotong", "Jiaotong University",
    "上海交通", "交通大学", "SJTU",
]

_conn_local = threading.local()


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
        with open(html_path, "r", encoding="utf-8") as f:
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


def _make_report_url(findings: dict, chinese_reports_dir: str = None) -> str:
    from modules.chinese_report_generator import _make_filename
    doi = findings["paper"].get("doi", "unknown")
    title = findings["paper"].get("title", "unknown")
    filename = _make_filename(doi, title)
    return f"{REPORT_BASE_URL}/{filename}"


def insert_findings(findings: dict, chinese_reports_dir: str = None, html_path: str = None):
    """Insert analysis results into SQL Server."""
    from modules.chinese_report_generator import _compute_dimension_risk, _compute_overall_risk, _apply_data_caps

    paper = findings.get("paper", {})
    summary = findings.get("summary", {})

    image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    capped_data = _apply_data_caps(findings.get("data_anomalies", []))
    data_risk = _compute_dimension_risk(capped_data)
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)

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

    report_url = _make_report_url(findings, chinese_reports_dir)

    row = {
        "title": (paper.get("title") or "")[:500],
        "author": ", ".join(sjtu_authors)[:500] if sjtu_authors else (paper.get("author") or "")[:500],
        "author_type": sjtu_type or "通讯作者",
        "department": "; ".join(sjtu_depts)[:500] if sjtu_depts else "",
        "author_all": ", ".join(authors_full) if authors_full else (paper.get("author") or ""),
        "department_all": "; ".join(affiliations) if affiliations else "",
        "journal": (paper.get("journal") or "")[:200],
        "doi": (paper.get("doi") or "")[:200],
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

    if overall["level"] == "低风险":
        sql_update = """UPDATE yujing_quanliang SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
            journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s, data_num=%(data_num)s, ref_num=%(ref_num)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s,
            review_result=NULL, review_report_url=NULL, review_time=NULL
        WHERE doi=%(doi)s"""
    else:
        sql_update = """UPDATE yujing_quanliang SET
            title=%(title)s, author=%(author)s, author_type=%(author_type)s,
            department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
            journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s, data_num=%(data_num)s, ref_num=%(ref_num)s,
            total_score=%(total_score)s, risk_level=%(risk_level)s,
            pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
            data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
            ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
            report_url=%(report_url)s, generation_time=%(generation_time)s
        WHERE doi=%(doi)s"""

    sql_insert = """INSERT INTO yujing_quanliang (
        title, author, author_type, department, author_all, department_all,
        journal, doi, page, pic_num, data_num, ref_num,
        total_score, risk_level, pic_score, pic_risk_level,
        data_score, data_risk_level, ref_score, ref_risk_level,
        report_url, generation_time
    ) VALUES (
        %(title)s, %(author)s, %(author_type)s, %(department)s, %(author_all)s, %(department_all)s,
        %(journal)s, %(doi)s, %(page)s, %(pic_num)s, %(data_num)s, %(ref_num)s,
        %(total_score)s, %(risk_level)s, %(pic_score)s, %(pic_risk_level)s,
        %(data_score)s, %(data_risk_level)s, %(ref_score)s, %(ref_risk_level)s,
        %(report_url)s, %(generation_time)s
    )"""

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT risk_level, review_result FROM yujing_quanliang WHERE doi=%(doi)s", row)
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
            cursor.execute(sql_update, row)
            conn.commit()
            log.info("Updated in DB: DOI=%s, score=%s/%s", row["doi"], row["total_score"], row["risk_level"])
        else:
            cursor.execute(sql_insert, row)
            conn.commit()
            log.info("Inserted into DB: DOI=%s, score=%s/%s", row["doi"], row["total_score"], row["risk_level"])
    except Exception as e:
        log.error("Failed to insert into DB (DOI=%s): %s", row["doi"], e)
        try:
            conn.rollback()
        except Exception:
            pass
        _conn_local.conn = None
