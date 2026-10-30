import json
import os
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from api.models import PaperInfo, TaskRecord, TaskMode, TaskStatus
from api.services.task_progress import build_task_progress
from api import progress_app


def record(tmp_path, status=TaskStatus.REVIEWING):
    output = tmp_path / 'out'
    output.mkdir()
    (output / 'report.json').write_text('{}')
    return TaskRecord(task_id='20260928-11223344', mode=TaskMode.SINGLE, status=status,
                      created_at=datetime(2026, 9, 28, tzinfo=timezone.utc),
                      papers=[PaperInfo(doi_slug='test', input_dir=str(tmp_path / 'input'),
                                        output_dir=str(output))])


def test_saved_rounds_are_not_task_completion_and_no_verdict_exposed(tmp_path):
    task = record(tmp_path)
    paper = task.papers[0]
    state = tmp_path / 'out/review_artifacts/x_state.json'
    state.parent.mkdir()
    report = tmp_path / 'out/report.json'
    state.write_text(json.dumps({'identity': {'input': {'path': paper.input_dir},
                                            'report': {'path': str(report), 'mtime_ns': report.stat().st_mtime_ns}},
                                 'stages': {'reviewer1': {'result': '高风险'}}}))
    result = build_task_progress(task, sessions=[])
    assert result['label'] == '第二轮复审'
    assert result['percent'] is None
    assert result['steps'][2]['state'] == 'active'
    assert '高风险' not in json.dumps(result, ensure_ascii=False)
    # An old checkpoint for changed findings must not be presented as current.
    report.write_text('{"changed": true}')
    stamp = report.stat().st_mtime_ns + 1_000_000_000
    os.utime(report, ns=(stamp, stamp))
    assert build_task_progress(task, sessions=[])['label'] == '第一轮复审'


def test_low_initial_completion_skips_review(tmp_path):
    task = record(tmp_path, TaskStatus.COMPLETED)
    task.result = {'detection_reports': [{'chushen_result': '低风险', 'review_result': None}]}
    result = build_task_progress(task, sessions=[])
    assert result['percent'] == 100
    assert result['steps'][2]['state'] == 'skipped'
    assert not result['indeterminate']


def test_recovered_process_error_is_not_a_ready_final_report(monkeypatch, tmp_path):
    from api.services import task_progress as progress
    task = record(tmp_path)
    stages = {'judge': {'result': '高风险'},
              'final': {'result': '高风险', 'trigger': 'review_error'}}
    monkeypatch.setattr(progress, '_review_stages', lambda paper: stages)
    assert build_task_progress(task, sessions=[])['label'] == '综合复核后的补充核查'
    stages['final'] = {'result': '高风险', '_coverage_gaps': ['missing fact']}
    assert build_task_progress(task, sessions=[])['label'] == '综合复核后的补充核查'
    stages['final'] = {'result': '高风险', '_coverage_gaps': []}
    assert build_task_progress(task, sessions=[])['label'] == '复审结果已保存，等待报告处理'


def test_cancellation_never_animates_as_running(tmp_path):
    task = record(tmp_path, TaskStatus.FAILED)
    task.stage = 'cancelled_by_user'
    result = build_task_progress(task, sessions=[])
    assert result['label'] == '已人工终止'
    assert not result['indeterminate']
    assert all(s['state'] != 'active' for s in result['steps'])


def test_activity_is_scoped_to_input_and_silence_is_not_failure(tmp_path):
    task = record(tmp_path)
    now = datetime(2026, 9, 28, 2, tzinfo=timezone.utc)
    session = {'prompt': task.papers[0].input_dir + '\n', 'started_at': '2026-09-28T01:00:00Z',
               'last_activity_at': '2026-09-28T01:00:00Z', 'activity': '核查工具已返回'}
    result = build_task_progress(task, sessions=[session], now=now)
    assert result['papers'][0]['activity_state'] == 'quiet'
    assert result['status'] == 'reviewing'
    session['prompt'] += 'another task'
    task.papers[0].input_dir += '-different'
    assert build_task_progress(task, sessions=[session], now=now)['papers'][0]['activity_state'] == 'unknown'


def test_read_only_endpoint_and_validation(monkeypatch, tmp_path):
    task = record(tmp_path)
    folder = tmp_path / task.task_id
    folder.mkdir()
    source = folder / 'status.json'
    source.write_text(task.model_dump_json())
    before = source.read_bytes()
    monkeypatch.setattr(progress_app, 'TASKS_DIR', tmp_path)
    client = TestClient(progress_app.app)
    assert client.get('/api/task/bad-id/progress').status_code == 400
    assert client.get('/api/task/20260928-ffffffff/progress').status_code == 404
    response = client.get('/api/task/' + task.task_id + '/progress')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert source.read_bytes() == before
    assert client.post('/api/task/' + task.task_id + '/progress').status_code == 405


def test_long_synthesis_prompt_and_tool_activity_are_read_without_raw_content(monkeypatch, tmp_path):
    from api.services import task_progress as progress
    session = tmp_path / 'session.jsonl'
    prompt = '资料' * 15000 + '\n/private/paper\n'
    session.write_text('\n'.join(json.dumps(r) for r in [
        {'type': 'user', 'timestamp': '2026-09-28T01:00:00Z', 'message': {'content': prompt}},
        {'type': 'assistant', 'timestamp': '2026-09-28T01:01:00Z', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'openpyxl PRIVATE_COMMAND'}}]}},
    ]))
    monkeypatch.setattr(progress, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(progress, '_session_cache', (0, []))
    sessions = progress._sessions()
    assert len(sessions) == 1
    assert sessions[0]['activity'] == '正在核对源数据'
    assert sessions[0]['last_activity_at'] == '2026-09-28T01:01:00Z'
