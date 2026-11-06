import asyncio
import datetime as dt
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api.models import PaperInfo
from api.services import detection_reports_worker as worker, report
from api.services.api_capacity import WorkLimiter
from api.services.review_runtime import api_review_budget
from review_v3 import config, engine
from review_v3.workbook_cache import read_sheet


@pytest.mark.parametrize("language,expected", [("zh", {"zh"}), ("en", {"en"}), ("both", {"zh", "en"})])
def test_review_languages_overlap_and_share_deadline(tmp_path, monkeypatch, language, expected):
    async def run():
        entered = set()
        ready = asyncio.Event()
        deadline = time.monotonic() + 20

        async def render(lang, **kwargs):
            assert kwargs['deadline'] == deadline
            entered.add(lang)
            if entered == expected:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)  # serial execution would deadlock
            return ["zh.pdf"] if lang == "zh" else "en.pdf"

        async def zh(*args, **kwargs):
            return await render("zh", **kwargs)

        async def en(*args, **kwargs):
            return await render("en", **kwargs)

        monkeypatch.setattr(worker, "generate_reports", zh)
        monkeypatch.setattr(worker, "generate_english_report", en)
        monkeypatch.setattr(worker, "api_work", WorkLimiter(2))
        paper = PaperInfo(doi_slug="paper", language=language)
        result = await worker._render_review_pair(paper, {'_report_namespace': 'test'}, tmp_path, deadline)
        assert set(result) == expected
        assert all(not isinstance(value, Exception) for value in result.values())
    asyncio.run(run())


def test_expired_queue_does_not_launch_or_leak_a_slot():
    async def run():
        limiter = WorkLimiter(1)
        await limiter._semaphore.acquire()
        operation = AsyncMock()
        with pytest.raises(TimeoutError):
            await limiter.run_until(time.monotonic() + .02, operation)
        assert not limiter.waiting and not limiter.active
        operation.assert_not_awaited()
        limiter._semaphore.release()
        await limiter.run(operation)
        operation.assert_awaited_once()
    asyncio.run(run())


def test_english_timeout_kills_the_child_before_it_can_publish(tmp_path, monkeypatch):
    # Real subprocess, no LLM: a delayed writer must never leave a late PDF.
    root = tmp_path / 'root'
    (root / 'scripts').mkdir(parents=True)
    output = tmp_path / 'late.pdf'
    (root / 'scripts' / 'generate_english_report.py').write_text(
        f'import time\nfrom pathlib import Path\ntime.sleep(.5)\nPath({str(output)!r}).write_bytes(b"%PDF-late")\n')
    monkeypatch.setattr(report, 'BASE_DIR', root)

    async def run():
        with pytest.raises(TimeoutError):
            await report.generate_english_report({}, tmp_path, tmp_path / 'task', kind='initial',
                                                 deadline=time.monotonic() + .1)
        await asyncio.sleep(.6)
        assert not output.exists()
    asyncio.run(run())


@pytest.mark.parametrize('language', ['en', 'both'])
def test_initial_reports_only_request_selected_languages(tmp_path, monkeypatch, language):
    from modules.initial_report_bundle import generate_initial_pdfs
    monkeypatch.setenv('PAPER_INITIAL_REPORT_LANGUAGE', language)
    monkeypatch.setenv('PAPER_ENGLISH_REPORT_DIR', str(tmp_path))
    barrier = threading.Barrier(2) if language == 'both' else None
    seen = []

    def english(value, output):
        seen.append('en')
        assert value['paper']['title'] == 'original'
        if barrier:
            barrier.wait(timeout=2)
        path = tmp_path / 'en.pdf'
        path.write_bytes(b'%PDF-en')
        return str(path)

    def chinese(value, output, text):
        seen.append('zh')
        barrier.wait(timeout=2)
        value['paper']['title'] = 'mutation only in Chinese copy'
        return str(tmp_path / 'zh.pdf'), {}

    monkeypatch.setattr('modules.english_report_generator.generate_english_initial_pdf', english)
    findings = {'paper': {'title': 'original'}}
    generate_initial_pdfs(findings, tmp_path, chinese_renderer=chinese)
    assert set(seen) == ({'en'} if language == 'en' else {'zh', 'en'})
    assert findings['_initial_report_languages'] == (['en'] if language == 'en' else ['zh', 'en'])


