import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api.models import PaperInfo, TaskConfig, TaskMode, TaskStatus
from api.services import task_manager as managers
from api.services import submission_policy as policy


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(managers, 'TASKS_DIR', tmp_path / 'tasks')
    manager = managers.TaskManager()
    starts = []
    monkeypatch.setattr(manager, 'start_detection_worker', starts.append)

    def make(sub, content=b'original', status=TaskStatus.REVIEWING, rules='v1', second=False):
        config = TaskConfig(table_name='detection_reports', submission_no=sub,
                            rules_fingerprint=rules, requested_doi='10.1000/test')
        tid = manager.create_detection_submission(TaskMode.SINGLE, config, 240)
        root = tmp_path / tid
        inputs = root / 'input'; inputs.mkdir(parents=True)
        (inputs / 'paper.pdf').write_bytes(content)
        output = root / 'output'; output.mkdir()
        (output / 'report.json').write_text(json.dumps({'paper': {'doi': '10.1000/test'}, 'summary': {}}))
        paper = PaperInfo(doi='10.1000/test', doi_slug='10.1000_test',
                          input_dir=str(inputs), output_dir=str(output),
                          material_fingerprint=policy.material_fingerprint(str(inputs)),
                          submission_no=sub, status='detected')
        papers = [paper]
        if second:
            papers.append(paper.model_copy(update={'doi': '10.1000/other', 'doi_slug': 'other',
                                                  'fold_name': 'other-paper'}))
        manager.update_task(tid, status=status, papers=papers)
        return manager.get_task(tid)

    return manager, make, starts


OLD = '20260928080000_alice_aabbccdd'
NEW = OLD  # A repeated submit keeps the exact uploaded file_id.
OTHER_FILE = '20260928090000_alice_11223344'


def test_active_identical_submission_stops_old_and_reuses_checkpoint(setup):
    manager, make, starts = setup

    async def scenario():
        old = make(OLD)
        never = asyncio.Event()
        running = asyncio.create_task(never.wait())
        manager._workers[old.task_id] = running
        new = make(NEW, status=TaskStatus.EXTRACTING)
        await policy.prepare_submission(new.task_id, manager)
        assert running.cancelled()
        assert old.status == TaskStatus.SUPERSEDED
        assert old.superseded_by == new.task_id
        assert new.papers[0].output_dir == old.papers[0].output_dir
        assert new.papers[0].reused_from_task_id == old.task_id
        assert new.papers[0].submission_decision == 'resume_unfinished'

    asyncio.run(scenario())


@pytest.mark.parametrize('verdict', ['高风险', '低风险'])
def test_completed_at_arrival_requires_fresh_workflow(setup, verdict):
    manager, make, starts = setup
    old = make(OLD, status=TaskStatus.COMPLETED)
    old.result = {'detection_reports': [{'review_result': verdict, 'processing_completed': True}]}
    new = make(NEW, status=TaskStatus.EXTRACTING)
    expected = new.papers[0].output_dir
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert new.papers[0].submission_decision == 'fresh_after_terminal'
    assert new.papers[0].output_dir == expected
    assert not new.papers[0].reused_from_task_id
    assert old.status == TaskStatus.COMPLETED
    assert not old.superseded_by


def test_failed_at_arrival_reuses_unfinished_checkpoint(setup):
    manager, make, _ = setup
    old = make(OLD, status=TaskStatus.FAILED)
    old.result = {'detection_reports': [{
        'submission_no': OLD, 'fold_name': None,
        'chushen_result': '高风险', 'review_result': None,
    }]}
    new = make(NEW, status=TaskStatus.EXTRACTING)
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert old.status == TaskStatus.SUPERSEDED
    assert new.papers[0].reused_from_task_id == old.task_id
    assert new.papers[0].submission_decision == 'resume_unfinished'
    assert new.papers[0].output_dir == old.papers[0].output_dir


