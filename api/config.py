import os
import re
import sys
from datetime import datetime
from pathlib import Path

if sys.version_info[:2] != (3, 12):
    raise RuntimeError(
        "Paper Pecker requires Python 3.12; "
        f"got {sys.version_info.major}.{sys.version_info.minor} ({sys.executable})"
    )

BASE_DIR = Path(
    os.environ.get("PAPER_INTEGRITY_ROOT", Path(__file__).resolve().parents[1])
).resolve()
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


def _default_review_skill_scripts() -> Path:
    candidates = (
        Path("/opt/.claude/skills/paper-batch-review/scripts"),
        Path("/home/library/.claude/skills/paper-batch-review/scripts"),
        Path.home() / ".claude" / "skills" / "paper-batch-review" / "scripts",
    )
    return next((path for path in candidates if path.is_dir()), candidates[1])


REVIEW_SKILL_SCRIPTS = Path(
    os.environ.get("PAPER_REVIEW_SKILL_SCRIPTS") or _default_review_skill_scripts()
)
_PROJECT_REVIEW_REPORT_SCRIPT = BASE_DIR / "scripts" / "generate_review_report.py"
REVIEW_REPORT_SCRIPT = Path(os.environ.get(
    "PAPER_REVIEW_REPORT_SCRIPT",
    _PROJECT_REVIEW_REPORT_SCRIPT
    if _PROJECT_REVIEW_REPORT_SCRIPT.is_file()
    else REVIEW_SKILL_SCRIPTS / "generate_review_report.py",
))

DEFAULT_TABLE = "yujing_quanliang"
FORBIDDEN_TABLES = {"yujing"}
TABLE_PATTERN = re.compile(r"^yujing_[a-z0-9_]{1,50}$")

API_PORT = 8001
MAX_UPLOAD_SIZE_MB = 2048
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
MAX_EXTRACTED_SIZE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 50000
MAX_CONCURRENT_TASKS = 3
# User-facing uploads have a dedicated budget, independent of the legacy
# developer/batch API. Cap work across all batches as well as task admission.
API_MAX_CONCURRENT_TASKS = int(os.environ.get("API_MAX_CONCURRENT_TASKS", "120"))
API_MAX_CONCURRENT_PAPERS = int(os.environ.get("API_MAX_CONCURRENT_PAPERS", "120"))
if API_MAX_CONCURRENT_TASKS < 1 or API_MAX_CONCURRENT_PAPERS < 1:
    raise ValueError("API concurrency limits must be positive")
MAX_BATCH_PAPERS = 10
# Unfinished tasks kept in the in-process queue. This includes running tasks and
# queued tasks waiting behind MAX_CONCURRENT_TASKS. Requests beyond this fail fast
# instead of accumulating unbounded work in memory.
MAX_PENDING_TASKS = int(os.environ.get("API_MAX_PENDING_TASKS", "240"))
if MAX_PENDING_TASKS < API_MAX_CONCURRENT_TASKS:
    raise ValueError("API_MAX_PENDING_TASKS must allow the full API concurrency budget")

# /api/run is a legacy streaming endpoint and does not use TaskManager. Keep it
# available for administrators, but protect the host with a cross-request gate.
MAX_RUN_CONCURRENT_STREAMS = 1
MAX_REVIEW_WORKERS = 4

_PROJECT_VENV_PYTHON = BASE_DIR / "venv" / "bin" / "python"
PYTHON = os.environ.get(
    "PAPER_CHECKER_PYTHON",
    str(_PROJECT_VENV_PYTHON) if _PROJECT_VENV_PYTHON.is_file() else sys.executable,
)

DETECT_TIMEOUT_SECONDS = 30 * 60
# A full review may need to render several figures and inspect large source
# workbooks.  Thirty minutes was too short for some legitimate reviews and
# caused an avoidable retry.  Keep the limit configurable for constrained
# deployments; the review code still kills and reaps the child on timeout.
REVIEW_TIMEOUT_SECONDS = int(os.environ.get("REVIEW_TIMEOUT_SECONDS", str(90 * 60)))
REPORT_TIMEOUT_SECONDS = 15 * 60

DB_CONFIG = {
    "login_timeout": 10,
    "timeout": 60,
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

ALLOWED_EXTENSIONS = {
    ".pdf", ".xlsx", ".xls", ".csv", ".docx",
    ".tif", ".tiff", ".png", ".jpg", ".jpeg",
    ".fcs", ".sav", ".pdb", ".cif",
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
    if not isinstance(file_id, str) or not re.fullmatch(r'[\w-]{1,160}', file_id):
        raise ValueError('Invalid file_id')
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