def test_api_budget_keeps_late_phases_until_one_review_deadline(tmp_path):
    review = engine.Review('x', '', str(tmp_path), tmp_path, time.monotonic() + 200, None)
    token = api_review_budget.set(True)
    try:
        for phase in ('investigate', 'complete', 'judge', 'evidence'):
            assert review._hard_deadline(phase) == review.deadline
        # Calibration alone stops early so the judge still gets its turn.
        assert review._hard_deadline('calibrate') == review.deadline - config.JUDGE_RESERVE_SECONDS
    finally:
        api_review_budget.reset(token)
    assert engine._required_finding('image::visual_pass')


def test_workbook_cache_preserves_values_and_invalidates_edits(tmp_path, monkeypatch):
    import openpyxl
    path, cache = tmp_path / 'book.xlsx', tmp_path / 'cache'
    book = openpyxl.Workbook()
    book.active.append(['name', 'number', 'date', 'flag', 'empty'])
    book.active.append(['sample', 1.7005281876211287, dt.datetime(2026, 11, 6, 9, 30), True, None])
    book.save(path)
    names, rows = read_sheet(path, 'Sheet', cache)
    cold = list(rows)
    loader = openpyxl.load_workbook
    monkeypatch.setattr(openpyxl, 'load_workbook', lambda *a, **k: pytest.fail('warm cache reparsed workbook'))
    assert list(read_sheet(path, 'Sheet', cache)[1]) == cold
    monkeypatch.setattr(openpyxl, 'load_workbook', loader)
    book.active['B2'] = 2.5
    book.save(path)
    assert list(read_sheet(path, 'Sheet', cache)[1])[1][1] == 2.5


def test_short_sheet_preview_never_publishes_partial_cache(tmp_path):
    import openpyxl
    path, cache = tmp_path / 'book.xlsx', tmp_path / 'cache'
    book = openpyxl.Workbook()
    for i in range(5):
        book.active.append([i])
    book.save(path)
    _, rows = read_sheet(path, 'Sheet', cache)
    assert next(rows) == (0,)
    rows.close()
    assert not list(cache.iterdir())
    assert len(list(read_sheet(path, 'Sheet', cache)[1])) == 5


def test_deadline_recovery_keeps_saved_capture_without_changing_verdict(tmp_path, monkeypatch):
    from review_v3 import ledger
    record = {'id': 'high::1', 'status': 'issue', 'severity': 'substantive', 'dimension': 'data'}
    review = engine.Review('x', '', str(tmp_path), tmp_path, None, None)
    monkeypatch.setattr(review, 'merge', lambda: ({'high::1': dict(record)}, []))
    crop = tmp_path / 'main_p001_c20_30_300_400_d150.png'
    crop.write_bytes(b'png')
    ev = {'path': str(crop), 'caption': 'Fig. 1 原文截图'}
    (tmp_path / 'capture_checkpoint.json').write_text(json.dumps({'high::1': [ev]}))
    result = engine._recorded_findings(review, tmp_path)
    assert result['high::1']['evidence'] == [ev]
    assert result['high::1']['severity'] == 'substantive'


