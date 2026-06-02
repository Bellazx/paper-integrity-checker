from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import rarfile

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse

from api.config import (
    DEFAULT_TABLE, FORBIDDEN_EXTENSIONS, MAX_BATCH_PAPERS,
    MAX_REVIEW_WORKERS, MAX_RUN_CONCURRENT_STREAMS,
    MAX_UPLOAD_SIZE_BYTES, MAX_UPLOAD_SIZE_MB, RUNTIME_TMP_DIR,
    validate_table_name,
)
from api.services.detect import run_detection_single
from api.services.review import run_review_single
from api.services.report import generate_reports
from api.services.zip_handler import apply_doi_override, extract_single, extract_batch

log = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_ARCHIVE_EXTS = {".zip", ".rar"}
_run_gate_lock = asyncio.Lock()
_active_run_streams = 0


def _event(stage: str, message: str, **extra) -> str:
    obj = {"stage": stage, "message": message, **extra}
    return json.dumps(obj, ensure_ascii=False) + "\n"


async def _concurrent_stream(items, worker, limit):
    """Run `worker(item)` concurrently (capped at `limit`) and yield event strings
    as they are produced, bridging concurrent tasks back into this generator.

    `worker` is an async function `worker(item, emit)` where `emit(event_str)` queues
    an event for immediate streaming. Order of events across items is not guaranteed
    (by design — only intermediate human-readable status lines are affected).
    """
    queue: asyncio.Queue = asyncio.Queue()
    sem = asyncio.Semaphore(limit)
    _DONE = object()

    async def _run_one(item):
        async with sem:
            await worker(item, queue.put_nowait)

    async def _driver():
        await asyncio.gather(*(_run_one(it) for it in items))
        queue.put_nowait(_DONE)

    driver = asyncio.create_task(_driver())
    try:
        while True:
            ev = await queue.get()
            if ev is _DONE:
                break
            yield ev
    finally:
        await driver


@router.post("/run")
async def run_pipeline_endpoint(
    file: UploadFile = File(...),
    mode: str = Form(""),
    doi: str = Form(""),
    table_name: str = Form(DEFAULT_TABLE),
    author_type: str = Form(""),
    max_workers: int = Form(4),
):
    # Pre-validate before entering the stream
    errors = _pre_validate(file, table_name)
    if errors:
        return StreamingResponse(
            _error_stream(errors),
            media_type="application/x-ndjson",
            status_code=400,
        )

    if not await _try_acquire_run_slot():
        return StreamingResponse(
            _error_stream([
                f"/api/run 当前已有 {MAX_RUN_CONCURRENT_STREAMS} 个任务运行，请稍后重试"
            ]),
            media_type="application/x-ndjson",
            status_code=429,
            headers={"Retry-After": "60"},
        )

    return StreamingResponse(
        _run_stream_with_slot(file, mode, doi, table_name, author_type, max_workers),
        media_type="application/x-ndjson",
    )


async def _try_acquire_run_slot() -> bool:
    global _active_run_streams
    async with _run_gate_lock:
        if _active_run_streams >= MAX_RUN_CONCURRENT_STREAMS:
            return False
        _active_run_streams += 1
        return True


async def _release_run_slot():
    global _active_run_streams
    async with _run_gate_lock:
        _active_run_streams = max(0, _active_run_streams - 1)


async def _run_stream_with_slot(*args, **kwargs):
    try:
        async for event in _run_stream(*args, **kwargs):
            yield event
    finally:
        await _release_run_slot()


def _pre_validate(file: UploadFile, table_name: str) -> list[str]:
    errors = []
    if not file.filename:
        errors.append("未提供文件")
        return errors
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_ARCHIVE_EXTS:
        errors.append(f"仅支持 ZIP 和 RAR 格式压缩包，当前: {ext}")
    try:
        validate_table_name(table_name)
    except ValueError as e:
        errors.append(str(e))
    return errors


async def _error_stream(errors: list[str]):
    yield _event("error", "; ".join(errors), errors=errors)


