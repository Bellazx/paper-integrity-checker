import io
import json

import pytest

from scripts import deploy_api_when_idle as deploy


def test_isolated_recovery_prevents_restart_even_if_api_cache_is_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, 'TASKS_DIR', tmp_path)
    monkeypatch.setattr(deploy.urllib.request, 'urlopen',
                        lambda *a, **kw: io.BytesIO(b'{"total":0}'))
    task = tmp_path / 'task'; task.mkdir()
    state = task / 'status.json'
    state.write_text(json.dumps({'status': 'reviewing'}))
    assert deploy.unfinished()['reviewing'] == 1
    state.write_text(json.dumps({'status': 'completed'}))
    assert not any(deploy.unfinished().values())


def test_unreadable_disk_status_never_counts_as_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy, 'TASKS_DIR', tmp_path)
    monkeypatch.setattr(deploy.urllib.request, 'urlopen',
                        lambda *a, **kw: io.BytesIO(b'{"total":0}'))
    task = tmp_path / 'task'; task.mkdir()
    (task / 'status.json').write_text('{')
    with pytest.raises(ValueError):
        deploy.unfinished()
