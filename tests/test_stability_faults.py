import asyncio
import json
import stat
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api.models import PaperInfo, TaskConfig, TaskMode, TaskStatus, DetectionSubmitRequest
from api.services import detect, review, task_manager, zip_handler
from api.services.review_validation import validate


@pytest.mark.parametrize('existing', [False, True])
def test_detector_zero_exit_without_fresh_report_is_failure(monkeypatch, tmp_path, existing):
    report = tmp_path / 'report.json'
    if existing:
        report.write_text(json.dumps({'paper': {'doi': '10.1/old'}, 'summary': {}}))
    proc = type('Process', (), {'returncode': 0, 'communicate': AsyncMock(return_value=(b'', b''))})()
    monkeypatch.setattr(detect.asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    with pytest.raises(RuntimeError, match='fresh report.json'):
        asyncio.run(detect.run_detection_single('/input', str(tmp_path), write_db=False))


def test_normal_batch_never_marks_all_detection_failures_complete(monkeypatch, tmp_path):
    from api import worker
    monkeypatch.setattr(task_manager, 'TASKS_DIR', tmp_path / 'tasks')
    tm = task_manager.TaskManager()
    tid = tm.create_task(TaskMode.BATCH, TaskConfig())
    paper = PaperInfo(doi_slug='test', output_dir=str(tmp_path / 'output'))
    tm.update_task(tid, papers=[paper])
    monkeypatch.setattr(worker, 'run_detection_single', AsyncMock(side_effect=RuntimeError('missing report')))
    asyncio.run(worker.run_pipeline(tid, tm, [paper], {}))
    task = tm.get_task(tid)
    assert task.status == TaskStatus.FAILED
    assert task.result['detected_ok'] == 0 and task.result['detected_fail'] == 1
    assert 'missing report' in task.error


def test_normal_batch_resume_preserves_detection_and_successful_sibling(monkeypatch, tmp_path):
    from api import worker
    monkeypatch.setattr(task_manager, 'TASKS_DIR', tmp_path / 'tasks')
    monkeypatch.setattr(worker, 'TASKS_DIR', tmp_path / 'tasks')
    tm = task_manager.TaskManager()
    tid = tm.create_task(TaskMode.BATCH, TaskConfig())
    papers = []
    for name in ('good', 'bad'):
        output = tmp_path / name; output.mkdir()
        (output / 'report.json').write_text(json.dumps({'paper': {'doi': '10.1000/' + name}, 'summary': {}}))
        papers.append(PaperInfo(doi='10.1000/' + name, doi_slug=name, output_dir=str(output), status='detected'))
    tm.update_task(tid, papers=papers)
    detector = AsyncMock(side_effect=AssertionError('must reuse completed detection'))
    monkeypatch.setattr(worker, 'run_detection_single', detector)
    monkeypatch.setattr('modules.chinese_report_generator._compute_overall_risk', lambda f: {'level': '高风险'})
    async def reviewer(**kw):
        assert kw['resume'] is True
        if kw['doi'].endswith('bad'):
            return review._fallback_result(kw['doi'], 'missing evidence')
        return {'doi': kw['doi'], 'result': '高风险', 'trigger': 'review'}
    monkeypatch.setattr(worker, 'run_review_single', reviewer)
    render = AsyncMock(return_value=['good.pdf'])
    monkeypatch.setattr(worker, 'generate_reports', render)
    asyncio.run(worker.run_pipeline(tid, tm, papers, {}))
    task = tm.get_task(tid)
    assert task.status == TaskStatus.FAILED
    assert task.result['reviewed'] == task.result['review_failed'] == 1
    assert task.result['confirmed_high'] == 1 and task.result['reports_generated'] == 1
    assert len(render.call_args.args[0]) == 1
    detector.assert_not_awaited()


def test_stream_worker_exception_ends_stream_and_cancels_siblings():
    from api.routes.run import _concurrent_stream
    async def scenario():
        sibling_stopped = asyncio.Event()
        async def worker(item, emit):
            if item == 'bad':
                await asyncio.sleep(0)
                raise ValueError('worker failed')
            try:
                await asyncio.Event().wait()
            finally:
                sibling_stopped.set()
        async def consume():
            return [event async for event in _concurrent_stream(['good', 'bad'], worker, 2)]
        with pytest.raises(ValueError, match='worker failed'):
            await asyncio.wait_for(consume(), 1)
        assert sibling_stopped.is_set()
    asyncio.run(scenario())


def test_stream_disconnect_cancels_work_instead_of_waiting_forever():
    from api.routes.run import _concurrent_stream
    async def scenario():
        stopped = asyncio.Event()
        async def worker(item, emit):
            emit('started')
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        stream = _concurrent_stream([1], worker, 1)
        assert await anext(stream) == 'started'
        await asyncio.wait_for(stream.aclose(), 1)
        assert stopped.is_set()
    asyncio.run(scenario())


def test_bad_new_upload_does_not_suppress_valid_old_task_on_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(task_manager, 'TASKS_DIR', tmp_path / 'tasks')
    inputs = tmp_path / 'input'; inputs.mkdir()
    tm = task_manager.TaskManager()
    config = TaskConfig(table_name='detection_reports', submission_no='upload')
    old = tm.create_task(TaskMode.SINGLE, config)
    tm.update_task(old, status=TaskStatus.REVIEWING, papers=[PaperInfo(doi_slug='paper', input_dir=str(inputs))])
    bad = tm.create_detection_submission(TaskMode.SINGLE, config, 240)
    tm.update_task(bad, status=TaskStatus.FAILED, error='invalid archive')
    recovered = task_manager.TaskManager().load_from_disk()
    assert [r.task_id for r in recovered] == [old]


@pytest.mark.parametrize('filename', ['../escape.pdf', 'C:/escape.pdf', '..\\escape.pdf', '/escape.pdf'])
def test_archive_rejects_unsafe_member_paths(monkeypatch, tmp_path, filename):
    monkeypatch.setattr(zip_handler, 'RUNTIME_TMP_DIR', tmp_path)
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(filename, b'pdf')
    with pytest.raises(ValueError, match='unsafe path'):
        zip_handler.validate_archive(archive)


def test_archive_rejects_links_and_oversized_expansion(monkeypatch, tmp_path):
    monkeypatch.setattr(zip_handler, 'RUNTIME_TMP_DIR', tmp_path)
    archive = tmp_path / 'upload.zip'
    item = zipfile.ZipInfo('paper.pdf'); item.create_system = 3
    item.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(item, '/outside')
    with pytest.raises(ValueError, match='link'):
        zip_handler.validate_archive(archive)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('paper.pdf', b'0' * 1024)
    monkeypatch.setattr(zip_handler, 'MAX_EXTRACTED_SIZE_BYTES', 100)
    with pytest.raises(ValueError, match='expanded-size'):
        zip_handler.validate_archive(archive)


def test_failed_extraction_removes_its_scratch_directory(monkeypatch, tmp_path):
    runtime = tmp_path / 'runtime'; runtime.mkdir()
    monkeypatch.setattr(zip_handler, 'RUNTIME_TMP_DIR', runtime)
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('not_a_paper.txt', 'no pdf')
    with pytest.raises(ValueError, match='PDF'):
        zip_handler.extract_single(archive, 'test-task')
    assert list(runtime.iterdir()) == []


@pytest.mark.parametrize('file_id', ['../x', '/tmp/x', 'x/y', '*', 'x\\y'])
def test_invalid_file_id_cannot_resolve_upload_outside_its_root(file_id):
    from api.config import resolve_upload_dir
    with pytest.raises(ValueError):
        DetectionSubmitRequest(file_id=file_id)
    with pytest.raises(ValueError):
        resolve_upload_dir(file_id)


def test_evidence_failure_and_missing_labels_do_not_invent_high_risk():
    original = {'result': '低风险', 'verdict': '建议低风险', 'data_review': 'unchanged'}
    blocked = review._fail_closed_on_evidence_missing('10.1/test', original, 'timeout')
    assert blocked['result'] == '低风险' and blocked['_coverage_error'] == 'timeout'
    assert original['data_review'] == blocked['data_review']
    assert review._normalize_review_result({'result': '高风险'})['dimension_verdicts'] == {}


def test_review_json_parser_preserves_nested_fields_with_braces_in_prose():
    value = {'doi': '10.1/test', 'result': '高风险', 'data_review': 'label contains } and an open {',
             'dimension_verdicts': {'data': '建议高风险'}}
    assert review._extract_json_from_text('Here is the result:\n' + json.dumps(value) + '\nDone.') == value


@pytest.mark.parametrize('bad', [{'result': []}, {'data_review': {}}, {'dimension_verdicts': []}])
def test_malformed_review_schema_returns_actionable_gaps(bad):
    _, gaps = validate(bad, {'high_findings': []})
    assert gaps and any('must' in gap for gap in gaps)


@pytest.mark.parametrize('row', [None, ('wrong', 'review'), ('initial', 'review')])
def test_english_urls_require_matching_current_owner_row(monkeypatch, row):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api.services import detection_reports_db as db
    cursor = Mock(); cursor.fetchone.return_value = row
    connection = Mock(); connection.cursor.return_value = cursor
    monkeypatch.setattr(db, 'pymssql', SimpleNamespace(connect=lambda **kw: connection))
    if row == ('initial', 'review'):
        db.update_english_reports('upload', 'folder', 'new-task', 'initial', 'review')
        connection.commit.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match='current task row'):
            db.update_english_reports('upload', 'folder', 'new-task', 'initial', 'review')
        connection.commit.assert_not_called()
    for call in cursor.execute.call_args_list:
        assert 'task_id=%(task_id)s' in call.args[0]
        assert call.args[1]['task_id'] == 'new-task'
        assert call.args[1]['fold_name'] == 'folder'
    connection.close.assert_called_once()