async def _run_stream(
    file: UploadFile,
    mode: str,
    doi: str,
    table_name: str,
    author_type: str,
    max_workers: int,
):
    max_workers = min(max(1, max_workers), MAX_REVIEW_WORKERS)
    tmp_dir = Path(tempfile.mkdtemp(prefix="paper_run_", dir=str(RUNTIME_TMP_DIR)))
    ext = Path(file.filename).suffix.lower()
    archive_path = tmp_dir / f"upload{ext}"

    try:
        # --- Save file ---
        yield _event("validating", "保存并校验压缩包...")
        size = 0
        with open(archive_path, "wb") as f:
            while chunk := await file.read(8192):
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE_BYTES:
                    yield _event("error", f"文件超过 {MAX_UPLOAD_SIZE_MB}MB 限制")
                    return
                f.write(chunk)

        # --- Validate archive integrity ---
        if ext == ".zip" and not zipfile.is_zipfile(archive_path):
            yield _event("error", "ZIP 压缩包已损坏或格式无效")
            return
        if ext == ".rar" and not rarfile.is_rarfile(str(archive_path)):
            yield _event("error", "RAR 压缩包已损坏或格式无效")
            return

        # --- Validate contents ---
        validation = _validate_contents(archive_path, ext)
        if isinstance(validation, list):
            yield _event("error", f"校验失败: {'; '.join(validation)}", errors=validation)
            return

        detected_mode = validation["mode"]
        if mode and mode != detected_mode:
            yield _event("warning", f"指定模式 {mode} 与识别结果 {detected_mode} 不一致，使用指定模式")
        else:
            mode = detected_mode

        yield _event("extracting", f"校验通过，{validation['paper_count']} 篇论文 ({mode} 模式)，解压中...")

        # --- Extract ---
        run_id = tmp_dir.name.replace("paper_run_", "")
        if mode == "single":
            papers = extract_single(archive_path, run_id)
            if doi and papers:
                apply_doi_override(papers[0], doi)
        else:
            papers = extract_batch(archive_path, run_id)

        yield _event("extracting", f"解压完成，识别到 {len(papers)} 篇论文")

        # --- Detection (concurrent, capped to protect the box: each paper spawns main.py) ---
        high_risk_papers = []
        detect_errors = 0
        detect_done = 0
        detect_total = len(papers)
        detect_limit = min(max(1, max_workers), 2)

        async def _detect_worker(paper, emit):
            nonlocal detect_errors, detect_done
            emit(_event("detecting", f"检测中: {paper.doi_slug}"))
            try:
                findings = await run_detection_single(
                    input_dir=paper.input_dir,
                    output_dir=paper.output_dir,
                    author_type=author_type,
                    table_name=table_name,
                    doi=paper.doi,
                )

                report_json = Path(paper.output_dir) / "report.json"
                paper.report_json = str(report_json) if report_json.exists() else ""

                if not paper.doi and "paper" in findings:
                    paper.doi = findings["paper"].get("doi", "")

                risk_level = "低风险"
                if "summary" in findings:
                    from modules.chinese_report_generator import _compute_overall_risk
                    overall = _compute_overall_risk(findings)
                    risk_level = overall.get("level", "低风险")

                paper.status = "detected"
                if risk_level == "高风险":
                    high_risk_papers.append(paper)

                detect_done += 1
                emit(_event("detecting", f"[{detect_done}/{detect_total}] {paper.doi_slug} → {risk_level}"))

            except Exception as e:
                detect_errors += 1
                detect_done += 1
                paper.status = "error"
                paper.error = str(e)
                emit(_event("detecting", f"[{detect_done}/{detect_total}] {paper.doi_slug} 检测失败: {e}"))

        async for ev in _concurrent_stream(papers, _detect_worker, detect_limit):
            yield ev

        yield _event("detecting",
            f"检测完成: {len(papers)} 篇，高风险 {len(high_risk_papers)} 篇，失败 {detect_errors} 篇")

        # --- Review (concurrent, capped at max_workers) ---
        review_results = []
        if high_risk_papers:
            review_done = 0
            review_total = len(high_risk_papers)

            async def _review_worker(paper, emit):
                nonlocal review_done
                emit(_event("reviewing", f"AI复核中: {paper.doi or paper.doi_slug}"))
                try:
                    review = await run_review_single(
                        doi=paper.doi,
                        report_json_path=paper.report_json,
                        input_dir=paper.input_dir,
                        output_dir=paper.output_dir,
                    )
                    review.setdefault("trigger", "auto_detection")
                    review_results.append(review)
                    verdict = review.get("verdict", "")
                    result = review.get("result", "")
                    review_done += 1
                    emit(_event("reviewing", f"[{review_done}/{review_total}] {paper.doi or paper.doi_slug} → {verdict} ({result})"))
                except Exception as e:
                    log.error("Review failed for %s: %s", paper.doi, e)
                    review_results.append({
                        "doi": paper.doi,
                        "result": "高风险",
                        "trigger": "review_error",
                        "image_review": f"复核失败：{e}",
                        "data_review": f"复核失败：{e}",
                        "ref_review": "",
                        "verdict": "建议高风险",
                        "reason": str(e),
                    })
                    review_done += 1
                    emit(_event("reviewing", f"[{review_done}/{review_total}] {paper.doi or paper.doi_slug} 复核失败: {e}"))

            async for ev in _concurrent_stream(high_risk_papers, _review_worker, max(1, max_workers)):
                yield ev
        else:
            yield _event("reviewing", "无高风险论文，跳过复核")

        # --- Generate reports ---
        report_count = 0
        if review_results:
            yield _event("generating_report", "生成复核报告...")
            try:
                report_paths = await generate_reports(review_results, tmp_dir, table_name)
                report_count = len(report_paths)
                yield _event("generating_report", f"生成 {report_count} 份复核报告")
            except Exception as e:
                yield _event("generating_report", f"报告生成失败: {e}")

        # --- Final result ---
        confirmed_high = sum(1 for r in review_results if r.get("result") == "高风险")
        downgraded = sum(1 for r in review_results if r.get("result") == "低风险")

        result = {
            "total_papers": len(papers),
            "detected_ok": len(papers) - detect_errors,
            "detected_fail": detect_errors,
            "high_risk_detected": len(high_risk_papers),
            "reviewed": len(review_results),
            "confirmed_high": confirmed_high,
            "downgraded": downgraded,
            "reports_generated": report_count,
        }

        papers_summary = [
            {"doi_slug": p.doi_slug, "doi": p.doi, "status": p.status, "error": p.error or None}
            for p in papers
        ]

        yield _event("completed", "全部完成", result=result, papers=papers_summary)

    except Exception as e:
        log.exception("Run pipeline failed")
        yield _event("error", f"流水线异常: {e}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _validate_contents(archive_path: Path, ext: str) -> dict | list[str]:
    names = []
    if ext == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
    elif ext == ".rar":
        with rarfile.RarFile(str(archive_path)) as rf:
            names = rf.namelist()

    errors = []

    unsafe = [n for n in names if ".." in n or n.startswith("/")]
    for p in unsafe:
        errors.append(f"不安全路径: {p}")

    forbidden = [n for n in names if not n.endswith("/") and Path(n).suffix.lower() in FORBIDDEN_EXTENSIONS]
    if forbidden:
        errors.append(f"不允许的文件: {', '.join(forbidden)}")

    pdf_files = [n for n in names if n.lower().endswith(".pdf") and "__MACOSX" not in n]
    if not pdf_files:
        errors.append("未找到 PDF 文件")

    if errors:
        return errors

    # Detect mode
    top_entries = set()
    for name in names:
        if "__MACOSX" in name:
            continue
        parts = name.split("/")
        if parts[0]:
            top_entries.add(parts[0])

    effective_root = ""
    if len(top_entries) == 1:
        sole = list(top_entries)[0]
        if any(n.startswith(sole + "/") and "__MACOSX" not in n for n in names):
            effective_root = sole + "/"

    if effective_root:
        sub_entries = set()
        for name in names:
            if not name.startswith(effective_root) or "__MACOSX" in name:
                continue
            rel = name[len(effective_root):]
            if rel:
                sub_entries.add(rel.split("/")[0])

        sub_dirs_with_pdf = []
        root_pdfs = []
        for entry in sub_entries:
            prefix = effective_root + entry + "/"
            if any(n.startswith(prefix) and n.lower().endswith(".pdf") for n in names):
                sub_dirs_with_pdf.append(entry)
            if (effective_root + entry).lower().endswith(".pdf"):
                root_pdfs.append(entry)

        if sub_dirs_with_pdf and not root_pdfs:
            mode, count = "batch", len(sub_dirs_with_pdf)
        else:
            mode, count = "single", 1
    else:
        root_pdfs = [n for n in pdf_files if "/" not in n]
        sub_dirs_with_pdf = [
            e for e in top_entries
            if any(n.startswith(e + "/") and n.lower().endswith(".pdf") and "__MACOSX" not in n for n in names)
        ]
        if root_pdfs or not sub_dirs_with_pdf:
            mode, count = "single", 1
        else:
            mode, count = "batch", len(sub_dirs_with_pdf)

    if mode == "batch" and count > MAX_BATCH_PAPERS:
        return [f"批量上传最多 {MAX_BATCH_PAPERS} 篇，当前 {count} 篇"]

    return {"mode": mode, "paper_count": count}
