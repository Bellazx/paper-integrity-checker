#!/usr/bin/env python3
"""Audit and export the 64-row 4050 reviewed-high-risk multiset delta."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
sys.path.insert(0, str(BASE))

import pymssql  # noqa: E402

from utils.db import DB_CONFIG  # noqa: E402


NEW_TABLE = "yujing_4050_202607"
# The user's stated 188-row baseline is yujing_4050. The similarly named
# yujing_4050_202606 currently contains only 101 reviewed-high-risk rows.
BASELINE_TABLE = "yujing_4050"
EXPECTED_ROWS = 64
EXPORT_NAME = "yujing_4050_202607_review_high_delta_64_20260918"
EXPORT_DIR = BASE / "data" / "downloads" / EXPORT_NAME
ZIP_PATH = EXPORT_DIR.with_suffix(".zip")
REPORT_DIR = BASE / "data" / "output" / NEW_TABLE / "review_v2"
INPUT_ROOTS = (
    BASE / "data" / "input" / "20260716",
    BASE / "data" / "input" / "20260728",
    BASE / "data" / "input" / "8月八大刊新增",
    BASE / "data" / "input" / "9月八大刊新增",
    BASE / "data" / "input" / "4050-matched",
)


def fetch_delta_rows() -> list[dict]:
    sql = f"""
    WITH newer AS (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY LOWER(LTRIM(RTRIM(doi))) ORDER BY id
        ) AS occurrence_number
        FROM {NEW_TABLE}
        WHERE review_result=N'高风险'
    ), baseline AS (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY LOWER(LTRIM(RTRIM(doi))) ORDER BY id
        ) AS occurrence_number
        FROM {BASELINE_TABLE}
        WHERE review_result=N'高风险'
    )
    SELECT newer.id, newer.doi, newer.title, newer.review_time,
           newer.review_result, newer.review_report_url
    FROM newer
    LEFT JOIN baseline
      ON LOWER(LTRIM(RTRIM(newer.doi)))=LOWER(LTRIM(RTRIM(baseline.doi)))
     AND newer.occurrence_number=baseline.occurrence_number
    WHERE baseline.id IS NULL
    ORDER BY newer.id
    """
    connection = pymssql.connect(**DB_CONFIG)
    try:
        cursor = connection.cursor(as_dict=True)
        cursor.execute(sql)
        rows = cursor.fetchall()
    finally:
        connection.close()
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"Expected {EXPECTED_ROWS} delta rows, found {len(rows)}")
    return rows


def safe_doi(doi: str) -> str:
    return doi.replace("/", "_")


def build_source_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for root in INPUT_ROOTS:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if not path.is_dir():
                continue
            name = path.name.casefold()
            index.setdefault(name, []).append(path)
            match = re.match(r"^\d+_(10\..+)$", path.name, re.I)
            if match:
                legacy_name = match.group(1).replace("__", "_", 1).casefold()
                index.setdefault(legacy_name, []).append(path)
    return index


def find_source(row: dict, index: dict[str, list[Path]]) -> Path:
    matches = list(dict.fromkeys(index.get(safe_doi(row["doi"]).casefold(), [])))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one source directory for id={row['id']} doi={row['doi']}, "
            f"found {len(matches)}: {matches}"
        )
    return matches[0]


def find_report(row: dict) -> Path:
    candidates = []
    url_name = str(row.get("review_report_url") or "").rsplit("/", 1)[-1]
    if url_name:
        candidates.append(REPORT_DIR / url_name)
    candidates.append(REPORT_DIR / f"review_{safe_doi(row['doi'])}.pdf")
    for candidate in dict.fromkeys(candidates):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Review report missing for id={row['id']} doi={row['doi']}")


def run_text(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def extract_section(text: str, start: str, end_pattern: str) -> str:
    match = re.search(re.escape(start) + r"(.*?)" + end_pattern, text, re.S)
    return match.group(1) if match else ""


def audit_report(row: dict, report: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    pdfinfo = run_text(["pdfinfo", str(report)])
    if pdfinfo.returncode:
        errors.append("pdfinfo_failed")
    info = {}
    for line in pdfinfo.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            info[key.strip()] = value.strip()
    pages = int(info.get("Pages", "0") or 0)
    if pages < 1:
        errors.append("no_pages")
    if "A4" not in info.get("Page size", ""):
        errors.append("not_A4")
    if info.get("Encrypted", "").lower() != "no":
        errors.append("encrypted")

    extracted = run_text(["pdftotext", "-layout", str(report), "-"])
    if extracted.returncode:
        errors.append("text_extraction_failed")
    text = extracted.stdout.replace("\x0c", "\n")
    if len(text.strip()) < 800:
        errors.append("too_little_text")
    if row["doi"].casefold() not in text.casefold():
        errors.append("doi_missing")
    if "论文预警报告（AI复核版）" not in text:
        errors.append("report_title_missing")
    if "复核过程中出现错误" in text:
        errors.append("review_error_placeholder")

    overview = extract_section(
        text,
        "二、复核风险结论",
        r"(?:三、初检结果参考|三、复核详情)",
    )
    details_match = re.search(
        r"(?:三|四)、复核详情(.*?)(?:四|五)、综合评估",
        text,
        re.S,
    )
    details = details_match.group(1) if details_match else ""
    final_match = re.search(
        r"(?:四|五)、综合评估(.*?)(?:五|六)、免责声明",
        text,
        re.S,
    )
    final = final_match.group(1) if final_match else ""
    if not overview:
        errors.append("overview_section_missing")
    if not details:
        errors.append("details_section_missing")
    if not final:
        errors.append("final_section_missing")
    if not re.search(r"复核结论[：:]\s*建议高风险", overview):
        errors.append("overview_not_high")
    if not re.search(r"复核结论[：:]\s*建议高风险", final):
        errors.append("final_not_high")
    if re.search(r"复核结论[：:]\s*建议低风险", overview):
        errors.append("overview_low_conflict")
    if re.search(r"复核结论[：:]\s*建议低风险", final):
        errors.append("final_low_conflict")
    if re.search(
        r"(?:综合|最终|总体|整体)(?:判断|判定|结论|评估)?"
        r"\s*(?:为|是|：|:)\s*(?:建议)?低风险",
        final,
    ):
        errors.append("final_low_language")

    dimensions = {
        "image": (
            r"图像(?:重复检测|问题)复核",
            r"（一）图像(?:重复检测|问题)复核",
        ),
        "data": (r"数据异常检测复核", r"（二）数据异常检测复核"),
        "ref": (r"参考文献检测复核", r"（三）参考文献检测复核"),
        "methodology": (
            r"(?:方法学与统计|研究设计与统计)核查",
            r"（四）(?:方法学与统计|研究设计与统计)核查",
        ),
    }
    overview_verdicts: dict[str, str] = {}
    detail_verdicts: dict[str, str] = {}
    detail_heads = []
    for dimension, (overview_label, detail_head) in dimensions.items():
        match = re.search(
            overview_label + r"[：:]\s*(建议高风险|建议低风险)", overview
        )
        if not match:
            errors.append(f"overview_dimension_missing:{dimension}")
        else:
            overview_verdicts[dimension] = match.group(1)
        head_match = re.search(detail_head, details)
        if not head_match:
            errors.append(f"detail_dimension_missing:{dimension}")
        else:
            detail_heads.append((head_match.start(), dimension))

    detail_heads.sort()
    for position, (start, dimension) in enumerate(detail_heads):
        end = detail_heads[position + 1][0] if position + 1 < len(detail_heads) else len(details)
        part = details[start:end]
        match = re.search(
            r"本模块复核结论[：:]\s*(建议高风险|建议低风险)", part
        )
        if not match:
            errors.append(f"detail_verdict_missing:{dimension}")
        else:
            detail_verdicts[dimension] = match.group(1)

    for dimension, verdict in overview_verdicts.items():
        if detail_verdicts.get(dimension) != verdict:
            errors.append(f"dimension_conflict:{dimension}")
    if not any(value == "建议高风险" for value in overview_verdicts.values()):
        errors.append("no_high_risk_dimension")

    fonts = run_text(["pdffonts", str(report)])
    if fonts.returncode:
        errors.append("font_check_failed")
    else:
        for line in fonts.stdout.splitlines()[2:]:
            columns = line.split()
            if len(columns) >= 6 and columns[-5].lower() != "yes":
                errors.append("font_not_embedded")
                break

    if "**" in text:
        warnings.append("legacy_markdown_markers")
    return {
        "db_id": row["id"],
        "doi": row["doi"],
        "report": str(report),
        "pages": pages,
        "errors": errors,
        "warnings": warnings,
        "overview_dimensions": overview_verdicts,
        "detail_dimensions": detail_verdicts,
    }


def file_stats(path: Path) -> tuple[int, int]:
    count = 0
    size = 0
    for item in path.rglob("*"):
        if item.is_file():
            count += 1
            size += item.stat().st_size
    return count, size


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifests(records: list[dict], audits: list[dict]) -> None:
    audit_by_id = {item["db_id"]: item for item in audits}
    fields = (
        "sequence",
        "db_id",
        "doi",
        "title",
        "review_time",
        "source_batch",
        "source_directory",
        "source_file_count",
        "source_bytes",
        "report_pages",
        "report_sha256",
        "folder",
    )
    with (EXPORT_DIR / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({key: record[key] for key in fields})
    (EXPORT_DIR / "manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    (EXPORT_DIR / "report_audit.json").write_text(
        json.dumps(audits, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    warning_counts = Counter(
        warning for audit in audits for warning in audit.get("warnings", [])
    )
    readme = f"""4050复核高风险增量归档