@pytest.mark.parametrize('has_scholar', [True, False])
@pytest.mark.parametrize('rows', [[], [('低风险', 'old-url', 2)], [('高风险', 'new-url', 2)]])
def test_batch_db_writer_verifies_before_commit_and_closes(monkeypatch, rows, has_scholar):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from scripts.generate_review_report import _patch_database_writer
    cursor = Mock(); cursor.fetchall.return_value = rows
    # Test-set tables have no scholar column; the writer probes the schema first.
    cursor.fetchone.return_value = (1,) if has_scholar else None
    connection = Mock(); connection.cursor.return_value = cursor
    module = SimpleNamespace(DB_CONFIG={}, pymssql=SimpleNamespace(connect=lambda **kw: connection))
    _patch_database_writer(module)
    updates = [{'doi': '10.1000/test', 'review_result': '高风险', 'review_report_url': 'new-url'}]
    if rows == [('高风险', 'new-url', 2)]:
        module._update_db(updates, scholar='scholar')
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match='verification failed'):
            module._update_db(updates, scholar='scholar')
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
    probe, *statements = [c.args[0] for c in cursor.execute.call_args_list]
    assert 'INFORMATION_SCHEMA.COLUMNS' in probe
    assert statements
    assert all(('AND scholar=%(scholar)s' in sql) == has_scholar for sql in statements)
    connection.close.assert_called_once()


