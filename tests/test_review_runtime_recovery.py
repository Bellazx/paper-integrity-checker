import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from api.services import review, review_cli
from api.services.review_lock import exclusive_review
from api.services.review_validation import validate
from tests.test_review_cli_convergence import engine, mock_cli
from tests.test_review_failure_recovery import result


@pytest.fixture
def paper(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.mkdir()
    report = tmp_path / 'report.json'; report.write_text('{}')
    output = tmp_path / 'output'; output.mkdir()
    evidence = tmp_path / 'evidence.json'; evidence.write_text('{"high_findings": []}')
    args = ('10.1/test', str(report), str(source), str(output))
    state_path = review._review_state_path(*args[:3], review._review_artifacts_dir(str(output)))
    state = review._new_review_state(*args[:3])
    state['stages'] = {'evidence_path': str(evidence), 'llm_evidence_path': str(evidence)}
    review._save_review_state(state_path, state)
    monkeypatch.setattr(review, '_validate_review_result', lambda *a: a[3])
    monkeypatch.setattr(review, '_source_data_audit_highrisk_review_result', lambda *a: None)
    monkeypatch.setattr(review, '_prior_review_result_for_doi', lambda *a: None)
    return args, state_path, state


@pytest.mark.parametrize('failed_stage', ['first', 'second', 'judge'])
def test_process_failure_does_not_cascade_into_more_reviews(paper, monkeypatch, failed_stage):
    args, state_path, state = paper
    error = review._fallback_result(args[0], 'timeout')
    error['_retry_exhausted'] = True
    first = result('低风险')
    run = AsyncMock(return_value=error if failed_stage == 'first' else first)
    second = AsyncMock(return_value=error if failed_stage == 'second' else first)
    judge = AsyncMock(return_value=error)
    monkeypatch.setattr(review, '_run_with_retry', run)
    monkeypatch.setattr(review, '_run_lowrisk_verifier_with_retry', second)
    monkeypatch.setattr(review, '_run_judge', judge)
    monkeypatch.setattr(review, '_accept_lowrisk_consensus', lambda *a: False)
    out = asyncio.run(review.run_review_single(*args, resume=True))
    if failed_stage == 'first':
        assert out['trigger'] == 'review_error'
    else:
        assert out['trigger'] == 'review'
        assert out['result'] == '低风险'
    assert run.await_count == 1
    assert second.await_count == 0
    assert judge.await_count == 0
    final = json.loads(state_path.read_text())['stages']['final']
    if failed_stage == 'first':
        assert final['_review_error'] == 'timeout'
    else:
        assert final['result'] == '低风险'


def test_preserved_source_survives_failed_repair_and_is_resumed(paper, monkeypatch):
    args, state_path, state = paper
    candidate = result(); candidate['reason'] = 'latest substantive facts'
    state['stages'].update(coverage_repair_source=candidate,
                           coverage_repair=review._fallback_result(args[0], 'timeout'),
                           judge=review._fallback_result(args[0], 'timeout'))
    review._save_review_state(state_path, state)
    forbidden = AsyncMock(side_effect=AssertionError('must reuse preserved source'))
    monkeypatch.setattr(review, '_run_with_retry', forbidden)
    out = asyncio.run(review.run_review_single(*args, resume=True))
    assert out['reason'] == candidate['reason']
    assert not forbidden.called


def test_new_repair_draft_wins_over_stale_validated_candidate(paper, monkeypatch):
    args, path, state = paper
    state['stages']['coverage_repair_validated'] = result('低风险')
    new = result('高风险')
    review._store_stage(path, state, 'coverage_repair', new)
    review._store_stage(path, state, 'coverage_repair', review._fallback_result(args[0], 'timeout'))
    out = asyncio.run(review.run_review_single(*args, resume=True))
    assert out['result'] == '高风险'


def test_process_lock_prevents_duplicate_and_releases_after_error(tmp_path):
    lock = tmp_path / 'batch.lock'
    command = [sys.executable, '-c',
               'from pathlib import Path; from api.services.review_lock import exclusive_review; '
               f'\nwith exclusive_review(Path({str(lock)!r})): pass']
    with pytest.raises(ValueError):
        with exclusive_review(lock):
            assert subprocess.run(command, capture_output=True).returncode != 0
            raise ValueError('failed batch')
    assert subprocess.run(command, capture_output=True).returncode == 0


def test_old_stage_timeout_setting_is_ignored(tmp_path, monkeypatch):
    e = engine(tmp_path)
    e._review_subprocess_timeout_seconds = lambda: pytest.fail('legacy stage timer read')
    mock_cli(monkeypatch, [(0, {})] * 3)
    checkpoints = []
    asyncio.run(review_cli.run(e, 'x', 'review', 'prompt', 80,
        state={'timeout_seconds': 1800}, persist=checkpoints.append))
    assert checkpoints and all(s['timeout_seconds'] is None for s in checkpoints)


def test_checkpoint_separates_reviewers_and_survives_success(tmp_path, monkeypatch):
    evidence = tmp_path / 'evidence.json'; evidence.write_text('{}')
    calls = []
    async def transport(*a, state=None, persist=None, **kw):
        calls.append(state)
        saved = state or {'doi': 'x', 'label': a[2], 'completed_result': {'doi': 'x', 'result': '低风险'}}
        persist(saved)
        return saved['completed_result']
    monkeypatch.setattr(review_cli, 'run', transport)
    async def scenario():
        token = review._REVIEW_CLI_CONTEXT.set({'run_id': 'one', 'scope': 'paper'})
        try:
            for label in ('Review pass 1', 'Review pass 2', 'Review pass 1'):
                await review._run_claude_prompt('x', label, 'same prompt', 80, str(evidence))
        finally:
            review._REVIEW_CLI_CONTEXT.reset(token)
    asyncio.run(scenario())
    assert calls[0] is None and calls[1] is None
    assert calls[2]['completed_result']['result'] == '低风险'
    assert len(list(tmp_path.glob('cli_checkpoint*.json'))) == 2


def test_paper_deadline_cancels_work_and_resets_context(paper, monkeypatch):
    args, path, state = paper
    cancelled = []
    async def blocked(**kw):
        review._REVIEW_CLI_CONTEXT.set({'run_id': 'inner', 'scope': str(path)})
        try: await asyncio.Event().wait()
        finally: cancelled.append(True)
    monkeypatch.setattr(review, '_run_review_single_impl', blocked)
    monkeypatch.setenv('REVIEW_PAPER_TIMEOUT_SECONDS', '1')
    out = asyncio.run(review.run_review_single(*args, resume=True))
    assert cancelled == [True]
    assert out['trigger'] == 'review_error' and out['_retry_exhausted']
    assert 'Whole-paper deadline' in out['_review_error']
    assert review._REVIEW_CLI_CONTEXT.get() is None
    with exclusive_review(path.with_suffix('.lock')): pass


@pytest.mark.parametrize('phase', [0, 1, 2])
def test_whole_paper_deadline_reaps_cli_in_any_phase(paper, monkeypatch, phase):
    from types import SimpleNamespace
    args, path, state = paper
    e = engine(path.parent)
    async def blocked():
        await asyncio.Event().wait()
    spawn = AsyncMock(return_value=SimpleNamespace(communicate=blocked))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    async def run_cli(**kw):
        return await review_cli.run(e, args[0], 'review', 'prompt', 80,
                                    state={'phase': phase, 'session_id': 'saved'})
    monkeypatch.setattr(review, '_run_review_single_impl', run_cli)
    monkeypatch.setenv('REVIEW_PAPER_TIMEOUT_SECONDS', '1')
    out = asyncio.run(review.run_review_single(*args, resume=True))
    assert out['trigger'] == 'review_error'
    assert 'Whole-paper deadline' in out['_review_error']
    assert spawn.await_count == e._kill_and_reap_process.await_count == 1
    assert json.loads(path.read_text())['stages']['final']['trigger'] == 'review_error'


@pytest.mark.parametrize(('setting', 'expected'), [
    (None, 5400), ('90', 90), ('0', None), ('none', None), ('unlimited', None),
])
def test_paper_deadline_configuration(paper, monkeypatch, setting, expected):
    args, path, state = paper
    observed = []
    real_wait_for = asyncio.wait_for
    async def capture(awaitable, timeout):
        observed.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)
    monkeypatch.setattr(asyncio, 'wait_for', capture)
    monkeypatch.setattr(review, '_run_review_single_impl', AsyncMock(return_value=result('低风险')))
    if setting is None:
        monkeypatch.delenv('REVIEW_PAPER_TIMEOUT_SECONDS', raising=False)
    else:
        monkeypatch.setenv('REVIEW_PAPER_TIMEOUT_SECONDS', setting)
    out = asyncio.run(review.run_review_single(*args, resume=True))
    assert observed == [expected]
    assert out['result'] == '低风险'