@pytest.mark.parametrize('change', ['material', 'rules', 'author'])
def test_incompatible_active_submission_is_replaced_without_cache(setup, change):
    manager, make, _ = setup
    old = make(OLD)
    new = make(NEW, status=TaskStatus.EXTRACTING,
               content=b'changed' if change == 'material' else b'original',
               rules='v2' if change == 'rules' else 'v1')
    if change == 'author':
        new.config.author_type = 'different'
    expected = new.papers[0].output_dir
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert old.status == TaskStatus.SUPERSEDED
    assert new.papers[0].output_dir == expected
    assert not new.papers[0].reused_from_task_id


def test_arrival_snapshot_not_later_completion_decides_reuse(setup):
    manager, make, _ = setup
    old = make(OLD)
    new = make(NEW, status=TaskStatus.EXTRACTING)
    manager.update_task(old.task_id, status=TaskStatus.COMPLETED)
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert new.papers[0].reused_from_task_id == old.task_id


def test_paper_with_final_result_inside_unfinished_batch_is_fresh(setup):
    manager, make, _ = setup
    old = make(OLD, second=True)
    old.result = {'detection_reports': [{'submission_no': OLD, 'fold_name': None,
                                        'review_result': '低风险'}]}
    new = make(NEW, status=TaskStatus.EXTRACTING)
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert new.papers[0].submission_decision == 'fresh_after_terminal'
    assert not new.papers[0].reused_from_task_id
    assert not old.papers[0].superseded_by


def test_other_upload_user_is_not_cancelled_or_reused(setup):
    manager, make, _ = setup
    old = make(OLD)
    new = make('20260928090000_bob_11223344', status=TaskStatus.EXTRACTING)
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert old.status == TaskStatus.REVIEWING
    assert not new.papers[0].reused_from_task_id


@pytest.mark.parametrize('old_status', [TaskStatus.REVIEWING, TaskStatus.COMPLETED])
def test_same_user_doi_and_bytes_with_different_file_id_are_independent(setup, old_status):
    manager, make, starts = setup

    async def scenario():
        old = make(OLD, status=old_status)
        running = asyncio.create_task(asyncio.Event().wait())
        manager._workers[old.task_id] = running
        try:
            new = make(OTHER_FILE, status=TaskStatus.EXTRACTING)
            expected = new.papers[0].output_dir
            await policy.prepare_submission(new.task_id, manager)
            assert not running.done()
            assert old.status == old_status
            assert not old.superseded_by
            assert not new.submission_history
            assert new.papers[0].output_dir == expected
            assert new.papers[0].submission_decision == 'fresh'
            assert not new.papers[0].reused_from_task_id
            assert not starts
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize('doi', ['', '10.1000/corrected'])
def test_same_file_id_identity_does_not_depend_on_doi(setup, doi):
    manager, make, _ = setup
    old = make(OLD)
    new = make(NEW, status=TaskStatus.EXTRACTING)
    new.papers[0].doi = doi
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert old.status == TaskStatus.SUPERSEDED
    assert new.papers[0].reused_from_task_id == old.task_id


def test_batch_folders_do_not_match_even_with_same_doi_and_bytes(setup):
    manager, make, _ = setup
    old = make(OLD)
    new = make(NEW, status=TaskStatus.EXTRACTING)
    new.papers[0].fold_name = 'different-folder'
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert old.status == TaskStatus.REVIEWING
    assert not new.papers[0].reused_from_task_id


def test_partial_batch_restarts_unaffected_papers(setup):
    manager, make, starts = setup
    old = make(OLD, second=True)
    new = make(NEW, status=TaskStatus.EXTRACTING)
    asyncio.run(policy.prepare_submission(new.task_id, manager))
    assert old.status == TaskStatus.QUEUED
    assert old.papers[0].superseded_by == new.task_id
    assert not old.papers[1].superseded_by
    assert old.task_id in starts
    manager.update_task(old.task_id, papers=[old.papers[1]])
    assert len(old.papers) == 2


def test_late_extraction_cannot_replace_newer_submission(setup):
    manager, make, _ = setup
    old = make(OLD, status=TaskStatus.EXTRACTING)
    new = make(NEW, status=TaskStatus.EXTRACTING)
    asyncio.run(policy.prepare_submission(old.task_id, manager))
    assert old.status == TaskStatus.SUPERSEDED
    assert new.status == TaskStatus.EXTRACTING