def test_standalone_batch_retries_unpublished_failed_and_latest_entries(monkeypatch, tmp_path):
    import batch_rereview as batch
    monkeypatch.setattr(batch, 'RESULTS_FILE', tmp_path / 'results.json')
    monkeypatch.setattr(batch, 'REVIEW_V2_DIR', tmp_path)
    results = [
        {'doi': '10.1000/good', '_publication_complete': True},
        {'doi': '10.1000/failed', 'trigger': 'review_error', '_publication_complete': True},
        {'doi': '10.1000/unpublished'},
        {'doi': '10.1000/latest', '_publication_complete': True},
        {'doi': '10.1000/latest', '_publication_complete': False},
    ]
    for r in results:
        batch._published_pdf(r).write_bytes(b'%PDF-1.4')
    batch.save_results(results)
    assert batch.load_done() == ({'10.1000/good'}, results)
    assert not (tmp_path / 'results.json.tmp').exists()


@pytest.mark.parametrize('rc, marker, contents, published', [
    (1, True, b'%PDF-1.4', False), (0, False, b'%PDF-1.4', False),
    (0, True, b'not PDF', False), (0, True, b'%PDF-1.4', True),
])
def test_standalone_batch_never_counts_old_pdf_or_failed_render(monkeypatch, tmp_path, rc, marker, contents, published):
    from types import SimpleNamespace
    import batch_rereview as batch
    monkeypatch.setattr(batch, 'TMP_DIR', tmp_path)
    monkeypatch.setattr(batch, 'REVIEW_V2_DIR', tmp_path)
    result = {'doi': '10.1000/test', 'result': '高风险'}
    path = batch._published_pdf(result); path.write_bytes(contents)
    monkeypatch.setattr(batch.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=rc, stdout=f'[PDF] {path.name}' if marker else '', stderr=''))
    batch.generate_reports([result])
    assert result['_publication_complete'] is published


