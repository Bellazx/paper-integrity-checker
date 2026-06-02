from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from api.config import TASKS_DIR
from api.models import TaskConfig, TaskMode, TaskRecord, TaskStatus, TaskSummary

log = logging.getLogger(__name__)

TASKS_DIR.mkdir(parents=True, exist_ok=True)

_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED}


class CapacityExceeded(Exception):
    """Raised when a new task would exceed MAX_PENDING_TASKS unfinished tasks."""

    def __init__(self, unfinished: int, limit: int):
        self.unfinished = unfinished
        self.limit = limit
        super().__init__(f"unfinished={unfinished} >= limit={limit}")


class TaskManager:
    def __init__(self):
        self._tasks: dict[str, TaskRecord] = {}
        # Guards the check-then-create window so concurrent submits cannot both
        # slip past MAX_PENDING_TASKS. Cheap, non-async lock — held only for the
        # few in-memory ops of create, never across an await.
        self._lock = threading.Lock()

    def create_task(self, mode: TaskMode, config: TaskConfig) -> str:
        with self._lock:
            return self._create_locked(mode, config)

    def create_task_with_capacity(self, mode: TaskMode, config: TaskConfig, max_pending: int) -> str:
        """Atomically enforce the pending-task cap and create the task.

        Closes the race between unfinished_count() and create_task() — the
        capacity check and insert happen under one lock, so two concurrent
        submits can never both pass a near-full gate. Raises CapacityExceeded.
        """
        with self._lock:
            unfinished = self._unfinished_count_locked()
            if unfinished >= max_pending:
                raise CapacityExceeded(unfinished, max_pending)
            return self._create_locked(mode, config)

    def _create_locked(self, mode: TaskMode, config: TaskConfig) -> str:
        short_id = uuid.uuid4().hex[:8]
        task_id = f"{datetime.now():%Y%m%d}-{short_id}"
        task = TaskRecord(task_id=task_id, mode=mode, config=config)
        self._tasks[task_id] = task
        self._persist(task)
        return task_id

    def _unfinished_count_locked(self) -> int:
        return sum(1 for task in self._tasks.values() if task.status not in _TERMINAL)

    def unfinished_count(self) -> int:
        with self._lock:
            return self._unfinished_count_locked()

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        stage: str | None = None,
        progress: dict | None = None,
        result: dict | None = None,
        error: str | None = None,
        papers: list | None = None,
    ):
        task = self._tasks.get(task_id)
        if not task:
            return
        if status is not None:
            task.status = status
        if stage is not None:
            task.stage = stage
        if progress is not None:
            task.progress = progress
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if papers is not None:
            task.papers = papers
        task.updated_at = datetime.now()
        if status in _TERMINAL:
            task.completed_at = datetime.now()
        self._persist(task)

    def get_task(self, task_id: str) -> TaskRecord | None:
        task = self._tasks.get(task_id)
        if task:
            return task
        status_file = TASKS_DIR / task_id / "status.json"
        if status_file.exists():
            task = TaskRecord.model_validate_json(status_file.read_text())
            # A non-terminal task that is only on disk (not in memory) has no
            # live worker behind it — it is an orphan from a previous process.
            # Reconcile it the same way load_from_disk() does, so callers never
            # see a "running" task that nothing is actually advancing.
            if task.status not in _TERMINAL:
                task.status = TaskStatus.FAILED
                task.error = "Server restarted during processing"
                task.completed_at = datetime.now()
                self._persist(task)
            self._tasks[task_id] = task
            return task
        return None

    def list_tasks(self, limit: int = 20, offset: int = 0, status_filter: str | None = None) -> tuple[list[TaskSummary], int]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        if status_filter:
            tasks = [t for t in tasks if t.status.value == status_filter]
        total = len(tasks)
        page = tasks[offset: offset + limit]
        summaries = [
            TaskSummary(
                task_id=t.task_id,
                mode=t.mode.value,
                status=t.status.value,
                paper_count=len(t.papers),
                created_at=t.created_at.isoformat(),
            )
            for t in page
        ]
        return summaries, total

    def delete_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status not in _TERMINAL:
            return False
        task_dir = TASKS_DIR / task_id
        if task_dir.exists():
            import shutil
            shutil.rmtree(task_dir, ignore_errors=True)
        del self._tasks[task_id]
        return True

    def load_from_disk(self):
        if not TASKS_DIR.exists():
            return
        for task_dir in sorted(TASKS_DIR.iterdir()):
            if not task_dir.is_dir():
                continue
            status_file = task_dir / "status.json"
            if not status_file.exists():
                continue
            try:
                task = TaskRecord.model_validate_json(status_file.read_text())
                if task.status not in _TERMINAL:
                    task.status = TaskStatus.FAILED
                    task.error = "Server restarted during processing"
                    task.completed_at = datetime.now()
                    self._persist(task)
                self._tasks[task.task_id] = task
            except Exception as e:
                log.warning("Failed to load task from %s: %s", task_dir, e)
        log.info(
            "Task recovery: loaded %d task(s), %d unfinished marked failed",
            len(self._tasks),
            sum(1 for t in self._tasks.values()
                if t.status == TaskStatus.FAILED and t.error == "Server restarted during processing"),
        )

    def _persist(self, task: TaskRecord):
        task_dir = TASKS_DIR / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        status_file = task_dir / "status.json"
        # Atomic write: a crash mid-write must not leave a truncated status.json
        # that breaks restart recovery. Write to a temp file then rename (atomic
        # on the same filesystem).
        tmp = status_file.with_suffix(".json.tmp")
        tmp.write_text(task.model_dump_json(indent=2))
        tmp.replace(status_file)
