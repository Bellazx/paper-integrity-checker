import asyncio
import json
from types import SimpleNamespace

import pytest
from api.services import review_cli, review_runtime, review_validation
from scripts import generate_review_report as publisher
from tests.test_review_cli_convergence import engine, mock_cli


def test_explicit_chinese_issue_numbers_bind_without_semantic_inference():
    from modules.review_report_layout import bind_issue_evidence
    text = '问题 1：具体数值不同\n问题2：具体样本标签不同'
    _, items = bind_issue_evidence(text, [{'dimension':'data','issue_index':2}])
    assert items[0][0]==1 and items[0][2]==[]
    assert items[1][0]==2 and items[1][2][0]['issue_index']==2
    with pytest.raises(ValueError):
        bind_issue_evidence(text, [{'dimension':'data','issue_index':3}])


def test_judge_formatting_repair_cannot_restart_tools(monkeypatch):
    from api.services import review
    from unittest.mock import AsyncMock
    call = AsyncMock(return_value={'doi':'x','result':'低风险'})
    monkeypatch.setattr(review, '_run_claude_prompt', call)
    asyncio.run(review._run_judge('x','r','i','o',{}, {},
        coverage_gaps=['Evidence needs a valid issue_index matching a numbered issue in dimension data']))
    assert call.await_args.kwargs['allowed_tools']==()
    asyncio.run(review._run_judge('x','r','i','o',{}, {},
        coverage_gaps=['未对HIGH发现给出结论: data/rowwise_value_reuse']))
    assert call.await_args.kwargs['allowed_tools']==('Bash','Read')


def test_investigation_budget_uses_only_whole_paper_deadline(monkeypatch):
    monkeypatch.delenv('REVIEW_CLI_INVESTIGATION_TIMEOUT_SECONDS', raising=False)
    monkeypatch.setattr(review_cli.time, 'monotonic', lambda: 100)
    token = review_runtime.paper_deadline.set(600)
    try:
        assert review_cli.investigation_budget(0) == 500
        assert review_cli.investigation_budget(1) == 500
        assert review_cli.investigation_budget(2) == 500
    finally:
        review_runtime.paper_deadline.reset(token)
    assert review_cli.investigation_budget(0) is None


def test_real_budget_expiry_reaps_and_finalizes(tmp_path, monkeypatch):
    e = engine(tmp_path)
    commands = []
    async def spawn(*cmd, **kwargs):
        commands.append(cmd)
        async def communicate():
            if len(commands) == 1:
                await asyncio.Event().wait()
            return json.dumps({'result': json.dumps({'doi': 'x', 'result': '低风险'})}).encode(), b''
        return SimpleNamespace(returncode=0, communicate=communicate)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    token = review_runtime.paper_deadline.set(review_cli.time.monotonic() + .01)
    states = []
    try:
        out = asyncio.run(review_cli.run(e, 'x', 'review', 'p', 24, persist=lambda s: states.append(dict(s))))
    finally:
        review_runtime.paper_deadline.reset(token)
    assert out['trigger'] == 'review_error' and len(commands) == 1
    e._kill_and_reap_process.assert_awaited_once()
    assert states[0]['timeout_seconds'] <= .1


def test_schema_repair_disables_tools_without_changing_facts(tmp_path, monkeypatch):
    from api.services import review
    bundle = tmp_path / 'evidence.json'
    bundle.write_text('{}')
    prompt = review._publication_preflight_instruction('x', review.EvidencePaths('', str(bundle)))
    calls = []
    def check(value, evidence):
        calls.append(value)
        return value, [] if value.get('verdict') else ['Verdict must agree with the explicit risk conclusion']
    monkeypatch.setattr(review_validation, 'validate', check)
    commands = mock_cli(monkeypatch, [
        (0, {'result': json.dumps({'doi': 'x', 'result': '低风险', 'data_review': 'verified facts'})}),
        (0, {'result': json.dumps({'doi': 'x', 'result': '低风险', 'verdict': '建议低风险', 'data_review': 'verified facts'})}),
    ])
    out = asyncio.run(review_cli.run(engine(tmp_path), 'x', 'review', prompt, 24))
    assert out['data_review'] == 'verified facts' and len(commands) == 2
    assert commands[1][commands[1].index('--tools')+1] == ''
    assert 'verified facts' in commands[1][commands[1].index('-p')+1]
    assert not review_cli.schema_only_gaps(['未对HIGH发现给出结论: data/rowwise_value_reuse'])


