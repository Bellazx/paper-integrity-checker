"""Regressions reproduced from the Oct 28/29 long-running review jobs."""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from api.services import review, review_cli, review_runtime
from tests.test_review_cli_convergence import engine, mock_cli
from tests.test_review_failure_recovery import result


def saved_draft(tmp_path, info=None, evidence=None):
    bundle = tmp_path / 'full evidence.json'
    bundle.write_text(json.dumps(evidence or {'high_findings': []}))
    prompt = review._publication_preflight_instruction('10.1/test', review.EvidencePaths('', str(bundle)))
    paths = review_cli.preflight_paths(prompt)
    from pathlib import Path
    Path(paths['result']).write_text(json.dumps(info or result()))
    return prompt, paths


def test_saved_valid_draft_returns_without_another_model_call(tmp_path, monkeypatch):
    prompt, _ = saved_draft(tmp_path)
    calls = mock_cli(monkeypatch, [(1, {'is_error': True, 'subtype': 'error_max_turns'})])
    checkpoints = []
    out = asyncio.run(review_cli.run(engine(tmp_path), '10.1/test', 'review', prompt, 80,
                                    persist=lambda s: checkpoints.append(dict(s))))
    assert len(calls) == 0
    assert out['data_review'] == result()['data_review']
    assert out['_coverage_status'] == 'ok'
    assert checkpoints[-1]['result_source'] == 'validated_draft'
    assert 'phase_metrics' not in checkpoints[-1]


def test_resumed_draft_uses_original_path_not_new_random_filename(tmp_path, monkeypatch):
    prompt, paths = saved_draft(tmp_path)
    replacement = review._publication_preflight_instruction('10.1/test',
                                                           review.EvidencePaths('', paths['evidence']))
    calls = mock_cli(monkeypatch, [])
    out = asyncio.run(review_cli.run(engine(tmp_path), '10.1/test', 'review', replacement, 80,
        state={'phase': 3, 'session_id': 'old', 'preflight': paths}))
    assert out['result'] == '高风险' and calls == []


@pytest.mark.parametrize('kind', ['wrong_doi', 'missing_finding', 'process_error', 'empty_evidence'])
def test_invalid_saved_drafts_cannot_bypass_publication_gate(tmp_path, monkeypatch, kind):
    info = result()
    evidence = {'high_findings': []}
    if kind == 'wrong_doi': info['doi'] = '10.1/other'
    if kind == 'process_error': info['trigger'] = 'review_error'
    if kind == 'missing_finding':
        evidence['high_findings'] = [{'dim': 'data', 'test': 'rowwise_value_reuse'}]
    prompt, paths = saved_draft(tmp_path, info, evidence)
    if kind == 'empty_evidence':
        from pathlib import Path
        Path(paths['evidence']).write_text('{}')
    mock_cli(monkeypatch, [(1, {'is_error': True, 'subtype': 'error_max_turns'})] * 3)
    out = asyncio.run(review_cli.run(engine(tmp_path), '10.1/test', 'review', prompt, 80))
    assert out['trigger'] == 'review_error'


def test_actual_endpoint_failure_switches_model_and_preserves_session(tmp_path, monkeypatch):
    calls = mock_cli(monkeypatch, [
        (1, {'is_error': True, 'subtype': 'error_during_execution', 'errors': ['API 503 overloaded']}),
        (0, {'result': json.dumps({'doi': 'x', 'result': '低风险'})}),
    ])
    out = asyncio.run(review_cli.run(engine(tmp_path), 'x', 'review', 'p', 80))
    assert out['result'] == '低风险'
    assert calls[1][calls[1].index('--model') + 1] == 'fallback'
    assert calls[1][calls[1].index('--resume') + 1] == calls[0][calls[0].index('--session-id') + 1]
    assert calls[0][calls[0].index('--tools') + 1] == 'Bash,Read'


def test_old_single_turn_finalize_gets_one_output_only_correction(tmp_path, monkeypatch):
    prompt, paths = saved_draft(tmp_path, {'doi': 'wrong'})
    state = {'phase': 3, 'session_id': 'old-finalizer', 'preflight': paths,
             'phase_metrics': [{'phase': 2, 'subtype': 'error_max_turns'}]}
    calls = mock_cli(monkeypatch, [(1, {'is_error': True, 'subtype': 'error_max_turns'})])
    checkpoints = []
    out = asyncio.run(review_cli.run(engine(tmp_path), '10.1/test', 'review', prompt, 80,
        state=state, persist=lambda s: checkpoints.append(dict(s))))
    assert out['trigger'] == 'review_error'
    assert len(calls) == 1
    assert calls[0][calls[0].index('--max-turns') + 1] == '6'
    assert calls[0][calls[0].index('--tools') + 1] == ''
    instruction = calls[0][calls[0].index('-p') + 1]
    assert paths['result'] not in instruction
    assert '不要保存或修改任何文件' in instruction
    # A restart must not refund the correction budget.
    asyncio.run(review_cli.run(engine(tmp_path), '10.1/test', 'review', prompt, 80,
                              state=checkpoints[-1]))
    assert len(calls) == 1