def test_material_hash_ignores_container_path_but_includes_source_changes(tmp_path):
    for name in ('a', 'b'):
        path = tmp_path / name; path.mkdir()
        (path / 'paper.pdf').write_bytes(b'pdf')
        (path / 'source.xlsx').write_bytes(b'data')
    assert policy.material_fingerprint(str(tmp_path / 'a')) == policy.material_fingerprint(str(tmp_path / 'b'))
    (tmp_path / 'b' / 'source.xlsx').write_bytes(b'changed')
    assert policy.material_fingerprint(str(tmp_path / 'a')) != policy.material_fingerprint(str(tmp_path / 'b'))


def test_unknown_or_anonymous_uploads_are_not_grouped_by_doi():
    assert policy.submission_scope('20260928080000_anonymous_aabbccdd') != policy.submission_scope('20260928090000_anonymous_11223344')
    assert policy.submission_scope('unrecognized-a') != policy.submission_scope('unrecognized-b')


@pytest.mark.parametrize('old_status', [TaskStatus.REVIEWING, TaskStatus.COMPLETED])
def test_submit_endpoint_applies_snapshot_and_claims_new_task(setup, monkeypatch, tmp_path, old_status):
    from api.routes import detection
    from api.models import DetectionSubmitRequest
    from api.services import detection_reports_db as db
    manager, make, starts = setup
    old = make(OLD, status=old_status)
    upload = tmp_path / 'upload'; upload.mkdir()
    (upload / 'upload.zip').write_bytes(b'archive')
    claims = []

    def extract(archive, tid):
        inputs = tmp_path / tid / 'new-input'; inputs.mkdir(parents=True)
        (inputs / 'paper.pdf').write_bytes(b'original')
        return [PaperInfo(doi='10.1000/test', doi_slug='10.1000_test',
                          input_dir=str(inputs), output_dir=str(tmp_path / tid / 'new-output'))]

    monkeypatch.setattr(detection, 'get_task_manager', lambda: manager)
    monkeypatch.setattr(detection, 'resolve_upload_dir', lambda _: upload)
    monkeypatch.setattr(detection, 'TASKS_DIR', tmp_path / 'tasks')
    monkeypatch.setattr(detection, 'rules_fingerprint', lambda: 'v1')
    monkeypatch.setattr(detection, 'extract_single', extract)
    monkeypatch.setattr(db, 'claim_submission', lambda *args: claims.append(args))
    response = asyncio.run(detection.submit_detection(DetectionSubmitRequest(file_id=NEW, language='zh')))
    new = manager.get_task(response.task_id)
    assert claims == [(NEW, '', new.task_id, '10.1000/test')]
    assert new.task_id != old.task_id
    assert new.task_id in starts
    assert new.submission_prepared
    if old_status == TaskStatus.COMPLETED:
        assert new.papers[0].submission_decision == 'fresh_after_terminal'
        assert new.papers[0].status == 'pending'
        assert not new.papers[0].reused_from_task_id
    else:
        assert old.status == TaskStatus.SUPERSEDED
        assert new.papers[0].reused_from_task_id == old.task_id


def test_review_process_cancellation_kills_tool_children():
    import sys
    from api.services.review import _kill_and_reap_process

    async def scenario():
        script = ('import subprocess,sys,time; '
                  'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                  'print(p.pid,flush=True); time.sleep(60)')
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', script,
                    stdout=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            child = int(await asyncio.wait_for(proc.stdout.readline(), 5))
            await _kill_and_reap_process(proc)
            assert proc.returncode is not None
            for _ in range(20):
                try:
                    state = Path(f'/proc/{child}/stat').read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    return  # reaped, possibly between lookup and read
                if state == 'Z':
                    return
                await asyncio.sleep(0.05)
            raise AssertionError('review tool child is still running after cancellation')
        finally:
            if proc.returncode is None:
                await _kill_and_reap_process(proc)

    asyncio.run(scenario())
