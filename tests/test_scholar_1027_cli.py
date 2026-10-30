import asyncio
import json
from types import SimpleNamespace

from scripts.scholar_1027_cli import install
from scripts import scholar_1027_cli
from scripts.run_scholar_1027_review import load, save


def test_cli_resumes_turn_limit_and_ignores_nonfatal_model_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(scholar_1027_cli, 'prior_session', lambda *args: None)
    commands = []
    responses = [
        (1, {'is_error': True, 'subtype': 'error_max_turns'}),
        (0, {'result': json.dumps({'doi': '10.test/a', 'result': '低风险'})}),
    ]

    async def spawn(*command, **kwargs):
        commands.append(command)
        rc, payload = responses.pop(0)

        async def communicate():
            return json.dumps(payload).encode(), b'[claude-code:unrecognized_model]'

        return SimpleNamespace(returncode=rc, communicate=communicate)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    engine = SimpleNamespace(
        BASE_DIR=tmp_path, REVIEW_MODEL='test-model', JUDGE_MAX_TURNS=80,
        _find_claude_cli=lambda: 'claude', _claude_subprocess_env=lambda: {},
        _review_subprocess_timeout_seconds=lambda: 30,
        _extract_json_from_text=json.loads, _normalize_review_result=dict,
        _clean_internal_references=dict,
        _fallback_result=lambda doi, error: {'doi': doi, 'trigger': 'review_error'},
        _build_judge_prompt=lambda *a, **k: 'synthesize',
    )
    install(engine, tmp_path, save, load)
    result = asyncio.run(engine._run_claude_prompt('10.test/a', 'first', 'review', 80))
    assert result['result'] == '低风险'
    original_session = commands[0][commands[0].index('--session-id') + 1]
    assert commands[1][commands[1].index('--resume') + 1] == original_session
    assert len(list((tmp_path / 'cli_sessions').glob('*_response.json'))) == 2


def test_judge_failure_never_uses_candidate_consensus(tmp_path, monkeypatch):
    monkeypatch.setattr(scholar_1027_cli, 'prior_session', lambda *args: None)
    async def spawn(*args, **kwargs):
        async def communicate():
            return b'{"is_error":true,"subtype":"error_max_turns"}', b''
        return SimpleNamespace(returncode=1, communicate=communicate)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    engine = SimpleNamespace(
        BASE_DIR=tmp_path, REVIEW_MODEL='test-model', JUDGE_MAX_TURNS=80,
        _find_claude_cli=lambda: 'claude', _claude_subprocess_env=lambda: {},
        _review_subprocess_timeout_seconds=lambda: 30,
        _clean_internal_references=dict,
        _fallback_result=lambda doi, error: {'doi': doi, 'trigger': 'review_error'},
        _build_judge_prompt=lambda *a, **k: 'synthesize',
    )
    install(engine, tmp_path, save, load)
    result = asyncio.run(engine._run_judge('10.test/a', '', '', '',
                                         {'result': '低风险'}, {'result': '低风险'}))
    assert result['trigger'] == 'review_error'
    assert 'result' not in result


def test_prior_session_requires_exact_prompt(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.claude/projects/-project'
    root.mkdir(parents=True)
    (root / 'same-doi-wrong-stage.jsonl').write_text(json.dumps({
        'type': 'user', 'message': {'content': 'DOI a, second review'}}) + '\n')
    assert scholar_1027_cli.prior_session('DOI a, first review', '/project') is None
    (root / 'matching.jsonl').write_text(json.dumps({
        'type': 'user', 'message': {'content': 'DOI a, first review'}}) + '\n')
    assert scholar_1027_cli.prior_session('DOI a, first review', '/project') == 'matching'


def test_random_draft_filename_does_not_reset_session(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.claude/projects/-project'
    root.mkdir(parents=True)
    prompt = 'DOI a, first review; /paper/artifacts/review_draft_' + 'a' * 32 + '.json'
    (root / 'saved.jsonl').write_text(json.dumps({'type': 'user', 'message': {'content': prompt}}) + '\n')
    changed = prompt.replace('a' * 32, 'b' * 32)
    assert scholar_1027_cli.prior_session(changed, '/project') == 'saved'
    assert scholar_1027_cli.prior_session(changed.replace('/paper/', '/another/'), '/project') is None


def test_resume_prefers_substantial_checkpoint_over_accidental_restart(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.claude/projects/-project'
    root.mkdir(parents=True)
    prompt = 'same paper and review stage'
    for name in ('advanced', 'empty-restart'):
        (root / f'{name}.jsonl').write_text(json.dumps({'type': 'user', 'message': {'content': prompt}}) + '\n')
    checkpoints = tmp_path / 'checkpoints'
    checkpoints.mkdir()
    (checkpoints / 'old_response.json').write_text(json.dumps({
        'session_id': 'advanced', 'response': {'num_turns': 160}}))
    assert scholar_1027_cli.prior_session(prompt, '/project', checkpoints) == 'advanced'


def test_changed_evidence_filename_requires_equal_contents(tmp_path):
    old = tmp_path / ('paper_' + 'a' * 12 + '_evidence.json')
    new = tmp_path / ('paper_' + 'b' * 12 + '_evidence.json')
    old.write_text(json.dumps({'high_findings': []}))
    new.write_text(json.dumps({'high_findings': []}))
    assert scholar_1027_cli.same_review_prompt(f'paper A evidence {old}', f'paper A evidence {new}')
    new.write_text(json.dumps({'high_findings': ['new finding']}))
    assert not scholar_1027_cli.same_review_prompt(f'paper A evidence {old}', f'paper A evidence {new}')
    assert not scholar_1027_cli.same_review_prompt(f'paper A evidence {old}', f'paper B evidence {old}')
