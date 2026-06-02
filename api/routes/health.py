from fastapi import APIRouter

from api.config import DB_CONFIG

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "paper-integrity-checker", "version": "1.0.0"}


@router.get("/health/detailed")
async def health_detailed():
    db_status = "connected"
    try:
        import pymssql
        conn = pymssql.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        conn.close()
    except Exception as e:
        db_status = f"error: {e}"

    from api.services.task_manager import TaskManager
    return {
        "status": "ok",
        "service": "paper-integrity-checker",
        "version": "1.0.0",
        "db": db_status,
    }
