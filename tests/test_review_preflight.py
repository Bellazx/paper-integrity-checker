import asyncio
import json
import re
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from api.services import review
from api.services.review_validation import validate
from tests.test_review_failure_recovery import result


@pytest.mark.parametrize('change', ['missing_result', 'process_error', 'verdict', 'empty_evidence'])
def test_publication_gate_rejects_same_invalid_inputs_as_preflight(change):
    info = result()
    evidence = {'high_findings': []}
    if change == 'missing_result':
        info.pop('result')
    elif change == 'process_error':
        info['trigger'] = 'review_error'
    elif change == 'verdict':
        info['verdict'] = '建议低风险'
    else:
        evidence = {}
    _, gaps = validate(info, evidence)
    assert gaps


def test_revalidation_replaces_old_failure_metadata_without_changing_facts():
    info = {**result(), '_coverage_error': 'old unavailable evidence',
            '_coverage_status': 'error', '_coverage_gaps': ['old missing detail']}
    checked, gaps = validate(info, {'high_findings': []})
    assert not gaps and checked['_coverage_status'] == 'ok'
    assert '_coverage_error' not in checked
    assert checked['data_review'] == info['data_review']
    assert info['_coverage_error'] == 'old unavailable evidence'
    checked, gaps = validate(info, {'high_findings': [{'dim': 'data', 'test': 'rowwise_value_reuse'}]})
    assert any('rowwise_value_reuse' in gap for gap in gaps)

def test_preflight_rejects_missing_findings_and_accepts_complete_review(tmp_path):
    draft = tmp_path / 'draft.json'
    bundle = tmp_path / 'evidence.json'
    evidence = {'deterministic_findings': {'decimal_precision_mismatch': [{}]},
                'high_findings': []}
    bundle.write_text(json.dumps(evidence))
    info = result()
    draft.write_text(json.dumps(info))
    command = [sys.executable, 'scripts/validate_review_result.py', '--result', str(draft),
               '--evidence', str(bundle), '--doi', info['doi']]
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode == 1
    assert '小数精度' in json.loads(failed.stdout)['gaps'][0]
    info['data_review'] += '小数精度不一致：Fig.2 对应不同量纲的计数和比例，核查为假阳性。'
    draft.write_text(json.dumps(info))
    before = draft.read_bytes(), bundle.read_bytes()
    passed = subprocess.run(command, capture_output=True, text=True)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert json.loads(passed.stdout) == {'ok': True, 'gaps': []}
    assert before == (draft.read_bytes(), bundle.read_bytes())
    mismatch = subprocess.run(command[:-1] + ['10.1/another'], capture_output=True, text=True)
    assert mismatch.returncode == 1
    info.pop('result')
    draft.write_text(json.dumps(info))
    assert subprocess.run(command, capture_output=True).returncode == 1


def test_every_review_stage_receives_full_evidence_preflight():
    ev = review.EvidencePaths('/tmp/summary.json', '/tmp/full evidence.json')
    info = result()
    prompts = [review._build_prompt('10.1/test', '/tmp/report', '/tmp/input', ev),
               review._build_lowrisk_verifier_prompt('10.1/test', '/tmp/report', '/tmp/input', info, ev),
               review._build_judge_prompt('10.1/test', '/tmp/report', '/tmp/input', info, info, ev)]
    for prompt in prompts:
        assert 'scripts/validate_review_result.py' in prompt
        assert "--evidence '/tmp/full evidence.json'" in prompt
        assert '--doi 10.1/test' in prompt
        assert '不要修改校验程序' in prompt
    drafts = [re.search(r'--result (/tmp/review_draft_[a-f0-9]+\.json)', prompt).group(1)
              for prompt in prompts]
    assert len(set(drafts)) == 3


def test_repair_preserves_evidence_at_end_of_long_review():
    info = result()
    info['data_review'] = '已核查。' * review.MAX_PROMPT_FIELD_CHARS + '末尾独立通道关键证据'
    prompt = review._build_judge_prompt('10.1/test', '/tmp/report', '/tmp/input', info, result(),
                                      coverage_gaps=['缺少小数精度核查'])
    assert '末尾独立通道关键证据' in prompt


