"""Submission-time decisions and safe transfer of unfinished paper work."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from api.config import BASE_DIR
from api.models import TaskStatus

_ACTIVE = {'queued', 'extracting', 'detecting', 'reviewing', 'generating_report'}


def api_dry_run() -> bool:
    """Return whether an isolated API instance must not touch production DBs.

    This is intentionally process-wide and environment-controlled: pressure tests
    can exercise the real HTTP upload/submit/task pipeline against an isolated data
    root without creating or overwriting ``detection_reports`` rows.
    """
    return os.environ.get("PAPER_API_DRY_RUN", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def submission_scope(file_id: str) -> str:
    # file_id identifies an uploaded file, across repeated detection submissions.
    # Neither username nor DOI identifies that upload.
    return 'upload:' + (file_id or '')


@lru_cache(maxsize=1)
def rules_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = [BASE_DIR / 'main.py']
    for directory in ('api', 'core', 'modules', 'utils', 'scripts'):
        paths.extend(sorted((BASE_DIR / directory).rglob('*.py')))
    from api.config import REVIEW_SKILL_SCRIPTS
    paths.extend(sorted(REVIEW_SKILL_SCRIPTS.glob('*.py')))
    for config in (BASE_DIR / '.env', Path.home() / '.claude/settings.json'):
        if config.is_file():
            paths.append(config)
    for path in paths:
        digest.update(str(path).encode())
        digest.update(path.read_bytes())
    env = {k: v for k, v in os.environ.items()
           if k.startswith(('REVIEW_', 'ANTHROPIC_', 'OPENAI_'))
           or k in ('PAPER_CLEAN_JUNK', 'PAPER_PREPROCESS')}
    digest.update(json.dumps(env, sort_keys=True).encode())
    return digest.hexdigest()


def material_fingerprint(directory: str) -> str:
    root = Path(directory)
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob('*') if p.is_file()
                   and '__MACOSX' not in p.parts and p.name != '.DS_Store')
    if not files:
        raise ValueError('Cannot fingerprint empty paper inputs')
    for path in files:
        if path.is_symlink():
            raise ValueError('Cannot reuse symlinked paper inputs')
        digest.update(path.relative_to(root).as_posix().encode() + b'\0')
        digest.update(str(path.stat().st_size).encode() + b'\0')
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        digest.update(b'\0')
    return digest.hexdigest()


def same_paper(current, previous: dict) -> bool:
    return bool(current.submission_no
                and current.submission_no == previous.get('submission_no')
                and (current.fold_name or '') == (previous.get('fold_name') or ''))


def choose_previous(paper, history):
    # Use the most recent matching submission at arrival, not its later state.
    for entry in history:
        for old in entry['papers']:
            if not old.get('superseded_by') and same_paper(paper, old):
                return entry, old
    return None, None


async def blocking_write(operation, *args, **kwargs):
    """Cancellation must drain a DB/file write before a successor takes over."""
    job = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        await job
        raise


async def prepare_submission(task_id, tm):
    """Called after extraction; no old work is cancelled for invalid uploads."""
    task = tm.get_task(task_id)
    for paper in task.papers:
        paper.material_fingerprint = await asyncio.to_thread(material_fingerprint, paper.input_dir)
    async with tm.submission_lock:
        for newer in tm._tasks.values():
            if (newer.task_id == task_id or newer.created_at <= task.created_at
                    or newer.config.table_name != 'detection_reports'
                    or newer.status.value not in _ACTIVE):
                continue
            newer_sub = newer.config.submission_no or next((p.submission_no for p in newer.papers), '')
            if submission_scope(newer_sub) != submission_scope(task.config.submission_no):
                continue
            for paper in task.papers:
                if any(p.material_fingerprint and same_paper(paper, p.model_dump()) for p in newer.papers):
                    paper.superseded_by = newer.task_id
                    paper.status = 'superseded'
            if all(p.superseded_by for p in task.papers):
                task.superseded_by = newer.task_id
                tm.update_task(task_id, status=TaskStatus.SUPERSEDED, stage='superseded')
                return
        for entry in task.submission_history:
            previous = tm.get_task(entry['task_id'])
            if previous and not entry['papers']:
                entry['papers'] = [p.model_dump() for p in previous.papers]
        for paper in task.papers:
            if paper.superseded_by:
                continue
            entry, prior = choose_previous(paper, task.submission_history)
            if not entry:
                continue
            may_reuse = entry['status'] in _ACTIVE and not prior.get('has_final_verdict')
            if not may_reuse:
                # A terminal submission always gets fresh inputs/outputs and
                # no reviewer cache. Normal risk gating applies to the new run.
                paper.submission_decision = 'fresh_after_terminal'
            else:
                paper.submission_decision = 'replace_unfinished'
            affected = []
            for snapshot in task.submission_history:
                if snapshot['status'] not in _ACTIVE:
                    continue
                old_task = tm.get_task(snapshot['task_id'])
                if not old_task:
                    continue
                for old_paper in old_task.papers:
                    was_final = any(p.get('has_final_verdict') and same_paper(paper, p)
                                    for p in snapshot['papers'])
                    if (same_paper(paper, old_paper.model_dump())
                            and not old_paper.superseded_by and not was_final):
                        affected.append((old_task, old_paper))
            # Joining cancellation also reaps owned detector/reviewer subprocesses.
            for old_id in dict.fromkeys(old.task_id for old, _ in affected):
                await tm.stop_worker(old_id)
            source = next(((old, p) for old, p in affected if old.task_id == entry['task_id']), None)
            if source and may_reuse:
                old_task, old_paper = source
                compatible = (
                    bool(old_task.config.rules_fingerprint)
                    and old_task.config.rules_fingerprint == task.config.rules_fingerprint
                    and old_task.config.author_type == task.config.author_type
                    and old_task.config.skip_refs == task.config.skip_refs
                    and old_paper.material_fingerprint == paper.material_fingerprint
                )
                if compatible:
                    try:
                        compatible = (await asyncio.to_thread(material_fingerprint, old_paper.input_dir)
                                      == paper.material_fingerprint)
                    except (OSError, ValueError):
                        compatible = False
                if compatible and old_paper.status in {'detected', 'review_error', 'persist_error'}:
                    report = Path(old_paper.output_dir) / 'report.json'
                    if report.is_file():
                        try:
                            findings = json.loads(report.read_text())
                        except (OSError, ValueError):
                            findings = {}
                        if findings.get('paper') and 'summary' in findings:
                            paper.input_dir = old_paper.input_dir
                            paper.output_dir = old_paper.output_dir
                            paper.report_json = str(report)
                            paper.status = 'detected'
                            paper.reused_from_task_id = old_task.task_id
                            paper.initial_report_namespace = (old_paper.initial_report_namespace
                                                              or f'detection_reports/{old_task.task_id}')
                            paper.submission_decision = 'resume_unfinished'
            for old_task, old_paper in affected:
                old_paper.superseded_by = task_id
                old_paper.status = 'superseded'
                if all(p.superseded_by for p in old_task.papers):
                    old_task.superseded_by = task_id
                    tm.update_task(old_task.task_id, status=TaskStatus.SUPERSEDED,
                                   stage='superseded', error=None)
                else:
                    tm.update_task(old_task.task_id, status=TaskStatus.QUEUED,
                                   stage='recovery_queued', error=None)
            tm.update_task(task_id, papers=task.papers)
            for old_id in dict.fromkeys(old.task_id for old, _ in affected):
                tm.start_detection_worker(old_id)


async def finalize_submission(task_id, tm):
    from api.services.detection_reports_db import claim_submission
    await prepare_submission(task_id, tm)
    record = tm.get_task(task_id)
    if record.status != TaskStatus.SUPERSEDED:
        if not api_dry_run():
            for paper in record.papers:
                if not paper.superseded_by:
                    await blocking_write(claim_submission, paper.submission_no, paper.fold_name, task_id, paper.doi)
        record.submission_prepared = True
        tm.update_task(task_id, status=TaskStatus.QUEUED, stage='queued')