def test_evidence_budget_survives_to_thread_and_does_not_restart_on_fallback():
    async def scenario():
        token = review_runtime.paper_deadline.set(time.monotonic() + 10)
        try:
            bounded = await asyncio.to_thread(review_runtime.subprocess_budget, 6000)
            assert 0 < bounded <= 10
            review_runtime.paper_deadline.set(time.monotonic() - 1)
            with pytest.raises(TimeoutError):
                await asyncio.to_thread(review_runtime.subprocess_budget, 2700)
        finally:
            review_runtime.paper_deadline.reset(token)
    asyncio.run(scenario())


def test_failed_evidence_is_rebuilt_instead_of_reused(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(review, 'REVIEW_DIR', tmp_path)
    bad = tmp_path / 'cached_evidence.json'
    bad.write_text(json.dumps({'_fallback_reason': 'OpenBLAS failure', 'doi': 'x'}))
    monkeypatch.setattr(review, '_evidence_bundle_path', lambda *a: bad)
    monkeypatch.setattr(review, '_augment_evidence_bundle', lambda *a: None)
    calls = []
    def build(cmd, **kwargs):
        calls.append(kwargs)
        bad.write_text(json.dumps({'doi': 'x', 'high_findings': []}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(review.subprocess, 'run', build)
    assert review._ensure_evidence_bundle('x', 'report', 'input') == str(bad)
    assert len(calls) == 1
    assert calls[0]['env']['OPENBLAS_NUM_THREADS'] == '1'


def test_failed_inventory_does_not_launch_expensive_model_chain(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    source = tmp_path / 'input'; source.mkdir()
    report = tmp_path / 'report.json'; report.write_text('{}')
    evidence = tmp_path / 'evidence.json'
    evidence.write_text(json.dumps({'_fallback_reason': 'evidence subprocess failed'}))
    monkeypatch.setattr(review, '_ensure_evidence_bundle', lambda *a: str(evidence))
    forbidden = AsyncMock(side_effect=AssertionError('partial inventory must not start model chain'))
    monkeypatch.setattr(review, '_run_with_retry', forbidden)
    out = asyncio.run(review.run_review_single('x', str(report), str(source), str(tmp_path / 'out')))
    assert out['trigger'] == 'review_error' and out['_retry_exhausted']
    assert 'Evidence preparation incomplete' in out['_review_error']
    forbidden.assert_not_called()


def test_numeric_import_works_under_existing_2gb_memory_limit(monkeypatch):
    monkeypatch.setenv('OPENBLAS_NUM_THREADS', '128')
    monkeypatch.delenv('REVIEW_NUMERIC_THREADS', raising=False)
    env = review._claude_subprocess_env()
    assert env['OPENBLAS_NUM_THREADS'] == env['OMP_NUM_THREADS'] == '1'
    assert os.environ['OPENBLAS_NUM_THREADS'] == '128'
    # Real child process: reproduces the RLIMIT_AS + BLAS allocation interaction.
    code = ('import resource; resource.setrlimit(resource.RLIMIT_AS, (2**31, 2**31)); '
            'import numpy as np; import scipy.stats; '
            'assert np.dot(np.ones((100,100)), np.ones((100,100)))[0,0] == 100')
    run = subprocess.run([sys.executable, '-c', code], env=env,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


def test_evidence_builders_are_bounded_without_serializing_all_papers(monkeypatch):
    monkeypatch.setenv('REVIEW_EVIDENCE_CONCURRENCY', '2')
    active = peak = 0
    lock = threading.Lock()
    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock: active -= 1
        return 'complete'
    async def scenario():
        return await asyncio.gather(*(review_runtime.limited_evidence(work) for _ in range(6)))
    assert asyncio.run(scenario()) == ['complete'] * 6
    assert peak == 2


def test_judge_repairs_are_focused_and_use_dedicated_budget(monkeypatch):
    from unittest.mock import AsyncMock
    call = AsyncMock(return_value=result())
    monkeypatch.setattr(review, '_run_claude_prompt', call)
    asyncio.run(review._run_judge('10.1/test', '/tmp/report', '/tmp/input', '/tmp/out',
                                  result(), result(), coverage_gaps=['missing label']))
    assert call.await_args.args[1] == 'Coverage repair'
    assert call.await_args.args[3] == review.COVERAGE_REPAIR_MAX_TURNS
    assert '不要重做' in call.await_args.args[2]


def test_prompts_do_not_demand_impossible_source_checks_or_conflicting_verdict():
    prompt = review._build_prompt('x', '/tmp/report', '/tmp/input')
    assert 'verdict 字段只能是' in prompt
    assert '未提供数值型源数据时' in prompt
    assert '最多校验修复两次' not in prompt  # no evidence path, no preflight command
    assert '每个图 vs 每个图' not in prompt
