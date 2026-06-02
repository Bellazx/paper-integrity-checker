import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.config import API_PORT
from api.routes import health, tasks, upload, run, detection
from api.services.task_manager import TaskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("paper-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tm = TaskManager()
    tm.load_from_disk()
    tasks.set_task_manager(tm)
    log.info("Task manager loaded, %d existing tasks", len(tm._tasks))
    yield
    log.info("Shutting down paper-api")


app = FastAPI(
    title="Paper Integrity Checker API",
    description="Upload → Detection → AI Review pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["Health"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(tasks.router, prefix="/api/task", tags=["Tasks"])
app.include_router(detection.router, prefix="/api/detection", tags=["Detection"])
app.include_router(run.router, prefix="/api", tags=["Run"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
