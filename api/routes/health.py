import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.config import DB_CONFIG
from api.services.api_capacity import capacity_status
from api.services.review_validation import PUBLICATION_POLICY_VERSION

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "paper-integrity-checker", "version": "1.0.0",
            "review_policy_version": PUBLICATION_POLICY_VERSION,
            "api_capacity": capacity_status()}


def _database_status():
    db_status = "connected"
    try:
        import pymssql
        conn = pymssql.connect(**DB_CONFIG)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        finally:
            conn.close()
    except Exception as e:
        db_status = f"error: {e}"

    return db_status


@router.get("/health/detailed")
async def health_detailed():
    from api.services.runtime_tools import runtime_tool_status

    db_status, tools = await asyncio.gather(
        asyncio.to_thread(_database_status), asyncio.to_thread(runtime_tool_status))
    return {
        "status": "ok" if db_status == "connected" and tools["ok"] else "degraded",
        "service": "paper-integrity-checker",
        "version": "1.0.0",
        "review_policy_version": PUBLICATION_POLICY_VERSION,
        "db": db_status,
        "tools": tools,
        "api_capacity": capacity_status(),
    }


@router.get("/health/deep")
async def health_deep():
    """Checks the service as it runs: on-disk code imports, process not older
    than the sources, database, disk, recent task failures. 503 when requests
    cannot be served, so an uptime monitor fails over on it too."""
    from api.services.deep_health import deep_status

    report = await asyncio.to_thread(deep_status)
    return JSONResponse(report, status_code=503 if report["status"] == "critical" else 200)
