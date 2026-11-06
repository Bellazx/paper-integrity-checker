#!/usr/bin/env python3
"""Screen and rereview a scholar-tagged paper batch."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Same limits as paper-api.service. Without them every paper thread lets
# OpenCV/OpenBLAS start one thread per CPU (192 here), and four papers in
# parallel spent most of their time contending for locks. Set before numpy
# and OpenCV are imported.
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "OPENCV_FOR_THREADS_NUM"):
    os.environ.setdefault(_name, "1")

import openpyxl
import pymssql

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
REPORT_PYTHON = os.environ.get(
    "PAPER_CHECKER_PYTHON",
    str(BASE / "venv" / "bin" / "python") if (BASE / "venv" / "bin" / "python").is_file() else sys.executable,
)

from api.services.review import run_review_single
from core.pipeline import initial_pdf_skipped, analyze_paper, find_pdf
from modules.chinese_report_generator import (
    _apply_explainability_dimension_downgrade,
    _apply_data_caps,
    _compute_dimension_risk,
    _compute_overall_risk,
    _make_filename,
    _reconcile_risk_levels,
)
from modules.doi_utils import dir_name_to_doi, normalize_doi
from utils.db import DB_CONFIG, build_paper_metadata

log = logging.getLogger("scholar-batch")
TABLE = "yujing_scholar"
KNOWN_SCHOLAR_NAMES = {
    "limeiqi": "李美琪",
    "rediliyusha": "热地力玉山",
    "rediliyushan": "热地力玉山",
    "shizhiwen": "史志文",
    "taowei": "陶卫",
    "wanbin": "张万斌",
    "yangqi": "杨启",
    "zhangao": "张翱",
    "zhanglizhi": "张礼知",
}
REPORT_BASE_URL = "http://202.120.57.100/chinese_reports"
TABLE_NAME_RE = re.compile(r"^yujing_[a-z0-9_]{1,60}$")


def _load_doi_map(path: Path, paper_dirs: list[Path]) -> dict[str, str]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise ValueError(f"Empty DOI workbook: {path}")
    header = [str(value or "").strip().lower() for value in rows[0]]
    doi_col = next((i for i, value in enumerate(header) if "doi" in value), None)
    if doi_col is None:
        raise ValueError(f"No DOI column in {path}")
    # Scholar workbooks call this column either ``文件夹`` or ``文件名``.
    # Treating a ``文件名`` workbook as an order-only workbook silently pairs
    # sorted directories with unrelated DOI rows when the sheet has a
    # different sort order.  Prefer the explicit directory identifier.
    folder_col = next(
        (
            i
            for i, value in enumerate(header)
            if "文件夹" in value or "文件名" in value or "folder" in value or "filename" in value
        ),
        None,
    )
    result = {}
    if folder_col is not None:
        directory_names = {paper_dir.name for paper_dir in paper_dirs}
        for row in rows[1:]:
            folder = str(row[folder_col] or "").strip()
            if folder not in directory_names:
                continue
            doi = normalize_doi(str(row[doi_col] or "").strip())
            if doi:
                result[folder] = doi
    else:
        for index, paper_dir in enumerate(paper_dirs):
            if index + 1 >= len(rows):
                break
            raw = rows[index + 1][doi_col]
            doi = normalize_doi(str(raw or "").strip())
            if doi:
                result[paper_dir.name] = doi
    if len(result) != len(paper_dirs):
        missing = [d.name for d in paper_dirs if d.name not in result]
        raise ValueError(f"DOI workbook does not cover all papers: {missing}")
    return result


def _doi_map_from_directories(paper_dirs: list[Path]) -> dict[str, str]:
    """Decode DOI directory names such as ``10.1109_TEC.2022.3207285``."""
    result = {}
    for paper_dir in paper_dirs:
        doi = normalize_doi(dir_name_to_doi(paper_dir))
        if not doi:
            raise ValueError(f"Cannot derive DOI from directory name: {paper_dir.name}")
        result[paper_dir.name] = doi
    return result


def _load_doi_map_json(path: Path, paper_dirs: list[Path]) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"DOI map must be a JSON object: {path}")
    result = {}
    for key, value in raw.items():
        identifier = str(value or "").strip()
        if not identifier:
            continue
        result[str(key)] = normalize_doi(identifier) or identifier
    missing = [paper_dir.name for paper_dir in paper_dirs if not result.get(paper_dir.name)]
    if missing:
        raise ValueError(f"DOI JSON map does not cover all papers: {missing}")
    return {paper_dir.name: result[paper_dir.name] for paper_dir in paper_dirs}


def _paper_dirs(input_root: Path) -> list[Path]:
    return sorted(
        (d for d in input_root.iterdir() if d.is_dir() and find_pdf(str(d))),
        key=lambda d: (int(d.name) if d.name.isdigit() else 10**9, d.name),
    )


def _scholar_from_workbook_name(input_root: Path) -> str:
    """Use the scholar name embedded in a top-level information workbook name."""
    noise = ("副本", "论文", "清单", "信息", "模板")
    candidates: list[str] = []
    for path in sorted(input_root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".xlsx", ".xls"}:
            continue
        for value in re.findall(r"[\u3400-\u9fff·]{2,20}", path.stem):
            candidate = value
            for word in noise:
                candidate = candidate.replace(word, "")
            if 2 <= len(candidate) <= 8:
                candidates.append(candidate)
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ""


def _db_row(findings: dict, scholar: str, college: str) -> dict:
    paper = findings.get("paper", {})
    summary = findings.get("summary", {})
    image_risk = _compute_dimension_risk(findings.get("image_duplicates", []))
    data_risk = _compute_dimension_risk(_apply_data_caps(findings.get("data_anomalies", [])))
    ref_risk = _compute_dimension_risk(findings.get("reference_issues", []))
    overall = _compute_overall_risk(findings)
    _apply_explainability_dimension_downgrade(findings, image_risk, data_risk, ref_risk)
    _reconcile_risk_levels(findings, overall, image_risk, data_risk, ref_risk)
    meta = build_paper_metadata(findings)
    doi = normalize_doi(paper.get("doi") or "") or str(paper.get("doi") or "")
    filename = _make_filename(doi, paper.get("title", "unknown"))
    return {
        "title": meta["title"][:500],
        "author": meta["author"][:500],
        "author_type": meta["author_type"],
        "department": meta["department"][:500],
        "author_all": meta["author_all"],
        "department_all": meta["department_all"],
        "journal": meta["journal"][:200],
        "doi": doi[:200],
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
        "report_url": f"{REPORT_BASE_URL}/{TABLE}/{filename}"[:500],
        "generation_time": datetime.now(),
        "scholar": scholar.strip()[:100],
        "college": college.strip()[:200],
    }


def _upsert_initial(row: dict) -> None:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id FROM {TABLE} WHERE doi=%(doi)s AND scholar=%(scholar)s",
            {"doi": row["doi"], "scholar": row["scholar"]},
        )
        exists = cursor.fetchone()
        if exists:
            cursor.execute(
                f"""UPDATE {TABLE} SET
                    title=%(title)s, author=%(author)s, author_type=%(author_type)s,
                    college=%(college)s,
                    department=%(department)s, author_all=%(author_all)s, department_all=%(department_all)s,
                    journal=%(journal)s, page=%(page)s, pic_num=%(pic_num)s, data_num=%(data_num)s,
                    ref_num=%(ref_num)s, total_score=%(total_score)s, risk_level=%(risk_level)s,
                    pic_score=%(pic_score)s, pic_risk_level=%(pic_risk_level)s,
                    data_score=%(data_score)s, data_risk_level=%(data_risk_level)s,
                    ref_score=%(ref_score)s, ref_risk_level=%(ref_risk_level)s,
                    report_url=%(report_url)s, generation_time=%(generation_time)s
                WHERE doi=%(doi)s AND scholar=%(scholar)s""",
                row,
            )
        else:
            cursor.execute(
                f"""INSERT INTO {TABLE} (
                    title, author, author_type, college, department, author_all, department_all, journal, doi,
                    page, pic_num, data_num, ref_num, total_score, risk_level,
                    pic_score, pic_risk_level, data_score, data_risk_level,
                    ref_score, ref_risk_level, report_url, generation_time, scholar
                ) VALUES (
                    %(title)s, %(author)s, %(author_type)s, %(college)s, %(department)s, %(author_all)s, %(department_all)s,
                    %(journal)s, %(doi)s, %(page)s, %(pic_num)s, %(data_num)s, %(ref_num)s,
                    %(total_score)s, %(risk_level)s, %(pic_score)s, %(pic_risk_level)s,
                    %(data_score)s, %(data_risk_level)s, %(ref_score)s, %(ref_risk_level)s,
                    %(report_url)s, %(generation_time)s, %(scholar)s
                )""",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def _existing_dois_for_scholar(scholar: str) -> set[str]:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT doi FROM {TABLE} WHERE scholar=%s", (scholar,))
        return {
            normalize_doi(str(row[0] or "")) or str(row[0] or "").strip()
            for row in cursor.fetchall()
            if str(row[0] or "").strip()
        }
    finally:
        conn.close()


def run_initial(
    input_root: Path,
    output_root: Path,
    scholar: str,
    college: str,
    doi_map: dict[str, str],
    workers: int,
    paper_dirs: list[Path] | None = None,
    summary_tag: str = "",
) -> list[dict]:
    output_root.mkdir(parents=True, exist_ok=True)
    chinese_reports = output_root / "chinese_reports"
    dirs = list(paper_dirs) if paper_dirs is not None else _paper_dirs(input_root)

    def process(paper_dir: Path) -> dict:
        started = time.monotonic()
        out_dir = output_root / "init" / paper_dir.name
        try:
            findings = analyze_paper(
                str(paper_dir),
                str(out_dir),
                skip_refs=False,
                chinese_reports_dir=str(chinese_reports),
                doi_override=doi_map[paper_dir.name],
            )
            skipped = initial_pdf_skipped(findings)
            if not findings.get("pdf_generated") and not skipped:
                raise RuntimeError("Initial PDF generation failed; result was not inserted into DB")
            row = _db_row(findings, scholar, college)
            if skipped:
                row["report_url"] = ""  # the review report is the one delivered
            else:
                filename = _make_filename(row["doi"], findings["paper"].get("title", "unknown"))
                pdf = chinese_reports / filename
                if not pdf.is_file() or pdf.stat().st_size == 0:
                    raise RuntimeError("Initial PDF is missing or empty; result was not inserted into DB")
            _upsert_initial(row)
            return {
                "dir": paper_dir.name,
                "doi": row["doi"],
                "title": row["title"],
                "risk_level": row["risk_level"],
                "score": row["total_score"],
                "status": "success",
                "report_json": str(out_dir / "report.json"),
                "input_dir": str(paper_dir),
                "elapsed": round(time.monotonic() - started, 1),
            }
        except Exception as exc:
            log.exception("Initial screening failed for %s", paper_dir.name)
            return {"dir": paper_dir.name, "status": "failed", "error": str(exc), "elapsed": round(time.monotonic() - started, 1)}

    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(process, d): d for d in dirs}
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            log.info("Initial [%d/%d] %s %s score=%s", index, len(dirs), result["status"], result["dir"], result.get("score", "-"))
    results.sort(key=lambda item: item["dir"])
    result_tag = re.sub(r'[\\/:*?"<>|]+', "_", scholar).strip("_") or "scholar"
    if summary_tag:
        result_tag += "_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", summary_tag).strip("._-")
    (output_root / f"screening_results_{result_tag}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return results


def _review_results_path(output_root: Path, scholar: str = "", summary_tag: str = "") -> Path:
    """Return a batch-local result path; never reuse another scholar's file."""
    token = re.sub(r'[^A-Za-z0-9_.-]+', '_', scholar or 'batch').strip('._-') or 'batch'
    if summary_tag:
        token += "_" + (re.sub(r'[^A-Za-z0-9_.-]+', '_', summary_tag).strip('._-') or 'run')
    return output_root / f"review_results_{token}.json"


