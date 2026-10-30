from __future__ import annotations

import logging
import asyncio
import threading
import uuid
import os
from datetime import datetime
from pathlib import Path

from api.config import TASKS_DIR
from api.models import TaskConfig, TaskMode, TaskRecord, TaskStatus, TaskSummary

log = logging.getLogger(__name__)

TASKS_DIR.mkdir(parents=True, exist_ok=True)

_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SUPERSEDED}
_UNSET = object()


class SubmissionInProgress(Exception):
    def __init__(self, task):
        self.task = task
        super().__init__(task.task_id)


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
        self._workers = {}
        self.submission_lock = asyncio.Lock()

    def start_detection_worker(self, task_id):
        from api.services.detection_reports_worker import run_detection_reports_pipeline
        from api.services.submission_policy import finalize_submission
        record = self._tasks[task_id]
        if record.status in _TERMINAL:
            return
        if task_id in self._workers and not self._workers[task_id].done():
            return
        async def run():
            if not record.submission_prepared:
                try:
                    await finalize_submission(task_id, self)
                except Exception as exc:
                    self.update_task(task_id, status=TaskStatus.FAILED, error=str(exc))
                    return
            if record.status != TaskStatus.SUPERSEDED:
                await run_detection_reports_pipeline(task_id, self, record.papers, record.config.model_dump())
        worker = asyncio.create_task(run())
        self._workers[task_id] = worker

    async def stop_worker(self, task_id):
        worker = self._workers.get(task_id)
        if worker and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def start_pipeline_worker(self, task_id):
        """Track ordinary API jobs just like detection_reports jobs."""
        from api.worker import run_pipeline
        record = self._tasks[task_id]
        worker = self._workers.get(task_id)
        if record.status in _TERMINAL or (worker and not worker.done()):
            return
        self._workers[task_id] = asyncio.create_task(
            run_pipeline(task_id, self, record.papers, record.config.model_dump()))

    def create_detection_submission(self, mode, config, max_pending):
        """Snapshot old states atomically before extraction or other awaits."""
        from api.services.submission_policy import submission_scope
        with self._lock:
            if self._unfinished_count_locked() >= max_pending:
                raise CapacityExceeded(self._unfinished_count_locked(), max_pending)
            scope = submission_scope(config.submission_no)
            history = []
            for old in sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True):
                if old.config.table_name != 'detection_reports':
                    continue
                old_sub = old.config.submission_no or next((p.submission_no for p in old.papers), '')
                if submission_scope(old_sub) != scope:
                    continue
                paper_snapshots = []
                for paper in old.papers:
                    snapshot = paper.model_dump()
                    records = (old.result or {}).get('detection_reports', [])
                    snapshot['has_final_verdict'] = any(
                        r.get('submission_no') == paper.submission_no
                        and (r.get('fold_name') or '') == paper.fold_name
                        and (r.get('review_result') in ('高风险', '低风险')
                             or (r.get('chushen_result') == '低风险' and r.get('processing_completed')))
                        for r in records
                    )
                    paper_snapshots.append(snapshot)
                history.append({
                    'task_id': old.task_id, 'status': old.status.value,
                    'config': old.config.model_dump(),
                    'papers': paper_snapshots,
                })
            task_id = self._create_locked(mode, config)
            task = self._tasks[task_id]
            task.submission_prepared = False
            task.submission_history = history
            self._persist(task)
            return task_id

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
            if config.submission_no:
                for task in self._tasks.values():
                    submission = task.config.submission_no or next(
                        (p.submission_no for p in task.papers if p.submission_no), ""
                    )
                    if (task.config.table_name == config.table_name
                            and submission == config.submission_no
                            and task.status not in _TERMINAL):
                        raise SubmissionInProgress(task)
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
        result: dict | None = _UNSET,
        error: str | None = _UNSET,
        papers: list | None = None,
    ):
        task = self._tasks.get(task_id)
        if not task:
            return
        if task.status == TaskStatus.SUPERSEDED:
            return
        if status is not None:
            task.status = status
        if stage is not None:
            task.stage = stage
        if progress is not None:
            task.progress = progress
        if result is not _UNSET:
            task.result = result
        if error is not _UNSET:
            task.error = error
        if papers is not None:
            kept = [p for p in task.papers if p.superseded_by and p not in papers]
            task.papers = list(papers) + kept
        task.updated_at = datetime.now()
        if status in _TERMINAL:
            task.completed_at = datetime.now()
        if status is not None and status not in _TERMINAL:
            task.completed_at = None
            if error is _UNSET:
                task.error = None
        self._persist(task)

    def get_task(self, task_id: str) -> TaskRecord | None:
        task = self._tasks.get(task_id)
        if task:
            # An isolated recovery worker can update a failed task without
            # restarting unrelated API jobs. Refresh its persisted status when
            # this process has no running worker for it; never replace a live
            # worker's in-memory record with a disk snapshot.
            worker = self._workers.get(task_id)
            if task.config.table_name == 'detection_reports' and (worker is None or worker.done()):
                try:
                    latest = TaskRecord.model_validate_json((TASKS_DIR / task_id / 'status.json').read_text())
                    if (latest.task_id == task_id and latest.created_at == task.created_at
                            and latest.updated_at > task.updated_at):
                        self._tasks[task_id] = task = latest
                except (OSError, ValueError):
                    pass
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
        # Use the same refresh/protection rules as the detail endpoint BEFORE
        # filtering and counting, so a recovered task cannot remain in failed.
        tasks = [self.get_task(task_id) for task_id in list(self._tasks)]
        tasks = sorted((task for task in tasks if task is not None),
                       key=lambda t: t.created_at, reverse=True)
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
        """Load persisted tasks and prepare safe restart recovery.

        A previous implementation converted every non-terminal task into a
        failure as soon as the API process started.  That made an ordinary
        deploy/restart look like a paper-detection failure.  Tasks that have
        persisted paper inputs can be replayed idempotently by the appropriate
        worker; only tasks interrupted before extraction (or whose input was
        removed) are unrecoverable and remain failed.

        Returns the records that the application lifespan should requeue.
        """
        if not TASKS_DIR.exists():
            return []
        recoverable = []
        for task_dir in sorted(TASKS_DIR.iterdir()):
            if not task_dir.is_dir():
                continue
            status_file = task_dir / "status.json"
            if not status_file.exists():
                continue
            try:
                task = TaskRecord.model_validate_json(status_file.read_text())
                if task.status not in _TERMINAL:
                    # Extraction persists PaperInfo before the worker starts.
                    # Replaying from those inputs is safe: detection/report DB
                    # writes are upserts and the workers verify their writes.
                    resume_papers = [p for p in task.papers if not p.superseded_by]
                    input_paths = [Path(p.input_dir) for p in resume_papers if p.input_dir]
                    # Every persisted paper must have an input directory.  A
                    # partially extracted batch must not be replayed as if all
                    # papers were present, otherwise the task can be reported
                    # complete while silently skipping or failing one member.
                    can_resume = (
                        bool(resume_papers)
                        and len(input_paths) == len(resume_papers)
                        and all(p.exists() and p.is_dir() for p in input_paths)
                    )
                    if can_resume:
                        task.status = TaskStatus.QUEUED
                        task.stage = "recovery_queued"
                        task.error = None
                        task.result = None
                        task.completed_at = None
                        task.updated_at = datetime.now()
                        self._persist(task)
                        recoverable.append(task)
                        log.warning(
                            "Recovering interrupted task %s from persisted paper inputs (%d paper(s))",
                            task.task_id, len(task.papers),
                        )
                    else:
                        task.status = TaskStatus.FAILED
                        task.error = "Server restarted during processing (task inputs unavailable)"
                        task.completed_at = datetime.now()
                        self._persist(task)
                self._tasks[task.task_id] = task
            except Exception as e:
                log.warning("Failed to load task from %s: %s", task_dir, e)
        # Only the newest task for a submission may resume. Older workers
        # would otherwise reset or overwrite the newer task's business row.
        newest = {}
        for task in sorted(self._tasks.values(), key=lambda t: t.created_at):
            submission = task.config.submission_no or next(
                (p.submission_no for p in task.papers if p.submission_no), ""
            )
            if (task.config.table_name == "detection_reports" and submission
                    and task.submission_prepared and task.papers):
                newest[submission] = task.task_id
        for task in list(recoverable):
            submission = task.config.submission_no or next(
                (p.submission_no for p in task.papers if p.submission_no), ""
            )
            if (task.config.table_name == "detection_reports" and submission
                    and task.submission_prepared and newest.get(submission) != task.task_id):
                self.update_task(task.task_id, status=TaskStatus.FAILED,
                                 stage="superseded", error="Superseded by a newer submission task")
                recoverable.remove(task)
        log.info(
            "Task recovery: loaded %d task(s), %d task(s) queued for replay, %d unrecoverable",
            len(self._tasks),
            len(recoverable),
            sum(1 for t in self._tasks.values()
                if t.status == TaskStatus.FAILED and t.error and t.error.startswith("Server restarted during processing")),
        )
        return recoverable

    def _persist(self, task: TaskRecord):
        task_dir = TASKS_DIR / task.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        status_file = task_dir / "status.json"
        # Atomic write: a crash mid-write must not leave a truncated status.json
        # that breaks restart recovery. Write to a temp file then rename (atomic
        # on the same filesystem).
        # Include the process/thread identity in the temporary name.  A restart
        # or an overlapping recovery worker must never rename/unlink another
        # writer's fixed ``status.json.tmp`` file.
        tmp = status_file.with_name(
            f"status.json.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(task.model_dump_json(indent=2))
        tmp.replace(status_file)
