"""Read-only dashboard served separately so active review jobs need no restart."""
from pathlib import Path
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from api.config import TASKS_DIR
from api.models import TaskRecord
from api.services.task_progress import build_task_progress

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
PAGE = Path(__file__).resolve().parents[1] / 'web/task-progress.html'


@app.get('/')
def dashboard():
    return FileResponse(PAGE, headers={'Cache-Control': 'no-store'})


@app.get('/api/task/{task_id}/progress')
def progress(task_id: str):
    if not re.fullmatch(r'\d{8}-[a-f0-9]{8}', task_id):
        raise HTTPException(400, '任务号格式不正确')
    path = TASKS_DIR / task_id / 'status.json'
    if not path.is_file():
        raise HTTPException(404, '任务不存在')
    try:
        task = TaskRecord.model_validate_json(path.read_text())
    except (OSError, ValueError):
        raise HTTPException(503, '暂时无法读取任务进度，请稍后重试')
    return JSONResponse(build_task_progress(task), headers={'Cache-Control': 'no-store'})