def test_equivalent_dimension_labels_are_not_missing_but_missing_stays_missing():
    info = result()
    info['dimension_verdicts'] = {'image': '未发现风险', 'data': '高风险', 'ref': '低风险'}
    checked, gaps = validate(info, {'high_findings': []})
    assert checked['dimension_verdicts']['data'] == '建议高风险'
    assert [g for g in gaps if '维度缺少明确风险结论' in g] == ['methodology维度缺少明确风险结论']
    assert info['dimension_verdicts']['data'] == '高风险'


def test_opaque_figure_names_accept_english_references_but_not_empty_claims():
    from modules.review_coverage import validate as coverage
    info = result('低风险')
    info['data_review'] = '已渲染 Figure 1 与 Fig.2，核对柱高与正文数值一致。' * 20
    evidence = {'high_findings': [], 'main_figures': ['下载.png', '下载 (1).png']}
    _, gaps = coverage(info, evidence)
    assert not any('引用主图' in g for g in gaps)
    info['data_review'] = '未发现异常。'
    info['image_review'] = '未发现异常。'
    _, gaps = coverage(info, evidence)
    assert any('引用主图' in g for g in gaps)


def test_batch_passes_explicit_resume_and_persists_failures(tmp_path, monkeypatch):
    from scripts import run_scholar_batch as batch
    call = AsyncMock(return_value={'doi': 'x', 'trigger': 'review_error', '_review_error': 'timeout'})
    monkeypatch.setattr(batch, 'run_review_single', call)
    target = tmp_path / 'results.json'
    asyncio.run(batch.run_reviews([{'status': 'success', 'doi': 'x', 'report_json': 'r', 'input_dir': 'i'}],
                                 tmp_path, 1, result_path=target, resume=True))
    assert call.call_args.kwargs['resume'] is True
    assert json.loads(target.read_text())[0]['_review_error'] == 'timeout'


