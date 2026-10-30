import json

import pytest

from modules.chinese_report_generator import _make_filename, _sanitize_paper_text


@pytest.mark.parametrize('title', [
    'Temperature above 100 \x01C',
    'Flow measurement for a 4 \x01 4 rod bundle',
    'Bilateral Inhibition of \x01-Aminobutyric Acid',
    'The \x01a signal and \x01b response',
])
def test_pdf_and_db_names_match_after_render_metadata_sanitization(title):
    from utils.db import _make_report_url

    doi = '10.1000/EXAMPLE'
    generated_name = _make_filename(doi, _sanitize_paper_text(title))
    url = _make_report_url({'paper': {'doi': doi, 'title': title}}, namespace='yujing_audit')
    assert url.rsplit('/', 1)[-1] == generated_name


def test_batch_counts_pdf_generation_failure_as_failure(monkeypatch, tmp_path):
    import main

    source = tmp_path / 'input' / '10.1000_example'
    source.mkdir(parents=True)
    (source / 'paper.pdf').touch()
    output = tmp_path / 'output'
    findings = {
        'paper': {'doi': '10.1000/example', 'title': 'Example'},
        'summary': {'total_issues': 0, 'high_severity': 0},
        'pdf_generated': False, 'pdf_skipped_low_risk': False,
    }
    monkeypatch.setattr(main, '_analyze_one', lambda *args, **kwargs: findings)
    writes = []
    monkeypatch.setattr(main, 'insert_findings', lambda *args, **kwargs: writes.append(args))
    main.run_batch(str(source.parent), str(output), force=True, isolate_complex=False,
                   max_workers=1, summary_tag='test')
    summary = json.loads((output / 'batch_summary_test.json').read_text())
    assert summary['success'] == 0
    assert summary['failed'] == 1
    assert summary['results'][0]['status'] == 'error'
    assert 'PDF generation failed' in summary['results'][0]['error']
    assert not writes


def test_single_does_not_succeed_when_pdf_generation_fails(monkeypatch, tmp_path):
    import main

    findings = {'pdf_generated': False, 'pdf_skipped_low_risk': False}
    monkeypatch.setattr(main, '_analyze_one', lambda *args, **kwargs: findings)
    monkeypatch.setattr(main, 'find_batch_excel', lambda *args: None)
    writes = []
    monkeypatch.setattr(main, 'insert_findings', lambda *args, **kwargs: writes.append(args))
    with pytest.raises(RuntimeError, match='PDF generation failed'):
        main.run_single(str(tmp_path / 'input'), str(tmp_path / 'output'))
    assert not writes


def test_scholar_rejects_failed_pdf_even_if_old_public_report_exists(monkeypatch, tmp_path):
    from scripts import run_scholar_batch as batch

    source = tmp_path / 'input' / '10.1000_example'
    source.mkdir(parents=True)
    output = tmp_path / 'output'
    (output / 'chinese_reports').mkdir(parents=True)
    findings = {'paper': {'doi': '10.1000/example', 'title': 'Example'}, 'pdf_generated': False}
    old = output / 'chinese_reports' / batch._make_filename('10.1000/example', 'Example')
    old.write_bytes(b'old public report')
    monkeypatch.setattr(batch, 'analyze_paper', lambda *a, **kw: findings)
    writes = []
    monkeypatch.setattr(batch, '_upsert_initial', lambda row: writes.append(row))
    results = batch.run_initial(source.parent, output, 'Example Scholar', '',
                                {source.name: '10.1000/example'}, 1,
                                paper_dirs=[source], summary_tag='shard01')
    assert results[0]['status'] == 'failed'
    assert 'PDF generation failed' in results[0]['error']
    assert not writes
    assert (output / 'screening_results_Example Scholar_shard01.json').exists()
