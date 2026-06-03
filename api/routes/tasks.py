from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

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


def get_task_manager():
    global _task_manager
    if _task_manager is None:
        from api.services.task_manager import TaskManager
        _task_manager = TaskManager()
    return _task_manager


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
            papers = extract_single(task_archive, task_id)
            # Override DOI if provided
            if req.doi and papers:
                apply_doi_override(papers[0], req.doi)
        else:
            papers = extract_batch(task_archive, task_id)
            if len(papers) > MAX_BATCH_PAPERS:
                raise ValueError(f"批量包含 {len(papers)} 篇论文，最多 {MAX_BATCH_PAPERS} 篇")

        tm.update_task(task_id, papers=papers)
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"文件解压失败: {e}")

    # Upload dir is kept so file_id can be reused for re-submission

    from api.worker import run_pipeline
    asyncio.create_task(run_pipeline(task_id, tm, papers, config.model_dump()))

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
        papers = extract_single(zip_path, task_id)
        tm.update_task(task_id, papers=papers)
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"ZIP extraction failed: {e}")

    from api.worker import run_pipeline
    asyncio.create_task(run_pipeline(task_id, tm, papers, config.model_dump()))

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
        papers = extract_batch(zip_path, task_id)
        if len(papers) > MAX_BATCH_PAPERS:
            raise ValueError(f"Batch contains {len(papers)} papers, maximum is {MAX_BATCH_PAPERS}")
        tm.update_task(task_id, papers=papers)
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"ZIP extraction failed: {e}")

    from api.worker import run_pipeline
    asyncio.create_task(run_pipeline(task_id, tm, papers, config.model_dump()))

    return TaskCreatedResponse(
        task_id=task_id,
        status="queued",
        message=f"Batch task created. {len(papers)} paper(s) queued for processing.",
        poll_url=f"/api/task/{task_id}",
    )


# ─── Query endpoints ───────────────────────────────────────────────

@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
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
        }
        for p in task.papers
    ]

    return TaskStatusResponse(
        task_id=task.task_id,
        mode=task.mode.value,
        status=task.status.value,
        stage=task.stage,
        progress=task.progress,
        created_at=task.created_at.isoformat(),
        updated_at=task.updated_at.isoformat(),
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
        result=task.result,
        error=task.error,
        papers=papers_info,
    )


@router.get("", response_model=TaskListResponse)
async def list_tasks(limit: int = 20, offset: int = 0, status: str | None = None):
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
