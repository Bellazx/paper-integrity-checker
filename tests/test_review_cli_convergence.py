import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services import review, review_cli
from scripts.scholar_1027_cli import install
from scripts import run_scholar_1027_review as runner


def engine(tmp_path):
    return SimpleNamespace(
        BASE_DIR=tmp_path, REVIEW_MODEL='primary', REVIEW_FALLBACK_MODEL='fallback',
        JUDGE_MAX_TURNS=80, _find_claude_cli=lambda: 'claude',
        _claude_subprocess_env=lambda: {}, _review_subprocess_timeout_seconds=lambda: None,
        _extract_json_from_text=review._extract_json_from_text,
        _normalize_review_result=dict, _clean_internal_references=dict,
        _kill_and_reap_process=AsyncMock(), _fallback_result=review._fallback_result,
    )


def mock_cli(monkeypatch, payloads):
    commands = []
    async def spawn(*cmd, **kwargs):
        commands.append(cmd)
        rc, payload = payloads.pop(0)
        async def communicate():
            return json.dumps(payload).encode(), b''
        return SimpleNamespace(returncode=rc, communicate=communicate)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    return commands


def test_max_turns_recovery_can_finish_without_restart(tmp_path, monkeypatch):
    e = engine(tmp_path)
    commands = mock_cli(monkeypatch, [
        (1, {'is_error': True, 'subtype': 'error_max_turns'}),
        (0, {'result': json.dumps({'doi': 'x', 'result': '低风险'})}),
    ])
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'original evidence prompt', 80))
    assert out['result'] == '低风险'
    assert [c[c.index('--max-turns') + 1] for c in commands] == ['80', '12']
    assert commands[0][commands[0].index('--model') + 1] == 'primary'
    assert commands[0][commands[0].index('--settings') + 1] == '/home/library/.claude/settings-mm.json'
    assert commands[1][commands[1].index('--model') + 1] == 'primary'
    assert commands[1][commands[1].index('--tools') + 1] == 'Bash,Read'
    assert '--allowedTools' in commands[1]
    session = commands[0][commands[0].index('--session-id') + 1]
    assert all(c[c.index('--resume') + 1] == session for c in commands[1:])


def test_max_turns_gets_bounded_targeted_recovery_before_finalizer(tmp_path, monkeypatch):
    e = engine(tmp_path)
    commands = mock_cli(monkeypatch, [
        (1, {'is_error': True, 'subtype': 'error_max_turns'}),
        (1, {'is_error': True, 'subtype': 'error_max_turns'}),
        (0, {'result': json.dumps({'doi': 'x', 'result': '低风险'})}),
    ])
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'original evidence prompt', 80))
    assert out['result'] == '低风险'
    assert [c[c.index('--max-turns') + 1] for c in commands] == ['80', '12', '6']
    # The middle call is a short, tool-enabled continuation; only the final
    # call is output-only. This prevents an exhausted first pass from being
    # turned into a conclusion without a chance to close substantive gaps.
    assert commands[1][commands[1].index('--tools') + 1] == 'Bash,Read'
    assert commands[2][commands[2].index('--tools') + 1] == ''


def test_outer_retry_cannot_reset_durable_phase_budget(tmp_path, monkeypatch):
    from scripts import scholar_1027_cli
    monkeypatch.setattr(scholar_1027_cli, 'prior_session', lambda *a: None)
    commands = mock_cli(monkeypatch, [(1, {'is_error': True, 'subtype': 'error_max_turns'})] * 5)
    e = engine(tmp_path)
    for _ in range(2):
        install(e, tmp_path, runner.save, runner.load)
        out = asyncio.run(e._run_claude_prompt('x', 'review', 'prompt', 80))
        assert out['trigger'] == 'review_error' and out['_retry_exhausted']
    # The second invocation resumes the exhausted checkpoint with focused
    # recovery and finalization; it must not restart the 80-turn research pass.
    assert len(commands) == 5
    assert [c[c.index('--max-turns') + 1] for c in commands] == ['80', '12', '6', '12', '6']


