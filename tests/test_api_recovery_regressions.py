import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from api.models import TaskConfig, TaskMode, TaskStatus, PaperInfo
from api.services import task_manager as tasks
from api.services import detection_reports_db as db
from api.services import report, review
from api.services.review_validation import validate
from scripts import generate_review_report as entrypoint


def test_atomic_duplicate_submission_returns_one_task(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASKS_DIR', tmp_path)
    manager = tasks.TaskManager()
    config = TaskConfig(table_name='detection_reports', submission_no='same-upload')

    def submit(_):
        try:
            return manager.create_task_with_capacity(TaskMode.SINGLE, config, 1)
        except tasks.SubmissionInProgress as exc:
            return exc.task.task_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(submit, range(16)))
    assert len(set(ids)) == 1
    assert manager.unfinished_count() == 1


def test_pdf_cannot_repair_failed_task_and_errors_can_clear(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASKS_DIR', tmp_path)
    manager = tasks.TaskManager()
    task_id = manager.create_task(TaskMode.SINGLE, TaskConfig())
    manager.update_task(task_id, status=TaskStatus.FAILED,
                        error='Report generation failed: DB write failed', result={'stale': True})
    (tmp_path / task_id / 'review_results.json').write_text('[{"doi":"10.1/test"}]')
    (tmp_path / task_id / 'review_10.1_test.pdf').write_bytes(b'%PDF-1.4')
    restored = tasks.TaskManager()
    assert restored.load_from_disk() == []
    assert restored.get_task(task_id).status == TaskStatus.FAILED
    restored.update_task(task_id, status=TaskStatus.QUEUED, result=None, error=None)
    assert restored.get_task(task_id).error is None
    assert restored.get_task(task_id).result is None
    assert restored.get_task(task_id).completed_at is None


def test_restart_only_recovers_latest_submission(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASKS_DIR', tmp_path / 'tasks')
    input_dir = tmp_path / 'input'
    input_dir.mkdir()
    manager = tasks.TaskManager()
    config = TaskConfig(table_name='detection_reports', submission_no='upload')
    ids = [manager.create_task(TaskMode.SINGLE, config) for _ in range(2)]
    for task_id in ids:
        manager.update_task(task_id, papers=[PaperInfo(
            doi_slug='paper', input_dir=str(input_dir), submission_no='upload')])
    restored = tasks.TaskManager()
    assert [r.task_id for r in restored.load_from_disk()] == [ids[-1]]
    assert restored.get_task(ids[0]).stage == 'superseded'


def test_stale_initial_writer_rejected_using_creation_time(monkeypatch, tmp_path):
    monkeypatch.setattr(db, 'TASKS_DIR', tmp_path)
    for name, time in [('new', '2026-09-28T08:00:00'), ('old', '2026-09-27T08:00:00')]:
        (tmp_path / name).mkdir()
        (tmp_path / name / 'status.json').write_text(json.dumps({'created_at': time}))
    with pytest.raises(RuntimeError, match='Stale task'):
        db._assert_newer_owner('old', 'new')
    db._assert_newer_owner('new', 'old')


def test_late_review_write_and_rowcount_fallback_are_task_fenced(monkeypatch):
    class Cursor:
        rowcount = 0
        def __init__(self): self.queries = []
        def execute(self, sql, params): self.queries.append((sql, params))
        def fetchone(self): return None
    cursor = Cursor()
    class Connection:
        def cursor(self): return cursor
        def commit(self): pass
        def close(self): pass
    monkeypatch.setattr(db.pymssql, 'connect', lambda **kwargs: Connection())
    assert db.update_review('upload', None, '高风险', 'http://report', task_id='old') is False
    assert len(cursor.queries) == 2
    for sql, params in cursor.queries:
        assert 'AND task_id=%(task_id)s' in sql
        assert params['task_id'] == 'old'


def test_render_retry_does_not_repeat_review_and_semantic_errors_do_not_retry(monkeypatch, tmp_path):
    render = AsyncMock(side_effect=[TimeoutError('timeout'), ['report.pdf']])
    monkeypatch.setattr(report, '_generate_reports_once', render)
    monkeypatch.setattr(report.asyncio, 'sleep', AsyncMock())
    assert asyncio.run(report.generate_reports([], tmp_path)) == ['report.pdf']
    assert render.await_count == 2
    render.reset_mock(side_effect=True)
    render.side_effect = RuntimeError('incomplete multi-dimension review')
    with pytest.raises(RuntimeError):
        asyncio.run(report.generate_reports([], tmp_path))
    assert render.await_count == 1


def _result():
    return {
        'doi': '10.1/test', 'result': '低风险', 'verdict': '建议低风险',
        'image_review': '已渲染 Figure 1 和 Figure 2，与源数据逐项核对一致。',
        'data_review': '数据已逐列核对，没有发现足以构成高风险的独立证据。',
        'ref_review': '参考文献逐项核查，文献著录未见明显异常。',
        'methodology_review': '研究设计与统计方法已核对，未见明确异常。',
        'reason': '当前材料未发现足以判定为高风险的证据。',
        'dimension_verdicts': {k: '建议低风险' for k in ('image', 'data', 'ref', 'methodology')},
    }


def test_review_and_renderer_use_identical_gate(monkeypatch, tmp_path):
    evidence = {'main_figures': ['fig1.png', 'fig2.png'], 'high_findings': [], 'deterministic_findings': {}}
    evidence_path = tmp_path / 'evidence.json'
    evidence_path.write_text(json.dumps(evidence))
    result = dict(_result(), _evidence_path=str(evidence_path))
    class Renderer:
        def _coverage_check(self, result): raise AssertionError('should use exact evidence')
    renderer = Renderer()
    entrypoint._patch_coverage_entrypoint(renderer)
    reviewed = review._validate_review_result('10.1/test', '', '', result, evidence)
    rendered = renderer._coverage_check(result)
    assert reviewed['_coverage_gaps'] == rendered['_coverage_gaps'] == []
    assert rendered['image_review'] == result['image_review']


def test_pooled_description_cannot_hide_distinct_channel_duplicate():
    result = _result()
    result['data_review'] += ' Repeat1/2/3 pooled together，合并列可解释为正常汇总。'
    evidence = {'main_figures': [], 'deterministic_findings': {
        'duplicate_column_pairs': [{
            'file': 'source.xlsx', 'sheet': 'Fig3e', 'cross_group': True,
            'group_a': 'GFP', 'group_b': 'RFP', 'col_a_header': 'channel1', 'col_b_header': 'channel2',
        }],
        'passage_offset_pattern': [{'severity': 'high'}],
    }}
    _, gaps = validate(result, evidence)
    assert any('跨组重复列对' in gap for gap in gaps)
    assert any('跨行' in gap for gap in gaps)


def test_structured_detail_and_contradiction_are_caught_before_render():
    result = dict(_result(), result='高风险', verdict='建议高风险')
    _, gaps = validate(result, {'high_findings': []})
    assert any('structured fields' in gap for gap in gaps)
    result['verdict'] = '建议低风险'
    _, gaps = validate(result, {'high_findings': []})
    assert any('inconsistent review JSON' in gap for gap in gaps)


def test_detector_channel_type_accepts_explicit_channel_review():
    result = _result()
    result['data_review'] = '已核查 Fig3e 的两个独立通道，重复数值源于相同横轴；测量值各自不同，未见异常。'
    evidence = {'high_findings': [{'dim': 'data', 'test': 'same_sheet_channel_duplicate'}]}
    _, gaps = validate(result, evidence)
    assert not any('same_sheet_channel_duplicate' in gap for gap in gaps)


def test_highrisk_coverage_gap_enters_repair(monkeypatch, tmp_path):
    result = dict(_result(), result='高风险', verdict='建议高风险', trigger='review')
    monkeypatch.setattr(review, '_preprocess_review_input_dir', lambda _: None)
    monkeypatch.setattr(review, '_ensure_evidence_bundle', lambda *args: '')
    monkeypatch.setattr(review, '_run_with_retry', AsyncMock(return_value=result))
    monkeypatch.setattr(review, '_accept_single_review', lambda *args: (False, 'gap'))
    monkeypatch.setattr(review, '_accept_lowrisk_consensus', lambda *args: False)
    monkeypatch.setattr(review, '_validate_review_result', lambda *args: dict(
        result, _coverage_gaps=['未对HIGH发现给出结论: data/rowwise_value_reuse']))
    judge = AsyncMock(return_value=result)
    monkeypatch.setattr(review, '_run_judge', judge)
    final = asyncio.run(review._run_review_single_impl('10.1/test', '', '', str(tmp_path)))
    assert judge.await_count == 2
    assert judge.await_args.kwargs['coverage_gaps']
    assert final['trigger'] == 'review_error'
