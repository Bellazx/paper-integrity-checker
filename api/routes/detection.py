"""User-facing submit endpoint: POST /api/detection/submit

Same two-step flow as the developer endpoint (upload → submit → poll), but the target
table is fixed to detection_reports (no table_name parameter). Progress and results are
read back through the existing GET /api/task/{task_id} endpoint; the per-paper
detection_reports records are attached to that task's `result.detection_reports`.

submission_no == file_id for every paper. In batch mode the papers under one file_id are
disambiguated by fold_name (the source folder name); single-paper submissions leave
fold_name empty.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

from fastapi import APIRouter, HTTPException

from api.config import MAX_BATCH_PAPERS, TASKS_DIR, resolve_upload_dir
from api.models import (
    DetectionSubmitRequest, TaskConfig, TaskCreatedResponse, TaskMode, TaskStatus,
)
from api.routes.tasks import create_task_guarded, get_task_manager
from api.services.zip_handler import apply_doi_override, extract_single, extract_batch

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/submit", response_model=TaskCreatedResponse)
async def submit_detection(req: DetectionSubmitRequest):
    # Locate the uploaded archive for this file_id
    upload_dir = resolve_upload_dir(req.file_id)
    archive_path = None
    for ext in (".zip", ".rar"):
        candidate = upload_dir / f"upload{ext}"
        if candidate.exists():
            archive_path = candidate
            break
    if archive_path is None:
        raise HTTPException(404, f"文件 '{req.file_id}' 不存在或已过期")

    if req.mode not in ("single", "batch"):
        raise HTTPException(400, "mode 必须为 'single' 或 'batch'")

    tm = get_task_manager()
    # table_name is fixed to detection_reports for this flow; TaskConfig.table_name is
    # only carried for audit/persistence and is NOT used to pick a yujing table here.
    config = TaskConfig(
        table_name="detection_reports",
        author_type=req.author_type,
        max_workers=req.max_workers,
    )
    task_mode = TaskMode.SINGLE if req.mode == "single" else TaskMode.BATCH
    task_id = create_task_guarded(tm, task_mode, config)

    try:
        task_dir = TASKS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        task_archive = task_dir / archive_path.name
        shutil.copy2(archive_path, task_archive)

        tm.update_task(task_id, status=TaskStatus.EXTRACTING, stage="extracting")

        if req.mode == "single":
            papers = extract_single(task_archive, task_id)
            if req.doi and papers:
                apply_doi_override(papers[0], req.doi)
        else:
            papers = extract_batch(task_archive, task_id)
            if len(papers) > MAX_BATCH_PAPERS:
                raise ValueError(f"批量包含 {len(papers)} 篇论文，最多 {MAX_BATCH_PAPERS} 篇")

        # submission_no == file_id for every paper (single + batch).
        for paper in papers:
            paper.submission_no = req.file_id

        tm.update_task(task_id, papers=papers)
    except HTTPException:
        raise
    except Exception as e:
        tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))
        raise HTTPException(400, f"文件解压失败: {e}")

    from api.services.detection_reports_worker import run_detection_reports_pipeline
    asyncio.create_task(run_detection_reports_pipeline(task_id, tm, papers, config.model_dump()))

    return TaskCreatedResponse(
        task_id=task_id,
        status="queued",
        message=f"任务已创建，{len(papers)} 篇论文进入检测队列",
        poll_url=f"/api/task/{task_id}",
    )