def test_resume_successful_phase_does_not_rerun_tools(tmp_path, monkeypatch):
    e = engine(tmp_path)
    forbidden = AsyncMock(side_effect=AssertionError('already completed'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'p', 80,
        state={'completed_result': {'doi': 'x', 'result': '低风险'}}))
    assert out['result'] == '低风险'


def test_validation_gap_after_finalization_gets_one_focused_repair(tmp_path, monkeypatch):
    calls = mock_cli(monkeypatch, [
        (0, {'result': json.dumps({'doi': 'x', 'result': '高风险'})}),
    ])
    checkpoints = []
    out = asyncio.run(review_cli.run(
        engine(tmp_path), 'x', 'review', 'prompt', 36,
        state={
            'phase': 3,
            'session_id': 'saved',
            'finalization_version': 2,
            'output_gaps': ['未对HIGH发现给出结论: data/rowwise_value_reuse'],
        },
        persist=lambda value: checkpoints.append(dict(value)),
    ))
    assert out['result'] == '高风险'
    assert calls[0][calls[0].index('--max-turns') + 1] == '12'
    assert checkpoints[0]['post_validation_recovery_attempted']


def test_interrupted_finalization_resumes_output_only_phase(tmp_path, monkeypatch):
    calls = mock_cli(monkeypatch, [
        (0, {'result': json.dumps({'doi': 'x', 'result': '低风险'})}),
    ])
    out = asyncio.run(review_cli.run(
        engine(tmp_path), 'x', 'review', 'prompt', 36,
        state={
            'phase': 3,
            'session_id': 'saved',
            'finalization_version': 2,
            'last_error': 'error_max_turns',
            'phase_metrics': [
                {'phase': 0, 'subtype': 'error_max_turns'},
                {'phase': 1, 'subtype': 'error_max_turns'},
            ],
        },
    ))
    assert out['result'] == '低风险'
    assert len(calls) == 1
    assert calls[0][calls[0].index('--max-turns') + 1] == '6'
    assert calls[0][calls[0].index('--tools') + 1] == ''


def test_completed_finalization_error_reopens_focused_recovery(tmp_path, monkeypatch):
    calls = mock_cli(monkeypatch, [
        (0, {'result': json.dumps({'doi': 'x', 'result': '高风险'})}),
    ])
    checkpoints = []
    out = asyncio.run(review_cli.run(
        engine(tmp_path), 'x', 'review', 'prompt', 36,
        state={
            'phase': 3,
            'session_id': 'saved',
            'finalization_version': 2,
            'last_error': '上次未完成：主图逐图核查尚未完成',
            'phase_metrics': [
                {'phase': 0, 'subtype': 'error_max_turns'},
                {'phase': 1, 'subtype': 'error_max_turns'},
                {'phase': 2, 'subtype': 'success'},
            ],
        },
        persist=lambda value: checkpoints.append(dict(value)),
    ))
    assert out['result'] == '高风险'
    assert len(calls) == 1
    assert calls[0][calls[0].index('--max-turns') + 1] == '12'
    assert calls[0][calls[0].index('--tools') + 1] == 'Bash,Read'
    assert checkpoints[0]['continuation_attempts'] == 1


def test_phases_use_shared_deadline_with_bounded_investigation(tmp_path, monkeypatch):
    e = engine(tmp_path)
    commands = mock_cli(monkeypatch, [(0, {})] * 3)
    async def forbidden(coro, timeout):
        coro.close()
        pytest.fail('phase must use the whole-paper deadline')
    monkeypatch.setattr(asyncio, 'wait_for', forbidden)
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'p', 80))
    assert len(commands) == 3
    e._kill_and_reap_process.assert_not_awaited()
    assert out['_retry_exhausted']


