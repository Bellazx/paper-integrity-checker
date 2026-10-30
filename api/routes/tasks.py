from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from api.config import (
    DEFAULT_TABLE, MAX_BATCH_PAPERS, MAX_PENDING_TASKS, MAX_UPLOAD_SIZE_BYTES,
    TASKS_DIR, validate_table_name, resolve_upload_dir,
)
from api.models import (
    PaperInfo, SubmitRequest, TaskConfig, TaskCreatedResponse,
    TaskListResponse, TaskMode, TaskStatus, TaskStatusResponse, TaskSummary,
)
from api.services.zip_handler import apply_doi_override, extract_single, extract_batch
from api.services.task_manager import CapacityExceeded

log = logging.getLogger(__name__)

router = APIRouter()

_task_manager = None


async def _extract_archive(extractor, archive_path: Path, task_id: str):
    """Run archive work outside the async request loop.

    Large ZIP/RAR validation and copying can take minutes.  Keeping it in the
    event loop made cancelled uploads leave tasks permanently in ``extracting``.
    """
    return await asyncio.to_thread(extractor, archive_path, task_id)


def _mark_extraction_cancelled(tm, task_id: str) -> None:
    tm.update_task(
        task_id,
        status=TaskStatus.FAILED,
        stage="extracting",
        error="文件提取请求被取消；请重新提交",
    )


def get_task_manager():
    global _task_manager
    if _task_manager is None:
        from api.services.task_manager import TaskManager
        _task_manager = TaskManager()
    return _task_manager


def _decorate_detection_report(report: dict) -> dict:
    """Add a semantic completion state without changing the DB lifecycle code.

    Older task status files predate ``processing_status``.  Deriving the field
    at read time keeps those tasks compatible and prevents a frontend from
    interpreting status=0 (normal initial-only completion) as a failure.
    """
    if not isinstance(report, dict):
        return report
    if report.get("processing_status"):
        return report
    review_error = report.get("review_error")
    chushen = report.get("chushen_result")
    review = report.get("review_result")
    if review_error:
        state, text_value, completed = "review_failed", "复审失败", False
    elif review:
        state, text_value, completed = "completed_review", "检测完成（已复审）", True
    elif chushen == "低风险":
        state, text_value, completed = "completed_initial", "检测完成（初审低风险）", True
    elif chushen == "高风险":
        state, text_value, completed = "review_pending", "初审完成，等待复审", False
    else:
        state, text_value, completed = "failed", "检测失败", False
    result = dict(report)
    result.update({
        "processing_status": state,
        "processing_status_text": text_value,
        "processing_completed": completed,
    })
    return result


def set_task_manager(tm):
    global _task_manager
    _task_manager = tm


def ensure_task_capacity(tm):
    """Backward-compatible pre-check. NOTE: for race-free enforcement use
    create_task_guarded() which checks capacity and creates atomically. This
    standalone check remains for callers that want an early 503 before doing
    expensive prep work, but it is not the authoritative gate."""
    unfinished = tm.unfinished_count()
    if unfinished >= MAX_PENDING_TASKS:
        raise HTTPException(
            status_code=503,
            detail=(
                f"系统繁忙，当前未完成任务 {unfinished} 个，"
                f"已达到上限 {MAX_PENDING_TASKS}，请稍后重试"
            ),
            headers={"Retry-After": "60"},
        )


def create_task_guarded(tm, mode, config):
    """Atomically enforce MAX_PENDING_TASKS and create the task (race-free).
    Translates CapacityExceeded into the same HTTP 503 + Retry-After."""
    try:
        return tm.create_task_with_capacity(mode, config, MAX_PENDING_TASKS)
    except CapacityExceeded as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"系统繁忙，当前未完成任务 {e.unfinished} 个，"
                f"已达到上限 {e.limit}，请稍后重试"
            ),
            headers={"Retry-After": "60"},
        )


# ─── New: submit with fileId ───────────────────────────────────────