def test_same_doi_batch_folders_publish_independent_bilingual_reports(monkeypatch, tmp_path):
    from api.services import detection_reports_worker as worker
    from tests.test_detection_reports_state import _TaskCapture
    from modules import english_report_generator as english
    monkeypatch.setattr(worker, 'TASKS_DIR', tmp_path / 'tasks')
    monkeypatch.setattr(worker, 'OUTPUT_DIR', tmp_path / 'output')
    monkeypatch.setattr(worker, 'REVIEW_DIR', tmp_path / 'review')
    monkeypatch.setattr('modules.chinese_report_generator._compute_overall_risk', lambda _: {'level': '高风险'})
    monkeypatch.setattr(worker, 'verify_public_pdf', AsyncMock())
    monkeypatch.setattr(english, 'generate_english_initial_pdf', lambda *a, **kw: None)

    async def english_review(value, output_dir, task_dir, *, kind, slug='', deadline=None):
        # The review renderer runs as a subprocess (api.services.report); stand in for it.
        path = Path(output_dir) / english.review_report_filename(slug)
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'%PDF-1.4')
        return str(path)
    monkeypatch.setattr(worker, 'generate_english_report', english_review)
    chinese, bilingual = {}, {}
    monkeypatch.setattr(worker, 'upsert_chushen', lambda **kw: chinese.setdefault(kw['fold_name'], kw))
    monkeypatch.setattr(worker, 'verify_chushen_write', lambda **kw: (True, ''))
    monkeypatch.setattr(worker, 'update_review', lambda **kw: chinese[kw['fold_name']].update(kw) or True)
    monkeypatch.setattr(worker, 'verify_review_write', lambda **kw: (True, ''))
    monkeypatch.setattr(worker, 'update_english_reports', lambda sub, fold, tid, initial, review: bilingual.update({fold: (initial, review)}))
    async def detect_one(**kw):
        findings = {'paper': {'doi': '10.1000/same', 'title': 'Same paper'}, 'summary': {}, 'pdf_generated': True}
        out = Path(kw['output_dir']); out.mkdir(parents=True)
        (out / 'report.json').write_text(json.dumps(findings))
        return findings
    async def render(results, task_dir, **kw):
        path = worker.REVIEW_DIR / kw['report_namespace'] / 'review_10.1000_same.pdf'
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'%PDF-1.4')
        return [str(path)]
    monkeypatch.setattr(worker, 'run_detection_single', detect_one)
    monkeypatch.setattr(worker, 'run_review_single', AsyncMock(side_effect=lambda **kw: {'doi': '10.1000/same', 'result': '高风险', 'trigger': 'review'}))
    monkeypatch.setattr(worker, 'generate_reports', render)
    papers = [PaperInfo(doi='10.1000/same', doi_slug='10.1000_same', submission_no='upload',
                        fold_name=folder, language='both', input_dir=str(tmp_path / folder),
                        output_dir=str(tmp_path / folder / 'output')) for folder in ('folder-a', 'folder-b')]
    manager = _TaskCapture()
    asyncio.run(worker._run_inner('batch-task', manager, papers, {'max_workers': 1}))
    final = manager.updates[-1]
    assert final['status'] == TaskStatus.COMPLETED, final
    records = final['result']['detection_reports']
    for field in ('chushen_report_url', 'review_report_url', 'english_chushen_report_url', 'english_review_report_url'):
        assert len({r[field] for r in records}) == 2
    for r in records:
        fold = r['fold_name']
        assert chinese[fold]['chushen_report_url'] == r['chushen_report_url']
        assert chinese[fold]['review_report_url'] == r['review_report_url']
        assert bilingual[fold] == (r['english_chushen_report_url'], r['english_review_report_url'])


