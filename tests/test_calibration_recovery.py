"""Durable grading, bounded recovery and existing-finding updates (offline)."""
import asyncio
import json
import time

import pytest

from review_v3 import engine, ledger, prompts, session


def finding(item_id):
    return {"id": item_id, "status": "issue", "severity": "minor", "dimension": "data",
            "title": item_id, "location": "Table 1 p2", "issue_type": "孤立单格错误",
            "finding": "表中数值为 10，源数据正确值为 11", "why": "单格错误不影响核心比较",
            "basis": "已核对原表格和源数据，其余数值一致"}


def seed(path, ids):
    ledger.create(path, {"allow_new": False, "require_category": True},
                  [{"id": i, "dimension": "data", "first": finding(i)} for i in ids])


def grade(path, item_id):
    result = ledger.update(path, {"id": item_id, "status": "issue", "category": "M1",
                                 "why": "孤立单格且不改变核心比较结论"}, "calibrator")
    assert result == (1, []), result


@pytest.mark.parametrize('status', ['issue', 'cleared', 'not_applicable', 'unresolved'])
def test_existing_new_finding_inherits_dimension_for_every_status(tmp_path, status):
    path = tmp_path / 'cal.json'
    seed(path, ['new:known'])
    entry = {'id': 'new:known', 'status': status, 'category': 'M1',
             'why': '单格差异不影响核心比较结果', 'basis': '已逐项核对原文和源数据，具体数值没有矛盾',
             'note': '此项缺少原始测量文件，无法进一步核查'}
    assert ledger.update(path, entry, 'calibrator') == (1, [])
    assert ledger.load(path)['items']['new:known']['dimension'] == 'data'


@pytest.mark.parametrize('dimension', ['', None, 'invalid'])
def test_explicit_invalid_dimension_is_not_hidden(tmp_path, dimension):
    path = tmp_path / 'cal.json'
    seed(path, ['new:known'])
    applied, errors = ledger.update(path, {'id': 'new:known', 'status': 'cleared', 'dimension': dimension,
                                         'basis': '已逐项核对原文和源数据，具体数值没有矛盾'}, 'calibrator')
    assert applied == 0 and any('dimension' in e for e in errors)


def test_new_unknown_and_partial_batches_remain_validated(tmp_path):
    path = tmp_path / 'cal.json'
    seed(path, ['new:known'])
    entry = {'status': 'issue', 'category': 'M1', 'why': '单格差异不影响核心比较结果'}
    applied, errors = ledger.update(path, [dict(entry, id='new:known'), dict(entry, id='new:unknown')], 'calibrator')
    assert applied == 1 and any('不在本台账' in e for e in errors)
    assert 'new:unknown' not in ledger.load(path)['items']
    new_path = tmp_path / 'investigate.json'
    ledger.create(new_path, {'allow_new': True}, [])
    new = finding('new:unknown')
    new.pop('dimension')
    applied, errors = ledger.update(new_path, new, 'investigator')
    assert applied == 0 and any('dimension' in e for e in errors)


def source_state(tmp_path, **changes):
    spec = session.SessionSpec('cal_all', tmp_path, '', 'calibrator')
    state = dict(session_id='original-session', status='wrapped', chunks=2, started=True,
                 finished_at=time.time(), wrapped=True, stop_reason='wrapped',
                 metrics=[{'closed': 1, 'error': 'chunk timeout'}, {'closed': 1, 'error': 'chunk timeout'}])
    state.update(changes)
    session._save_state(spec, state)
    return session._state_path(spec).read_bytes()


def test_handoff_preserves_id_spend_and_source_audit(tmp_path):
    original = source_state(tmp_path)
    carry = session.continue_as('cal_all', 'cal_rest', tmp_path)
    assert carry['session_id'] == 'original-session'
    assert carry['chunks'] == carry['continuation_base_chunks'] == 2
    assert carry['metrics'] == [] and carry['continuation_mode'] == 'resume'
    assert 'wrapped' not in carry and 'status' not in carry
    assert (tmp_path / 'sessions/cal_all.json').read_bytes() == original
    target = session.SessionSpec('cal_rest', tmp_path, '', 'calibrator')
    carry.update(status='exhausted', chunks=5, metrics=[{'closed': 1}])
    session._save_state(target, carry)
    again = session.continue_as('cal_all', 'cal_rest', tmp_path)
    assert again['chunks'] == 5 and again['status'] == 'exhausted'


@pytest.mark.parametrize('changes', [
    {'status': 'error'}, {'status': 'stalled'}, {'finished_at': None}, {'restart': True},
    {'metrics': [{'closed': 0, 'error': 'chunk timeout'}, {'closed': 0, 'error': 'chunk timeout'}]},
    {'metrics': [{'closed': 2}, {'closed': 2, 'error': 'chunk timeout'}, {'closed': 2, 'error': 'chunk timeout'}]},
])
def test_failed_or_unresponsive_context_starts_fresh(tmp_path, changes):
    source_state(tmp_path, **changes)
    carry = session.continue_as('cal_all', 'cal_rest', tmp_path)
    assert carry['continuation_mode'] == 'restart'
    assert not carry['started'] and carry['continuation_base_chunks'] == 0
    assert 'session_id' not in carry


def test_legacy_recovery_state_keeps_its_budget(tmp_path):
    spec = session.SessionSpec('cal_rest', tmp_path, '', 'calibrator')
    session._save_state(spec, {'chunks': 2, 'status': 'done', 'session_id': 'legacy'})
    carry = session.continue_as('cal_all', 'cal_rest', tmp_path)
    assert carry['chunks'] == 2 and carry['status'] == 'done'
    assert carry['continuation_mode'] == 'existing' and carry['continuation_base_chunks'] == 0