def test_saved_invalid_json_uses_output_only_recovery(tmp_path, monkeypatch):
    from api.services import review
    from pathlib import Path
    bundle = tmp_path / 'evidence.json'
    bundle.write_text('{}')
    prompt = review._publication_preflight_instruction('x', review.EvidencePaths('', str(bundle)))
    paths = review_cli.preflight_paths(prompt)
    candidate = {'doi':'x', 'result':'低风险', 'data_review':'existing verification'}
    Path(paths['result']).write_text(json.dumps(candidate))
    def check(value, evidence):
        return value, [] if value.get('verdict') else ['Verdict must agree with the explicit risk conclusion']
    monkeypatch.setattr(review_validation, 'validate', check)
    commands = mock_cli(monkeypatch, [(0, {'result':json.dumps({**candidate,'verdict':'建议低风险'})})])
    out = asyncio.run(review_cli.run(engine(tmp_path),'x','repair',prompt,24,
                                    state={'phase':1,'session_id':'previous','preflight':paths}))
    assert out['data_review']=='existing verification' and len(commands)==1
    assert commands[0][commands[0].index('--tools')+1]==''


def test_lost_session_rehydrates_task_without_refunding_research(tmp_path, monkeypatch):
    commands = mock_cli(monkeypatch, [(0, {'result':json.dumps({'doi':'x','result':'低风险'})})])
    out = asyncio.run(review_cli.run(engine(tmp_path),'x','review','original task with evidence',24,
        state={'phase':2,'session_id':'lost','last_error':'CLI resume session not found'}))
    assert out['result']=='低风险' and len(commands)==1
    assert '--session-id' in commands[0] and '--resume' not in commands[0]
    assert commands[0][commands[0].index('--max-turns')+1]=='6'
    assert commands[0][commands[0].index('--tools')+1]==''
    assert 'original task with evidence' in commands[0][commands[0].index('-p')+1]


def test_invalid_paper_does_not_block_other_publications(tmp_path, monkeypatch):
    path = tmp_path / 'results.json'
    path.write_text(json.dumps([{'doi':'a'}, {'doi':'bad'}, {'doi':'error','trigger':'review_error'}, {'doi':'b'}]))
    argv = ['publish', '--results', str(path), '--no-db']
    monkeypatch.setattr(publisher.sys, 'argv', argv)
    calls = []
    def render():
        rows = json.loads(__import__('pathlib').Path(publisher.sys.argv[2]).read_text())
        calls.append(rows[0]['doi'])
        if rows[0]['doi'] == 'bad': raise ValueError('invalid evidence map')
        return 0
    module = SimpleNamespace(main=render)
    monkeypatch.setattr(publisher, 'renderer_module', lambda: module)
    monkeypatch.setattr(publisher, '_patch_database_writer', lambda m: None)
    assert publisher.main() == 2
    assert calls == ['a','bad','b']
    assert publisher.sys.argv == argv
    failures = json.loads(path.with_suffix('.publication_failures.json').read_text())
    assert [x['doi'] for x in failures] == ['bad','error']


def test_generated_page_evidence_gets_issue_indices():
    from scripts.generate_review_report import _attach_generated_evidence_indices

    info = {
        'methodology_review': (
            '（1）问题位置：论文第12页式(1)。问题类型：公式不一致。'
            '\n\n（2）问题位置：论文第17页方法段。问题类型：物种不一致。'
        ),
    }
    items = [
        {'dimension': 'methodology', 'caption': '页面证据：论文第12页；具体位置见问题说明。'},
        {'dimension': 'methodology', 'caption': '页面证据：论文第17页；具体位置见问题说明。'},
    ]

    result = _attach_generated_evidence_indices(info, items)

    assert [item['issue_index'] for item in result] == [1, 2]