def test_publication_failure_does_not_block_other_papers(tmp_path, monkeypatch):
    from scripts import review_phillip_pending as runner
    initial = tmp_path / 'initial.json'
    initial.write_text(json.dumps([{'doi': 'x'}, {'doi': 'y'}]))
    monkeypatch.setattr(runner, 'INITIAL', initial)
    monkeypatch.setattr(runner, 'OUTPUT', tmp_path)
    monkeypatch.setattr(runner, 'RESULTS', tmp_path / 'pending.json')
    monkeypatch.setattr(runner, 'PENDING', {'x', 'y'})
    run = AsyncMock(return_value=[{'doi': 'x', 'result': '低风险'}, {'doi': 'y', 'result': '低风险'}])
    monkeypatch.setattr(runner, 'run_reviews', run)
    rendered = []
    def render(items, *a, **kw):
        rendered.append(items[0]['doi'])
        if len(rendered) == 1: raise RuntimeError('renderer failed')
        return 0
    monkeypatch.setattr(runner, 'render_and_update_reviews', render)
    assert runner.main() == 2
    assert rendered == ['x', 'y']
    assert run.call_args.kwargs['resume'] is True


def test_fresh_review_does_not_resurrect_previous_synthesis_after_failure(paper, monkeypatch):
    args, path, state = paper
    state['stages']['synthesis_candidate'] = result()
    review._save_review_state(path, state)
    failure = review._fallback_result(args[0], 'new attempt incomplete')
    run = AsyncMock(return_value=failure)
    monkeypatch.setattr(review, '_run_with_retry', run)
    for resume in (False, True):
        out = asyncio.run(review.run_review_single(*args, resume=resume))
        assert out['trigger'] == 'review_error'
    assert 'synthesis_candidate' not in json.loads(path.read_text())['stages']
    assert list(path.parent.glob('*.history.json'))