数据口径：{NEW_TABLE} 的252条复核高风险记录，减去 {BASELINE_TABLE} 的188条复核高风险记录。
说明：用户提到的 yujing_4050_202606 当前只有101条复核高风险；188条对应的是 yujing_4050，因此本归档按该实际口径生成。
差集算法：按规范化DOI分组后使用ROW_NUMBER保留重复记录语义，得到{len(records)}条记录。

每篇目录包含：
1. 原文及源数据：原始输入目录的完整副本，保留原文件名和层级。
2. 复核报告/复核报告.pdf：数据库记录对应的复核PDF。
3. metadata.json：该条数据库记录与来源信息。

PDF门禁：{len(audits)}份全部可打开、A4、文字可提取、字体已嵌入；总评与综合结论均为建议高风险；概览和详情的四维结论逐项一致；每份至少有一个建议高风险维度。
格式提示：{warning_counts.get('legacy_markdown_markers', 0)}份历史报告正文保留少量原始“**”强调标记，不影响内容、结论或PDF打开。
修复记录：10.1016/j.cmet.2021.09.008 原报告总评为高风险但数据维度误标低风险，归档前已按最终Judge证据修正为数据维度高风险并重新生成PDF。
"""
    (EXPORT_DIR / "README.txt").write_text(readme, encoding="utf-8")


def create_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(
        ZIP_PATH, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        for path in sorted(EXPORT_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, Path(EXPORT_NAME) / path.relative_to(EXPORT_DIR))
    with zipfile.ZipFile(ZIP_PATH, "r", allowZip64=True) as archive:
        bad_file = archive.testzip()
        if bad_file:
            raise RuntimeError(f"ZIP CRC validation failed at {bad_file}")


def main() -> None:
    rows = fetch_delta_rows()
    source_index = build_source_index()
    resolved = []
    audits = []
    for row in rows:
        source = find_source(row, source_index)
        report = find_report(row)
        audit = audit_report(row, report)
        audits.append(audit)
        resolved.append((row, source, report, audit))

    failed = [audit for audit in audits if audit["errors"]]
    if failed:
        raise RuntimeError(
            "Report audit failed:\n" + json.dumps(failed, ensure_ascii=False, indent=2)
        )

    if EXPORT_DIR.exists():
        raise RuntimeError(f"Refusing to overwrite existing export: {EXPORT_DIR}")
    EXPORT_DIR.mkdir(parents=True)

    records = []
    for sequence, (row, source, report, audit) in enumerate(resolved, 1):
        folder_name = f"{sequence:02d}_id{row['id']}_{safe_doi(row['doi'])}"
        paper_dir = EXPORT_DIR / folder_name
        materials_dir = paper_dir / "原文及源数据"
        review_dir = paper_dir / "复核报告"
        shutil.copytree(source, materials_dir)
        review_dir.mkdir(parents=True)
        report_copy = review_dir / "复核报告.pdf"
        shutil.copy2(report, report_copy)
        source_file_count, source_bytes = file_stats(materials_dir)
        metadata = {
            "sequence": sequence,
            "db_id": row["id"],
            "doi": row["doi"],
            "title": row["title"],
            "review_time": row["review_time"],
            "review_result": row["review_result"],
            "source_table": NEW_TABLE,
            "baseline_table": BASELINE_TABLE,
            "source_directory": str(source),
            "source_batch": source.parent.name,
            "review_report_source": str(report),
            "review_report_url": row["review_report_url"],
            "report_audit": audit,
        }
        (paper_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=json_default) + "\n",
            encoding="utf-8",
        )
        records.append(
            {
                "sequence": sequence,
                "db_id": row["id"],
                "doi": row["doi"],
                "title": row["title"],
                "review_time": row["review_time"],
                "source_batch": source.parent.name,
                "source_directory": str(source),
                "source_file_count": source_file_count,
                "source_bytes": source_bytes,
                "report_pages": audit["pages"],
                "report_sha256": sha256(report_copy),
                "folder": folder_name,
            }
        )

    write_manifests(records, audits)
    create_zip()
    print(
        json.dumps(
            {
                "records": len(records),
                "unique_dois": len({record["doi"].casefold() for record in records}),
                "source_files": sum(record["source_file_count"] for record in records),
                "source_bytes": sum(record["source_bytes"] for record in records),
                "report_pages": sum(record["report_pages"] for record in records),
                "warnings": Counter(
                    warning for audit in audits for warning in audit["warnings"]
                ),
                "export_dir": str(EXPORT_DIR),
                "zip_path": str(ZIP_PATH),
                "zip_bytes": ZIP_PATH.stat().st_size,
                "zip_sha256": sha256(ZIP_PATH),
            },
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )
    )


if __name__ == "__main__":
    main()
