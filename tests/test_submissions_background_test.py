import asyncio
import errno
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts import test_paper_submissions as runner


def test_network_guard_must_be_policy_denial_not_unreachable(monkeypatch):
    monkeypatch.setattr(runner, 'check_guard_cgroup', lambda: None)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, addr): raise OSError(errno.EACCES, 'policy denied')
    monkeypatch.setattr(runner.socket, 'socket', Connection)
    assert runner.check_database_is_blocked()['database_network_blocked']
    monkeypatch.setattr(Connection, 'connect', lambda *a: None)
    with pytest.raises(RuntimeError, match='reachable'):
        runner.check_database_is_blocked()
    def unreachable(*args): raise OSError(errno.ECONNREFUSED, 'refused')
    monkeypatch.setattr(Connection, 'connect', unreachable)
    with pytest.raises(RuntimeError, match='policy'):
        runner.check_database_is_blocked()


@pytest.mark.parametrize('risk', ['高风险', '低风险'])
def test_offline_pipeline_never_enables_db_and_preserves_missing_material(monkeypatch, tmp_path, risk):
    from api.models import PaperInfo
    from api import config
    from api.services import detect, review, report, zip_handler
    from modules import english_report_generator as english
    project = tmp_path / 'project'; project.mkdir()
    monkeypatch.setattr(runner, 'ROOT', project)
    monkeypatch.setattr(config, 'OUTPUT_DIR', project / 'data/output')
    monkeypatch.setattr(config, 'REVIEW_DIR', project / 'data/output/review_v2')
    archive = tmp_path / 'upload.zip'; archive.write_bytes(b'archive')
    runner.save(tmp_path / 'manifest.json', {'jobs': [
        {'id': 1, 'submission_no': 'one', 'status': 'queued', 'sub_mode': 'single',
         'archive': str(archive), 'archive_sha256': runner.sha256(archive)},
        {'id': 2, 'submission_no': 'two', 'status': 'missing_material', 'error': 'no original'},
    ]})
    monkeypatch.setattr(runner, 'check_database_is_blocked', lambda: {'database_network_blocked': True})
    monkeypatch.setattr(runner, 'validate_pdf', lambda p: {'path': str(p)})
    paper = PaperInfo(doi='10.1000/test', doi_slug='paper', input_dir=str(tmp_path), output_dir=str(tmp_path / 'out'))
    monkeypatch.setattr(zip_handler, 'extract_single', lambda *a: [paper])
    async def detector(*a, **kw):
        assert kw['write_db'] is False
        output = config.OUTPUT_DIR / 'chinese_reports' / kw['report_namespace']
        output.mkdir(parents=True); (output / 'initial.pdf').touch()
        return {'paper': {'doi': paper.doi}, 'pdf_generated': True}
    monkeypatch.setattr(detect, 'run_detection_single', detector)
    monkeypatch.setattr('modules.chinese_report_generator._compute_overall_risk', lambda f: {'level': risk})
    reviewer = AsyncMock(return_value={'doi': paper.doi, 'result': '高风险'})
    monkeypatch.setattr(review, 'run_review_single', reviewer)
    async def renderer(*a, **kw):
        assert kw['write_db'] is False
        return ['review.pdf']
    monkeypatch.setattr(report, 'generate_reports', renderer)
    monkeypatch.setattr(english, 'generate_english_initial_pdf', lambda *a: 'initial-en.pdf')
    monkeypatch.setattr(english, 'generate_english_review_pdf', lambda *a: 'review-en.pdf')
    asyncio.run(runner.run(tmp_path, 54))
    progress = json.loads((tmp_path / 'progress.json').read_text())
    assert progress['counts'] == {'completed': 1, 'missing_material': 1}
    assert progress['database_writes_enabled'] is False
    assert reviewer.await_count == (1 if risk == '高风险' else 0)
    if reviewer.await_count:
        assert reviewer.call_args.kwargs['resume'] is False
