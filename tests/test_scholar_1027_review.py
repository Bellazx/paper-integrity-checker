import asyncio

import pytest

from api.services import review as engine
from scripts import run_scholar_1027_review as runner


def test_repeated_db_polls_use_fresh_connections(monkeypatch):
    import pymssql
    connections = []

    class Connection:
        closed = False

        def cursor(self, **kwargs):
            assert not self.closed
            return self

        def execute(self, *args):
            assert not self.closed

        def fetchall(self):
            return []

        def close(self):
            self.closed = True

    def connect(**kwargs):
        connection = Connection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(pymssql, 'connect', connect)
    assert runner.rows() == {}
    assert runner.rows() == {}
    assert len(connections) == 2
    assert all(c.closed for c in connections)


@pytest.mark.parametrize('risk, expected', [
    ('低风险', ['first', 'second', 'judge']),
    ('高风险', ['first', 'judge']),
])
def test_every_paper_reaches_judge(tmp_path, monkeypatch, risk, expected):
    calls = []
    bundle = tmp_path / 'evidence.json'
    runner.save(bundle, {'coverage_manifest': {}})
    monkeypatch.setattr(engine, '_ensure_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(engine, '_ensure_llm_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(engine, '_validate_review_result', lambda d, r, i, result, e: result)
    monkeypatch.setattr(engine, '_clean_internal_references', dict)

    async def first(*a, **k):
        calls.append('first')
        return {'result': risk, 'doi': '10.test/a'}

    async def second(*a, **k):
        calls.append('second')
        return {'result': risk, 'doi': '10.test/a'}

    async def judge(*a, **k):
        calls.append('judge')
        return {'result': risk, 'doi': '10.test/a'}

    monkeypatch.setattr(engine, '_run_with_retry', first)
    monkeypatch.setattr(engine, '_run_lowrisk_verifier_with_retry', second)
    monkeypatch.setattr(engine, '_run_judge', judge)
    final = asyncio.run(runner.review({'doi': '10.test/a', 'source': str(tmp_path)}, tmp_path))
    assert final['result'] == risk
    assert calls == expected
    assert runner.load(tmp_path / 'results.json')[0]['_evidence_path'] == str(bundle)


def test_incomplete_review_never_becomes_final(tmp_path, monkeypatch):
    bundle = tmp_path / 'evidence.json'
    runner.save(bundle, {'coverage_manifest': {}})
    monkeypatch.setattr(engine, '_ensure_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(engine, '_ensure_llm_evidence_bundle', lambda *a: str(bundle))
    monkeypatch.setattr(engine, '_validate_review_result',
                        lambda d, r, i, result, e: {**result, '_coverage_gaps': ['unreviewed image']})

    async def high(*a, **k):
        return {'result': '高风险', 'doi': '10.test/a'}

    monkeypatch.setattr(engine, '_run_with_retry', high)
    monkeypatch.setattr(engine, '_run_judge', high)
    with pytest.raises(RuntimeError, match='incomplete'):
        asyncio.run(runner.review({'doi': '10.test/a', 'source': str(tmp_path)}, tmp_path))
    assert not (tmp_path / 'results.json').exists()