@router.post("/submit", response_model=TaskCreatedResponse)
async def submit_task(req: SubmitRequest):
    # Validate fileId exists — find the archive (zip or rar)
    upload_dir = resolve_upload_dir(req.file_id)
    archive_path = None
    for ext in (".zip", ".rar"):
        candidate = upload_dir / f"upload{ext}"
        if candidate.exists():
            archive_path = candidate
            break
    if archive_path is None:
        raise HTTPException(404, f"文件 '{req.file_id}' 不存在或已过期")

    # Validate table name
    try:
        validate_table_name(req.table_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Validate mode
    if req.mode not in ("single", "batch"):
        raise HTTPException(400, "mode 必须为 'single' 或 'batch'")

    tm = get_task_manager()
    config = TaskConfig(
        table_name=req.table_name,
        author_type=req.author_type,
        max_workers=req.max_workers,
    )
    task_mode = TaskMode.SINGLE if req.mode == "single" else TaskMode.BATCH
    task_id = create_task_guarded(tm, task_mode, config)

    try:
        # Copy archive to task dir for audit trail
        task_dir = TASKS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task_archive = task_dir / archive_path.name
        shutil.copy2(archive_path, task_archive)

        tm.update_task(task_id, status=TaskStatus.EXTRACTING, stage="extracting")

        if req.mode == "single":
            papers = await _extract_archive(extract_single, task_archive, task_id)
            # Override DOI if provided
            if req.doi and papers:
                apply_doi_override(papers[0], req.doi)
        else:
            papers = await _extract_archive(extract_batch, task_archive, task_id)
            if len(papers) > MAX_BATCH_PAPERS:
                raise ValueError(f"批量包含 {len(papers)} 篇论文，最多 {MAX_BATCH_PAPERS} 篇")

        tm.update_task(task_id, papers=papers)
    except asyncio.CancelledError:
        _mark_extraction_cancelled(tm, task_id)
        raise
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"文件解压失败: {e}")

    # Upload dir is kept so file_id can be reused for re-submission

    from api.worker import run_pipeline
    tm.start_pipeline_worker(task_id)

    return TaskCreatedResponse(
        task_id=task_id,
        status="queued",
        message=f"任务已创建，{len(papers)} 篇论文进入检测队列",
        poll_url=f"/api/task/{task_id}",
    )


# ─── Legacy: direct upload+process (kept for backward compatibility) ───

async def _save_upload(file: UploadFile, task_id: str) -> Path:
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    zip_path = task_dir / "upload.zip"

    size = 0
    with open(zip_path, "wb") as f:
        while chunk := await file.read(8192):
            size += len(chunk)
            if size > MAX_UPLOAD_SIZE_BYTES:
                zip_path.unlink(missing_ok=True)
                raise HTTPException(413, f"File exceeds maximum size ({MAX_UPLOAD_SIZE_BYTES // 1024 // 1024}MB)")
            f.write(chunk)

    return zip_path


@router.post("/single", response_model=TaskCreatedResponse)
async def create_single_task(
    file: UploadFile = File(...),
    table_name: str = Form(DEFAULT_TABLE),
    skip_refs: bool = Form(False),
    author_type: str = Form(""),
    skip_review: bool = Form(False),
):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Only ZIP files are accepted")

    try:
        validate_table_name(table_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    tm = get_task_manager()
    config = TaskConfig(
        table_name=table_name,
        skip_refs=skip_refs,
        author_type=author_type,
        skip_review=skip_review,
    )
    task_id = create_task_guarded(tm, TaskMode.SINGLE, config)

    try:
        zip_path = await _save_upload(file, task_id)
        tm.update_task(task_id, status=TaskStatus.EXTRACTING, stage="extracting")
        papers = await _extract_archive(extract_single, zip_path, task_id)
        tm.update_task(task_id, papers=papers)
    except asyncio.CancelledError:
        _mark_extraction_cancelled(tm, task_id)
        raise
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"ZIP extraction failed: {e}")

    from api.worker import run_pipeline
    tm.start_pipeline_worker(task_id)

    return TaskCreatedResponse(
        task_id=task_id,
        status="queued",
        message=f"Task created. {len(papers)} paper(s) queued for processing.",
        poll_url=f"/api/task/{task_id}",
    )