async def run_reviews(
    results: list[dict],
    output_root: Path,
    workers: int,
    *,
    scholar: str = "",
    summary_tag: str = "",
    result_path: Path | None = None,
    resume: bool = False,
) -> list[dict]:
    semaphore = asyncio.Semaphore(max(1, workers))
    result_path = result_path or _review_results_path(output_root, scholar, summary_tag)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    ordered: list[dict | None] = [None] * len(results)
    write_lock = asyncio.Lock()

    async def save_progress() -> None:
        # A temporary file makes progress checkpoints safe even if the parent
        # process is interrupted while another worker finishes a paper.
        payload = [item for item in ordered if item is not None]
        temp = result_path.with_suffix(result_path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(result_path)

    async def review_one(index: int, item: dict) -> dict:
        if item.get("status") != "success":
            reviewed = item
            async with write_lock:
                ordered[index] = reviewed
                await save_progress()
            return reviewed
        async with semaphore:
            try:
                reviewed = await run_review_single(
                    doi=item["doi"],
                    report_json_path=item["report_json"],
                    input_dir=item["input_dir"],
                    output_dir=str(output_root / "review_v2"),
                    resume=resume,
                )
                reviewed.update({"_report_json": item["report_json"], "_input_dir": item["input_dir"]})
                log.info("Review %s -> %s", item["doi"], reviewed.get("result"))
            except Exception as exc:
                log.exception("Review failed for %s", item["doi"])
                reviewed = {
                    "doi": item["doi"],
                    "trigger": "review_error",
                    "result": "高风险",
                    "verdict": "确认高风险",
                    "reason": f"复核过程中出现错误：{exc}",
                    "image_review": "复核失败。",
                    "data_review": "复核失败。",
                    "ref_review": "复核失败。",
                    "_report_json": item["report_json"],
                    "_input_dir": item["input_dir"],
                }
        async with write_lock:
            ordered[index] = reviewed
            await save_progress()
        return reviewed

    await asyncio.gather(*(review_one(index, item) for index, item in enumerate(results)))
    return [item for item in ordered if item is not None]


def render_and_update_reviews(
    review_results: list[dict],
    output_root: Path,
    scholar: str,
    *,
    summary_tag: str = "",
    result_path: Path | None = None,
) -> int:
    results_path = result_path or _review_results_path(output_root, scholar, summary_tag)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    # Do not silently read a hard-coded file: callers may have supplied an
    # independent batch and its results must be the exact payload rendered.
    results_path.write_text(json.dumps(review_results, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["PAPER_INTEGRITY_ROOT"] = str(BASE)
    # Semantic coverage gaps are never warnings in a normal batch run. Keep
    # the child process strict even if an old shell/service exports a legacy
    # permissive value.
    env["REVIEW_COVERAGE_GAP_POLICY"] = "strict"
    env["REVIEW_COVERAGE_FAILURE_POLICY"] = "strict"
    script = BASE / "scripts" / "generate_review_report.py"
    command = [
        REPORT_PYTHON, str(script),
        "--results", str(results_path),
        "--output", str(output_root / "review_v2"),
        "--table", TABLE,
        "--namespace", TABLE,
        "--scholar", scholar,
    ]
    completed = subprocess.run(command, cwd=str(BASE), env=env, check=False)
    # The renderer returns 2 when some papers are review_error placeholders.
    # Valid papers have already been rendered and committed independently; a
    # per-paper failure must remain visible in logs without rolling back those
    # successful updates. Any other exit code indicates a batch-level failure.
    if completed.returncode not in (0, 2):
        raise subprocess.CalledProcessError(completed.returncode, command)
    return completed.returncode


def main() -> int:
    global TABLE
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True)
    parser.add_argument(
        "--scholar",
        help="Scholar name/identifier; inferred from a top-level information workbook, then the input directory name",
    )
    parser.add_argument(
        "--college",
        default="",
        help="Explicit scholar college/school; left blank when omitted (no automatic mapping)",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--review-workers", type=int, default=3)
    parser.add_argument(
        "--table",
        default=TABLE,
        help="Target database table and report namespace (default: yujing_scholar)",
    )
    parser.add_argument("--doi-workbook", help="Optional workbook containing a DOI column")
    parser.add_argument("--doi-map-json", help="Explicit JSON object mapping directory names to DOI or stable paper IDs")
    parser.add_argument("--skip-review", action="store_true")
    parser.add_argument(
        "--review-high-risk-only",
        action="store_true",
        help="Send only papers whose initial screening risk_level is 高风险 to review",
    )
    parser.add_argument("--summary-tag", default="", help="Unique result suffix for independent shards")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip DOI records already present for this scholar and process only missing papers",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    table = str(args.table or "").strip()
    if not TABLE_NAME_RE.fullmatch(table):
        raise SystemExit(
            f"Invalid --table {table!r}; expected yujing_<lowercase letters/digits/underscores>"
        )
    TABLE = table
    input_root = (BASE / args.input_root).resolve()
    output_root = BASE / "data" / "output" / TABLE
    scholar = (args.scholar or _scholar_from_workbook_name(input_root) or input_root.name).strip()
    scholar = KNOWN_SCHOLAR_NAMES.get(scholar, scholar)
    if not scholar:
        raise SystemExit("Scholar name cannot be empty")
    college = args.college.strip()
    if not college:
        log.warning("No --college supplied; yujing_scholar.college will be left blank for %s", scholar)
    dirs = _paper_dirs(input_root)
    if not dirs:
        raise SystemExit(f"No paper directories found in {input_root}")
    if args.doi_workbook and args.doi_map_json:
        raise SystemExit("Use only one of --doi-workbook and --doi-map-json")
    if args.doi_map_json:
        doi_map = _load_doi_map_json(Path(args.doi_map_json).expanduser().resolve(), dirs)
    elif args.doi_workbook:
        workbook = Path(args.doi_workbook).expanduser().resolve()
        doi_map = _load_doi_map(workbook, dirs)
    else:
        doi_map = _doi_map_from_directories(dirs)
    pending_dirs = dirs
    if args.resume:
        existing_dois = _existing_dois_for_scholar(scholar)
        pending_dirs = [
            directory
            for directory in dirs
            if doi_map[directory.name] not in existing_dois
        ]
        log.info(
            "Resume mode for %s: %d already stored, %d pending",
            scholar,
            len(dirs) - len(pending_dirs),
            len(pending_dirs),
        )
        if not pending_dirs:
            return 0
    initial = run_initial(
        input_root,
        output_root,
        scholar,
        college,
        doi_map,
        args.workers,
        paper_dirs=pending_dirs,
        summary_tag=args.summary_tag,
    )
    failed = [item for item in initial if item.get("status") != "success"]
    if failed:
        raise SystemExit(f"Initial screening failed for {len(failed)} paper(s): {failed}")
    if not args.skip_review:
        review_input = initial
        if args.review_high_risk_only:
            review_input = [item for item in initial if item.get("risk_level") == "高风险"]
            log.info(
                "Review scope: %d initial high-risk papers (of %d successful initial results)",
                len(review_input),
                len(initial),
            )
        reviewed = asyncio.run(run_reviews(
            review_input,
            output_root,
            args.review_workers,
            scholar=scholar,
            summary_tag=args.summary_tag,
        ))
        render_and_update_reviews(
            reviewed,
            output_root,
            scholar,
            summary_tag=args.summary_tag,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