def test_cancellation_reaps_and_does_not_start_recovery(tmp_path, monkeypatch):
    e = engine(tmp_path)
    async def scenario():
        started = asyncio.Event()
        async def blocked():
            started.set()
            await asyncio.Event().wait()
        spawn = AsyncMock(return_value=SimpleNamespace(communicate=blocked))
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        task = asyncio.create_task(review_cli.run(e, 'x', 'review', 'p', 80))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spawn.await_count == e._kill_and_reap_process.await_count == 1
    asyncio.run(scenario())


def test_process_error_and_wrong_doi_are_never_accepted(tmp_path, monkeypatch):
    e = engine(tmp_path)
    mock_cli(monkeypatch, [
        (0, {'result': json.dumps({'doi': 'other', 'result': '低风险'})}),
        (0, {'result': json.dumps({'doi': 'x', 'result': '高风险', 'trigger': 'review_error'})}),
        (0, {'result': json.dumps({'doi': 'x', 'trigger': 'review_error', '_review_error': 'unread Fig.4'})}),
    ])
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'p', 80))
    assert out['trigger'] == 'review_error'
    assert 'unread Fig.4' in out['_review_error']


def test_api_outer_retry_does_not_repeat_exhausted_transport(monkeypatch):
    call = AsyncMock(return_value={'trigger': 'review_error', '_retry_exhausted': True})
    monkeypatch.setattr(review, '_run_single_claude', call)
    asyncio.run(review._run_with_retry('x', '', '', '', 1))
    assert call.await_count == 1
    monkeypatch.setattr(review, '_run_lowrisk_verifier_claude', call)
    asyncio.run(review._run_lowrisk_verifier_with_retry('x', '', '', '', {}))
    assert call.await_count == 2


@pytest.mark.parametrize('label, expected', [('low_risk', '低风险'), ('high_risk', '高风险')])
def test_english_risk_labels_are_accepted_without_filling_evidence(tmp_path, monkeypatch, label, expected):
    e = engine(tmp_path)
    commands = mock_cli(monkeypatch, [(0, {'subtype': 'success', 'result': json.dumps({
        'doi': 'x', 'result': label, 'data_review': 'retained facts'})})])
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'prompt', 80))
    assert len(commands) == 1 and out['result'] == expected
    assert out['data_review'] == 'retained facts'
    assert 'dimension_verdicts' not in out


def test_successful_cli_without_valid_result_has_useful_error(tmp_path, monkeypatch):
    e = engine(tmp_path)
    mock_cli(monkeypatch, [(0, {'subtype': 'success', 'result': '{}'})] * 3)
    out = asyncio.run(review_cli.run(e, 'x', 'review', 'prompt', 80))
    assert 'no valid review risk label' in out['_review_error']


def test_supervisor_exhausted_jobs_stop_instead_of_polling_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'ROUND', tmp_path)
    monkeypatch.setattr(runner, 'scope', lambda: [{'key': '戴大伟/x', 'scholar': '戴大伟', 'doi': 'x'}])
    monkeypatch.setattr(runner, 'rows', lambda: {})
    runner.save(tmp_path / 'attempts.json', {'戴大伟/x': 3})
    runner.save(tmp_path / 'papers/戴大伟/x/status.json', {'state': 'retry', 'error': 'turn limit'})
    monkeypatch.setattr(runner.time, 'sleep', lambda *_: pytest.fail('must stop polling'))
    assert runner.supervise(1) == 1
    state = runner.load(tmp_path / 'status.json')
    assert state['settled'] and not state['finished']
    assert runner.load(tmp_path / 'papers/戴大伟/x/status.json')['state'] == 'needs_recovery'


