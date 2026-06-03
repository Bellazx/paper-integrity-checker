import re
from datetime import datetime
from pathlib import Path

BASE_DIR = Path("/opt/paper-integrity-checker")
DATA_DIR = BASE_DIR / "data"
TASKS_DIR = DATA_DIR / "tasks"
UPLOADS_DIR = DATA_DIR / "uploads"
INPUT_DIR = DATA_DIR / "input"
OUTPUT_DIR = DATA_DIR / "output"
REVIEW_DIR = OUTPUT_DIR / "review_v2"

# Runtime scratch space (archive extraction, pipeline temp). Lives on the 1T
# data disk (bind-mounted /dev/vdb), NOT the full root disk where /tmp sits.
RUNTIME_TMP_DIR = DATA_DIR / "runtime_tmp"
RUNTIME_TMP_DIR.mkdir(parents=True, exist_ok=True)

MAIN_PY = BASE_DIR / "main.py"
REVIEW_REPORT_SCRIPT = Path("/opt/.claude/skills/paper-batch-review/scripts/generate_review_report.py")

DEFAULT_TABLE = "yujing_quanliang"
FORBIDDEN_TABLES = {"yujing"}
TABLE_PATTERN = re.compile(r"^yujing_[a-z0-9_]{1,50}$")

API_PORT = 8001
MAX_UPLOAD_SIZE_MB = 2048
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
MAX_CONCURRENT_TASKS = 3
MAX_BATCH_PAPERS = 10
# Unfinished tasks kept in the in-process queue. This includes running tasks and
# queued tasks waiting behind MAX_CONCURRENT_TASKS. Requests beyond this fail fast
# instead of accumulating unbounded work in memory.
MAX_PENDING_TASKS = 30

# /api/run is a legacy streaming endpoint and does not use TaskManager. Keep it
# available for administrators, but protect the host with a cross-request gate.
MAX_RUN_CONCURRENT_STREAMS = 1
MAX_REVIEW_WORKERS = 4

PYTHON = "/usr/bin/python3"

DETECT_TIMEOUT_SECONDS = 30 * 60
REVIEW_TIMEOUT_SECONDS = 46 * 60
REPORT_TIMEOUT_SECONDS = 5 * 60

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

ALLOWED_EXTENSIONS = {
    ".pdf", ".xlsx", ".xls", ".csv", ".docx",
    ".tif", ".tiff", ".png", ".jpg", ".jpeg",
    ".fcs", ".sav",
    ".txt", ".html", ".json",
}

FORBIDDEN_EXTENSIONS = {
    ".exe", ".sh", ".bat", ".cmd", ".py", ".js", ".jar",
    ".dll", ".so", ".bin", ".msi", ".com", ".vbs", ".ps1",
}


def validate_table_name(name: str) -> str:
    if name in FORBIDDEN_TABLES:
        raise ValueError(f"Table '{name}' is read-only and cannot be written to")
    if not TABLE_PATTERN.match(name):
        raise ValueError(f"Invalid table name '{name}'. Must match pattern: yujing_[a-z0-9_]{{1,50}}")
    return name


def sanitize_report_namespace(name: str) -> str:
    """Filesystem/URL-safe report namespace, normally a table name.

    Slash-separated namespaces are allowed for task isolation, e.g.
    ``detection_reports/20260603-abcd``. Each segment is sanitized separately
    so callers cannot escape the report root.
    """
    parts = []
    for part in (name or "").strip().split("/"):
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._-")
        if cleaned:
            parts.append(cleaned[:80])
    return "/".join(parts) or "default"


def _day_from_id(value: str) -> str:
    value = value or ""
    return value[:8] if re.match(r"^\d{8}", value) else datetime.now().strftime("%Y%m%d")


def date_task_dir_name(value: str = "") -> str:
    return f"{_day_from_id(value)}-task"


def upload_dir_for_file_id(file_id: str) -> Path:
    return UPLOADS_DIR / date_task_dir_name(file_id) / file_id


def resolve_upload_dir(file_id: str) -> Path:
    preferred = upload_dir_for_file_id(file_id)
    if preferred.exists():
        return preferred

    legacy = UPLOADS_DIR / file_id
    if legacy.exists():
        return legacy

    for candidate in UPLOADS_DIR.glob(f"*-task/{file_id}"):
        if candidate.exists():
            return candidate
    return preferred


def input_task_root(task_id: str) -> Path:
    return INPUT_DIR / date_task_dir_name(task_id) / task_id


def output_task_root(task_id: str) -> Path:
    return OUTPUT_DIR / "tasks" / task_id