CAPTION_STYLE_MEMBERS = [
    '10.1038_s41467-026-77281-x/figure/Fig. 1_ Experimental realization of hydrodynamic reconfiguration..png',
    '10.1021_jacs.5c04397/figure/10.1021-jacs.5c04397_Figure_1..pptx',
    'paper/notes...txt',
]
TRAVERSAL_MEMBERS = ['../escape.pdf', 'C:/escape.pdf', '..\\escape.pdf', '/escape.pdf', 'a/../b.pdf']


def _archive_with(tmp_path, member):
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        if '/' in member and not member.startswith(('/', '..')):
            z.writestr(member.split('/')[0] + '/main.pdf', b'%PDF-1.4')
        z.writestr(member, b'%PDF-1.4' if member.endswith('.pdf') else b'x')
    return archive


@pytest.mark.parametrize('member', CAPTION_STYLE_MEMBERS)
def test_upload_validation_accepts_caption_style_double_dot_filenames(tmp_path, member):
    # 200 of 226 Nature packages carry "<caption>..png" figure names; the old
    # substring test for ".." rejected them as traversal attempts.
    from api.routes.upload import _validate_archive_contents
    info = _validate_archive_contents(_archive_with(tmp_path, member), '.zip')
    assert not isinstance(info, list), info
    assert info.paper_count == 1


@pytest.mark.parametrize('member', CAPTION_STYLE_MEMBERS)
def test_every_archive_gate_accepts_caption_style_double_dot_filenames(tmp_path, member):
    # /api/upload, the /api/run debug stream and extraction share one rule.
    from api.routes.run import _validate_contents
    from api.services.zip_handler import validate_archive
    archive = _archive_with(tmp_path, member)
    assert not isinstance(_validate_contents(archive, '.zip'), list)
    validate_archive(archive)


@pytest.mark.parametrize('filename', TRAVERSAL_MEMBERS)
def test_upload_validation_still_rejects_traversal_members(tmp_path, filename):
    from api.routes.upload import _validate_archive_contents
    errors = _validate_archive_contents(_archive_with(tmp_path, filename), '.zip')
    assert isinstance(errors, list) and any('不安全路径' in e for e in errors)


@pytest.mark.parametrize('filename', TRAVERSAL_MEMBERS)
def test_every_archive_gate_rejects_traversal_members(tmp_path, filename):
    from api.routes.run import _validate_contents
    from api.services.zip_handler import validate_archive
    archive = _archive_with(tmp_path, filename)
    errors = _validate_contents(archive, '.zip')
    assert isinstance(errors, list) and any('不安全路径' in e for e in errors)
    with pytest.raises(ValueError, match='unsafe path'):
        validate_archive(archive)


def test_table_extraction_searches_captioned_pages_and_the_page_after(tmp_path):
    import fitz
    from utils.pdf_table_extractor import _caption_page_indices
    path = tmp_path / "si.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_text((50, 80), "Figure S1. NMR spectrum of compound 3.")
        doc.new_page().insert_text((50, 80), "Results are summarised in Supplementary Table S2 | Summary of yields")
        doc.new_page().insert_text((50, 80), "1.0 M KOH   -0.503   1.95")
        doc.new_page().insert_text((50, 80), "Figure S4. Micrograph.")
        doc.new_page().insert_text((50, 80), "表 3 电池性能", fontname="china-s")
        doc.save(path)
    assert _caption_page_indices(str(path)) == [1, 2, 4]


def test_table_extraction_without_a_text_layer_searches_every_page(tmp_path):
    import fitz
    from utils.pdf_table_extractor import _caption_page_indices
    path = tmp_path / "scan.pdf"
    with fitz.open() as doc:
        doc.new_page().draw_rect(fitz.Rect(50, 50, 300, 300))
        doc.save(path)
    assert _caption_page_indices(str(path)) is None
