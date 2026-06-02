from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    QUEUED = "queued"
    EXTRACTING = "extracting"
    DETECTING = "detecting"
    REVIEWING = "reviewing"
    GENERATING_REPORT = "generating_report"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskMode(str, Enum):
    SINGLE = "single"
    BATCH = "batch"


class TaskConfig(BaseModel):
    table_name: str = "yujing_quanliang"
    skip_refs: bool = False
    author_type: str = ""
    skip_review: bool = False
    max_workers: int = 4


class PaperInfo(BaseModel):
    doi_slug: str
    doi: str = ""
    input_dir: str = ""
    output_dir: str = ""
    report_json: str = ""
    status: str = "pending"
    error: str = ""
    fold_name: str = ""        # batch: source paper folder name; single: empty
    submission_no: str = ""    # detection_reports flow: = file_id


class TaskRecord(BaseModel):
    task_id: str
    mode: TaskMode
    status: TaskStatus = TaskStatus.QUEUED
    stage: str = ""
    config: TaskConfig = TaskConfig()
    papers: list[PaperInfo] = []
    progress: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None


class TaskCreatedResponse(BaseModel):
    task_id: str
    status: str
    message: str
    poll_url: str


class TaskStatusResponse(BaseModel):
    task_id: str
    mode: str
    status: str
    stage: str
    progress: dict[str, Any]
    created_at: str
    updated_at: str
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    papers: list[dict[str, Any]] = []


class TaskSummary(BaseModel):
    task_id: str
    mode: str
    status: str
    paper_count: int
    created_at: str


class TaskListResponse(BaseModel):
    tasks: list[TaskSummary]
    total: int


class FileInfo(BaseModel):
    filename: str
    size_mb: float
    paper_count: int
    mode: str
    papers: list[dict[str, Any]]


class UploadResponse(BaseModel):
    file_id: str
    message: str
    file_info: FileInfo


class UploadErrorResponse(BaseModel):
    success: bool = False
    message: str
    errors: list[str]


class SubmitRequest(BaseModel):
    file_id: str
    mode: str = "single"
    doi: str = ""
    table_name: str = "yujing_quanliang"
    author_type: str = ""
    max_workers: int = 4


class DetectionSubmitRequest(BaseModel):
    """User-facing submit (writes to detection_reports). No table_name — the target
    table is fixed to detection_reports."""
    file_id: str
    mode: str = "single"
    doi: str = ""
    author_type: str = ""
    max_workers: int = 4