@pytest.mark.parametrize('language,english_fails', [('en', False), ('both', False), ('both', True)])
def test_worker_requires_all_requested_pdfs_and_hides_unrequested_urls(tmp_path, monkeypatch, language, english_fails):
    from tests.test_detection_reports_state import _TaskCapture
    output = tmp_path / 'input-results'
    output.mkdir()
    tid = 'language-task'
    from modules.english_report_generator import initial_report_filename
    findings = {'paper': {'doi': '10.1/test', 'title': 'Test'}, 'summary': {},
                'pdf_generated': True}
    initial_english = tmp_path / 'english_reports' / 'detection_reports' / tid / initial_report_filename(findings)
    initial_english.parent.mkdir(parents=True)
    initial_english.write_bytes(b'%PDF-initial')
    findings['_english_initial_pdf'] = str(initial_english)
    (output / 'report.json').write_text(json.dumps(findings))
    paper = PaperInfo(doi='10.1/test', doi_slug='10.1_test', input_dir=str(tmp_path),
                      output_dir=str(output), submission_no='submission', language=language)
    zh_path = tmp_path / 'review' / 'detection_reports' / tid / 'review_10.1_test.pdf'
    zh = AsyncMock(return_value=[str(zh_path)])
    en = AsyncMock(return_value=str(zh_path.with_name('review_10.1_test_en.pdf')),
                   side_effect=TimeoutError('English deadline') if english_fails else None)
    monkeypatch.setattr(worker, 'TASKS_DIR', tmp_path / 'tasks')
    monkeypatch.setattr(worker, 'OUTPUT_DIR', tmp_path)
    monkeypatch.setattr(worker, 'REVIEW_DIR', tmp_path / 'review')
    monkeypatch.setattr(worker, 'api_work', WorkLimiter(2))
    monkeypatch.setattr(worker, 'run_detection_single', AsyncMock(return_value=findings))
    monkeypatch.setattr(worker, 'run_review_single', AsyncMock(return_value={
        'doi': paper.doi, 'result': '高风险', 'trigger': 'review'}))
    monkeypatch.setattr(worker, 'generate_reports', zh)
    monkeypatch.setattr(worker, 'generate_english_report', en)
    monkeypatch.setattr(worker, 'verify_public_pdf', AsyncMock())
    monkeypatch.setattr(worker, 'upsert_chushen', lambda **kw: None)
    monkeypatch.setattr(worker, 'verify_chushen_write', lambda **kw: (True, ''))
    monkeypatch.setattr(worker, 'update_review_result', lambda **kw: True)
    monkeypatch.setattr(worker, 'update_review', lambda **kw: True)
    monkeypatch.setattr(worker, 'verify_review_write', lambda **kw: (True, ''))
    monkeypatch.setattr(worker, 'update_english_reports', lambda *a, **kw: None)
    monkeypatch.setattr('modules.chinese_report_generator._compute_overall_risk', lambda _: {'level': '高风险'})
    capture = _TaskCapture()
    asyncio.run(worker._run_inner(tid, capture, [paper], {'max_workers': 1}))
    record = capture.updates[-1]['result']['detection_reports'][0]
    assert record['processing_completed'] is (not english_fails)
    assert zh.await_count == (1 if language == 'both' else 0)
    assert en.await_count == 1  # initial sibling was reused, not translated again
    if language == 'en':
        assert record['chushen_report_url'] is None and record['review_report_url'] is None
        assert record['english_chushen_report_url'] and record['english_review_report_url']
    if english_fails:
        assert record['review_result'] is None


def test_english_decimal_loss_is_repaired_or_fails_explicitly(monkeypatch):
    from modules import english_report_generator as english
    source = {'issue': '压力为 12.5 kPa；值为 1.7000。'}
    incomplete = {'issue': 'Pressure; value 1.7000.'}
    monkeypatch.setattr(english, 'chat', lambda *a, **kw: json.dumps({'issue': 'Pressure 12.5 kPa; value 1.7000.'}))
    assert '12.5' in english._preserve_decimal_values(source, incomplete)['issue']
    monkeypatch.setattr(english, 'chat', lambda *a, **kw: json.dumps(incomplete))
    with pytest.raises(ValueError, match='omitted source values'):
        english._preserve_decimal_values(source, incomplete)