def test_worker_deadline_does_not_refund_retry_budget(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(runner, 'ROUND', tmp_path)
    monkeypatch.setattr(runner, 'scope', lambda: [{'key': '戴大伟/x', 'scholar': '戴大伟', 'doi': 'x'}])
    monkeypatch.setattr(runner, 'rows', lambda: {})
    runner.save(tmp_path / 'attempts.json', {'戴大伟/x': 3})
    runner.save(tmp_path / 'papers/戴大伟/x/status.json', {'state': 'running', 'pid': 123})
    class Process:
        def __init__(self, pid): self.pid = pid
        def cmdline(self): return [str(runner.Path(runner.__file__).resolve()), 'worker', '戴大伟/x']
        def create_time(self): return 1
        def is_running(self): return True
        def status(self): return 'sleeping'
    monkeypatch.setattr(psutil, 'Process', Process)
    killed = []
    monkeypatch.setattr(runner, 'stop_worker_tree', lambda p: killed.append(p.pid))
    monkeypatch.setattr(runner.time, 'sleep', lambda *_: pytest.fail('must stop polling'))
    assert runner.supervise(1) == 1
    assert killed == [123]
    assert runner.load(tmp_path / 'attempts.json')['戴大伟/x'] == 3
    assert 'deadline' in runner.load(tmp_path / 'papers/戴大伟/x/status.json')['error']


def test_synthesis_repair_preserves_latest_substantive_candidate(tmp_path, monkeypatch):
    bundle = tmp_path / 'evidence.json'
    runner.save(bundle, {'coverage_manifest': {}})
    monkeypatch.setattr(review, '_ensure_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(review, '_ensure_llm_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(review, '_validate_review_result', lambda d, r, i, value, e: dict(value))
    first = {'doi': 'x', 'result': '低风险'}
    runner.save(tmp_path / 'reviewer1.json', first)
    runner.save(tmp_path / 'reviewer2.json', first)
    new = {'doi': 'x', 'result': '高风险', '_coverage_gaps': ['missing source image'],
           'data_review': 'new substantive evidence'}
    error = {'trigger': 'review_error', '_retry_exhausted': True}
    judge = AsyncMock(side_effect=[new, error])
    monkeypatch.setattr(review, '_run_judge', judge)
    with pytest.raises(RuntimeError, match='incomplete'):
        asyncio.run(runner.review({'doi': 'x', 'source': str(tmp_path)}, tmp_path))
    assert judge.await_args_list[1].args[4]['data_review'] == 'new substantive evidence'
    assert judge.await_args_list[1].kwargs['coverage_gaps'] == ['missing source image']
    assert not (tmp_path / 'results.json').exists()


def test_consensus_after_transport_failure_still_requires_real_coverage(tmp_path, monkeypatch):
    bundle = tmp_path / 'evidence.json'
    runner.save(bundle, {'coverage_manifest': {}})
    monkeypatch.setattr(review, '_ensure_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(review, '_ensure_llm_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(review, '_validate_review_result',
        lambda d, r, i, value, e: dict(value, _coverage_gaps=['unread source data']))
    for name in ('reviewer1.json', 'reviewer2.json'):
        runner.save(tmp_path / name, {'doi': 'x', 'result': '低风险'})
    monkeypatch.setattr(review, '_run_judge', AsyncMock(return_value={
        'trigger': 'review_error', '_retry_exhausted': True}))
    with pytest.raises(RuntimeError, match='incomplete'):
        asyncio.run(runner.review({'doi': 'x', 'source': str(tmp_path)}, tmp_path))
    assert not (tmp_path / 'results.json').exists()


def test_finalizer_continuation_is_granted_only_once(tmp_path, monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError('continuation budget already used'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    out = asyncio.run(review_cli.run(engine(tmp_path), 'x', 'review', 'prompt', 36, state={
        'phase': 3, 'session_id': 'saved', 'finalization_version': 2,
        'continuation_attempts': 1, 'last_error': '上次未完成：主图逐图核查尚未完成',
        'phase_metrics': [{'phase': 1, 'subtype': 'error_max_turns'},
                          {'phase': 2, 'subtype': 'success'}],
    }))
    assert out['trigger'] == 'review_error'