@router.post("/batch", response_model=TaskCreatedResponse)
async def create_batch_task(
    file: UploadFile = File(...),
    table_name: str = Form(DEFAULT_TABLE),
    skip_refs: bool = Form(False),
    author_type: str = Form(""),
    skip_review: bool = Form(False),
    max_workers: int = Form(4),
):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Only ZIP files are accepted")

    try:
        validate_table_name(table_name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    tm = get_task_manager()
    config = TaskConfig(
        table_name=table_name,
        skip_refs=skip_refs,
        author_type=author_type,
        skip_review=skip_review,
        max_workers=max_workers,
    )
    task_id = create_task_guarded(tm, TaskMode.BATCH, config)

    try:
        zip_path = await _save_upload(file, task_id)
        tm.update_task(task_id, status=TaskStatus.EXTRACTING, stage="extracting")
        papers = await _extract_archive(extract_batch, zip_path, task_id)
        if len(papers) > MAX_BATCH_PAPERS:
            raise ValueError(f"Batch contains {len(papers)} papers, maximum is {MAX_BATCH_PAPERS}")
        tm.update_task(task_id, papers=papers)
    except asyncio.CancelledError:
        _mark_extraction_cancelled(tm, task_id)
        raise
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"ZIP extraction failed: {e}")

    from api.worker import run_pipeline
    tm.start_pipeline_worker(task_id)

    return TaskCreatedResponse(
        task_id=task_id,
        status="queued",
        message=f"Batch task created. {len(papers)} paper(s) queued for processing.",
        poll_url=f"/api/task/{task_id}",
    )


# ─── Query endpoints ───────────────────────────────────────────────

@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found")

    papers_info = [
        {
            "doi_slug": p.doi_slug,
            "doi": p.doi,
            "status": p.status,
            "error": p.error if p.error else None,
            "submission_no": p.submission_no,
            "fold_name": p.fold_name,
            "superseded_by": p.superseded_by,
            "reused_from_task_id": p.reused_from_task_id,
            "submission_decision": p.submission_decision,
        }
        for p in task.papers
    ]

    # The numeric ``detection_reports.status`` field is deliberately not used
    # here: status=0 means that initial screening was persisted, and is the
    # normal terminal state for a low-risk paper (it does not enter AI review).
    # Attach the same semantic state exposed in result.detection_reports so a
    # frontend cannot accidentally render those papers as failed.
    result_payload = copy.deepcopy(task.result) if task.result else None
    report_by_key = {}
    if result_payload and isinstance(result_payload.get("detection_reports"), list):
        decorated_reports = [
            _decorate_detection_report(report)
            for report in result_payload["detection_reports"]
        ]
        result_payload["detection_reports"] = decorated_reports
        for report in decorated_reports:
            key = (report.get("submission_no"), report.get("fold_name"))
            report_by_key[key] = report
    for paper_info, paper in zip(papers_info, task.papers):
        key = (paper.submission_no, paper.fold_name or None)
        report = report_by_key.get(key)
        if report:
            paper_info.update({
                "processing_status": report.get("processing_status"),
                "processing_status_text": report.get("processing_status_text"),
                "processing_completed": report.get("processing_completed"),
                "chushen_result": report.get("chushen_result"),
                "review_result": report.get("review_result"),
                "review_error": report.get("review_error"),
            })

    return TaskStatusResponse(
        task_id=task.task_id,
        mode=task.mode.value,
        status=task.status.value,
        stage=task.stage,
        progress=task.progress,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        result=result_payload,
        error=task.error,
        papers=papers_info,
        superseded_by=task.superseded_by,
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(response: Response, limit: int = 20, offset: int = 0, status: str | None = None):
    response.headers['Cache-Control'] = 'no-store'
    tm = get_task_manager()
    summaries, total = tm.list_tasks(limit, offset, status)
    return TaskListResponse(tasks=summaries, total=total)


@router.delete("/{task_id}")
async def delete_task(task_id: str):
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(404, f"Task '{task_id}' not found")
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        raise HTTPException(400, "Can only delete completed or failed tasks")
    tm.delete_task(task_id)
    return {"message": f"Task '{task_id}' deleted"}