@pytest.mark.parametrize('lose_context', [False, True])
def test_partial_timeout_recovers_only_remaining_items_and_survives_restart(tmp_path, monkeypatch, lose_context):
    calls = []
    monkeypatch.setattr(session, 'tool_waits', lambda *a: {})
    ids = ['new:first', 'new:second', 'new:third']

    async def invoke(spec, message, sid, resume, turns, wrapping=False, debug=False):
        calls.append((spec.name, sid, resume, wrapping, message))
        if spec.name == 'cal_all':
            if not wrapping:
                grade(spec.ledger_path, ids[0])
                spec.soft_deadline = time.monotonic() - 1
            return {'_error': 'chunk timeout', '_timeout': True, '_wall': .01}
        data = ledger.load(spec.ledger_path)
        assert set(data['items']) == set(ids[1:])
        assert 'new:first' in message  # summary kept for cross-finding reasoning
        remaining = ledger.open_items(data)
        n = sum(c[0] == 'cal_rest' for c in calls)
        if lose_context and n == 2:
            return {'_error': 'session not found', '_wall': .01}
        if lose_context and n == 3:
            assert not resume and sid != calls[0][1]
            assert '待定级的问题（1 项' in message
            assert 'new:second' in message  # persisted result is in handoff summary
        grade(spec.ledger_path, remaining[0]['id'])
        return {'subtype': 'success', '_wall': .01}

    monkeypatch.setattr(session, '_invoke', invoke)

    async def run():
        review = engine.Review('x', '', str(tmp_path), tmp_path / 'run', None)
        finals = {i: finding(i) for i in ids}
        result = await review.calibrate(finals)
        assert all(result[i]['category'] == 'M1' for i in ids)
        assert review.cal_status == 'done'
        assert review.calibration_metrics['pending_ids'] == []
        assert review.calibration_metrics['recovery_mode'] == 'resume'
        main = json.loads((review.run_dir / 'sessions/cal_all.json').read_text())
        rest = json.loads((review.run_dir / 'sessions/cal_rest.json').read_text())
        assert rest['continuation_base_chunks'] == main['chunks'] == 2
        assert rest['chunks'] == 4
        assert calls[2][1] == calls[0][1] and calls[2][2]
        assert rest['metrics'][0]['closed_before'] == 0
        assert rest['metrics'][0]['closed_delta'] == 1
        assert main['metrics'][1]['wrapping']
        count = len(calls)
        await review.close()
        restarted = engine.Review('x', '', str(tmp_path), review.run_dir, None)
        assert await restarted.calibrate(finals) == result
        assert len(calls) == count
        await restarted.close()
    asyncio.run(run())


def test_recovery_failure_is_incomplete_not_success(tmp_path, monkeypatch):
    async def fake_run(spec):
        if spec.name == 'cal_all':
            grade(spec.ledger_path, 'new:first')
        return session.SessionOutcome('wrapped')
    monkeypatch.setattr(session, 'run', fake_run)

    async def run():
        review = engine.Review('x', '', str(tmp_path), tmp_path / 'run', None)
        originals = {i: finding(i) for i in ['new:first', 'new:second']}
        out = await review.calibrate(originals)
        assert review.cal_status == 'incomplete'
        assert review.calibration_metrics['pending_ids'] == ['new:second']
        assert 'category' not in out['new:second']
        assert out['new:second'] == originals['new:second']
        await review.close()
    asyncio.run(run())


def test_expired_deadline_prevents_new_recovery_and_keeps_saved_results(tmp_path, monkeypatch):
    async def run():
        review = engine.Review('x', '', str(tmp_path), tmp_path / 'run', None)
        calls = []
        async def fake_run(spec):
            calls.append(spec.name)
            grade(spec.ledger_path, 'new:first')
            review.deadline = time.monotonic() - 1
            return session.SessionOutcome('deadline')
        monkeypatch.setattr(session, 'run', fake_run)
        out = await review.calibrate({i: finding(i) for i in ['new:first', 'new:second']})
        assert calls == ['cal_all']
        assert out['new:first']['category'] == 'M1'
        assert review.cal_status == 'incomplete'
        assert not (review.run_dir / 'sessions/cal_rest.json').exists()
        await review.close()
    asyncio.run(run())


def test_cancellation_keeps_partial_grades_and_records_interruption(tmp_path, monkeypatch):
    async def fake_run(spec):
        grade(spec.ledger_path, 'new:first')
        raise asyncio.CancelledError
    monkeypatch.setattr(session, 'run', fake_run)

    async def run():
        review = engine.Review('x', '', str(tmp_path), tmp_path / 'run', None)
        with pytest.raises(asyncio.CancelledError):
            await review.calibrate({i: finding(i) for i in ['new:first', 'new:second']})
        assert ledger.load(review.ledger_path('cal', 'all'))['items']['new:first']['category'] == 'M1'
        saved = json.loads((review.run_dir / 'calibration.json').read_text())
        assert saved['status'] == 'incomplete' and saved['passes'][-1]['status'] == 'interrupted'
        await review.close()
    asyncio.run(run())


def test_continuation_requests_small_saves_but_lists_all_pending_items():
    ids = [f'new:pending{i}' for i in range(10)]
    prompt = prompts.calibrator_continue(ids)
    assert all(i in prompt for i in ids)
    assert prompt.count('"category"') == 3
    assert '每完成 1–3 条' in prompt