def test_resume_repairs_latest_incomplete_checkpoint_not_earlier_judge(monkeypatch, tmp_path):
    inputs = tmp_path / 'input'; inputs.mkdir()
    outputs = tmp_path / 'output'; outputs.mkdir()
    report = outputs / 'report.json'; report.write_text('{}')
    evidence = tmp_path / 'evidence.json'; evidence.write_text('{}')
    artifacts = review._review_artifacts_dir(str(outputs))
    state_path = review._review_state_path('10.1/test', str(report), str(inputs), artifacts)
    state = review._new_review_state('10.1/test', str(report), str(inputs))
    early = {**result(), 'trigger': 'early judge'}
    latest = {**result(), 'trigger': 'latest incomplete repair'}
    good = {**result(), 'trigger': 'fixed final'}
    state['stages'] = {'evidence_path': str(evidence), 'llm_evidence_path': str(evidence),
                       'reviewer1': early, 'reviewer2': early, 'judge': early,
                       'coverage_repair': latest}
    review._save_review_state(state_path, state)
    def check(*args):
        value = args[3]
        return {**value, '_coverage_gaps': [] if value['trigger'] == 'fixed final' else ['missing fact']}
    monkeypatch.setattr(review, '_validate_review_result', check)
    monkeypatch.setattr(review, '_source_data_audit_highrisk_review_result', lambda *args: None)
    run = AsyncMock(return_value=good)
    monkeypatch.setattr(review, '_run_judge', run)
    reviewer = AsyncMock(side_effect=AssertionError('resume must not restart reviewer passes'))
    monkeypatch.setattr(review, '_run_with_retry', reviewer)
    monkeypatch.setattr(review, '_run_lowrisk_verifier_with_retry', reviewer)
    recovered = asyncio.run(review.run_review_single('10.1/test', str(report), str(inputs), str(outputs), resume=True))
    assert recovered['trigger'] == 'fixed final'
    assert run.await_count == 1
    assert run.call_args.args[4]['trigger'] == 'latest incomplete repair'
    assert run.call_args.kwargs['coverage_gaps'] == ['missing fact']


def test_resume_repair_failure_does_not_restart_reviewer_passes(monkeypatch, tmp_path):
    inputs = tmp_path / 'input'; inputs.mkdir()
    outputs = tmp_path / 'output'; outputs.mkdir()
    report = outputs / 'report.json'; report.write_text('{}')
    evidence = tmp_path / 'evidence.json'; evidence.write_text('{}')
    artifacts = review._review_artifacts_dir(str(outputs))
    state_path = review._review_state_path('10.1/failure', str(report), str(inputs), artifacts)
    state = review._new_review_state('10.1/failure', str(report), str(inputs))
    latest = {**result(), 'trigger': 'latest incomplete repair'}
    state['stages'] = {
        'evidence_path': str(evidence), 'llm_evidence_path': str(evidence),
        'reviewer1': {**result(), 'trigger': 'review_error'},
        'reviewer2': {**result(), 'trigger': 'review_error'},
        'judge': latest,
    }
    review._save_review_state(state_path, state)

    def check(*args):
        value = args[3]
        return {**value, '_coverage_gaps': ['missing fact']}

    monkeypatch.setattr(review, '_validate_review_result', check)
    monkeypatch.setattr(review, '_source_data_audit_highrisk_review_result', lambda *args: None)
    monkeypatch.setattr(review, '_run_judge', AsyncMock(return_value={
        **result(), 'trigger': 'review_error', '_review_error': 'timeout',
    }))
    reviewer = AsyncMock(side_effect=AssertionError('review passes must not restart'))
    monkeypatch.setattr(review, '_run_with_retry', reviewer)
    monkeypatch.setattr(review, '_run_lowrisk_verifier_with_retry', reviewer)

    recovered = asyncio.run(review.run_review_single(
        '10.1/failure', str(report), str(inputs), str(outputs), resume=True
    ))
    assert recovered['trigger'] == 'review_error'
    assert recovered['result'] == latest['result']
    assert 'timeout' in recovered['_review_error']
    saved = review._load_review_state(state_path, '10.1/failure', str(report), str(inputs))
    assert saved['stages']['coverage_repair_source']['trigger'] == latest['trigger']


def test_resume_ignores_failed_newer_repair_and_uses_substantive_judge(monkeypatch, tmp_path):
    inputs = tmp_path / 'input'; inputs.mkdir()
    outputs = tmp_path / 'output'; outputs.mkdir()
    report = outputs / 'report.json'; report.write_text('{}')
    evidence = tmp_path / 'evidence.json'; evidence.write_text('{}')
    artifacts = review._review_artifacts_dir(str(outputs))
    state_path = review._review_state_path('10.1/older', str(report), str(inputs), artifacts)
    state = review._new_review_state('10.1/older', str(report), str(inputs))
    judge = {**result(), 'trigger': 'substantive judge'}
    state['stages'] = {
        'evidence_path': str(evidence), 'llm_evidence_path': str(evidence),
        'judge': judge,
        'coverage_repair': {**result(), 'trigger': 'review_error'},
    }
    review._save_review_state(state_path, state)
    monkeypatch.setattr(review, '_source_data_audit_highrisk_review_result', lambda *args: None)
    monkeypatch.setattr(review, '_validate_review_result', lambda *args: args[3])
    reviewer = AsyncMock(side_effect=AssertionError('failed repair must not restart reviewers'))
    monkeypatch.setattr(review, '_run_with_retry', reviewer)
    monkeypatch.setattr(review, '_run_lowrisk_verifier_with_retry', reviewer)
    recovered = asyncio.run(review.run_review_single(
        '10.1/older', str(report), str(inputs), str(outputs), resume=True
    ))
    assert recovered['trigger'] == judge['trigger']
    assert reviewer.await_count == 0
