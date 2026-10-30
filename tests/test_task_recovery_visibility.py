import asyncio
from datetime import timedelta

from api.models import TaskConfig, TaskMode, TaskStatus
from api.services import task_manager


def test_isolated_recovery_status_refreshes_without_overriding_live_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(task_manager, 'TASKS_DIR', tmp_path)
    tm = task_manager.TaskManager()
    tid = tm.create_task(TaskMode.SINGLE, TaskConfig(table_name='detection_reports'))
    old = tm.get_task(tid)
    tm.update_task(tid, status=TaskStatus.FAILED)
    recovered = old.model_copy(deep=True)
    recovered.updated_at += timedelta(seconds=1)
    recovered.status = TaskStatus.COMPLETED
    tm._persist(recovered)

    async def check():
        worker = asyncio.create_task(asyncio.Event().wait())
        tm._workers[tid] = worker
        try:
            assert tm.get_task(tid).status == TaskStatus.FAILED
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert tm.get_task(tid).status == TaskStatus.COMPLETED

    asyncio.run(check())


def test_task_list_refreshes_before_status_filter_and_count(monkeypatch, tmp_path):
    monkeypatch.setattr(task_manager, 'TASKS_DIR', tmp_path)
    tm = task_manager.TaskManager()
    tid = tm.create_task(TaskMode.SINGLE, TaskConfig(table_name='detection_reports'))
    tm.update_task(tid, status=TaskStatus.FAILED)
    recovered = tm._tasks[tid].model_copy(deep=True)
    recovered.updated_at += timedelta(seconds=1)
    recovered.status = TaskStatus.COMPLETED
    tm._persist(recovered)
    assert tm.list_tasks(status_filter='failed') == ([], 0)
    items, count = tm.list_tasks(status_filter='completed')
    assert count == 1 and items[0].task_id == tid


def test_task_queries_explicitly_disable_http_caching(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import tasks
    monkeypatch.setattr(task_manager, 'TASKS_DIR', tmp_path)
    tm = task_manager.TaskManager()
    tid = tm.create_task(TaskMode.SINGLE, TaskConfig(table_name='detection_reports'))
    monkeypatch.setattr(tasks, '_task_manager', tm)
    app = FastAPI()
    app.include_router(tasks.router, prefix='/api/task')
    client = TestClient(app)
    for endpoint in ('/api/task', '/api/task/' + tid):
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
