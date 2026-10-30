"""Runtime dependency checks for the API service.

Python's ``rarfile`` package only parses RAR metadata itself; extracting a member
still needs an external backend.  Keep that easy-to-miss dependency visible in
the detailed health endpoint and fail with an actionable message at upload time.
"""
from __future__ import annotations

import importlib.metadata
import os
import shutil
from pathlib import Path

import rarfile

from api.config import (
    INPUT_DIR,
    OUTPUT_DIR,
    PYTHON,
    REVIEW_REPORT_SCRIPT,
    REVIEW_SKILL_SCRIPTS,
    RUNTIME_TMP_DIR,
    UPLOADS_DIR,
)


def _rar_backend() -> dict[str, str | bool]:
    try:
        # Failed discovery is not cached by rarfile, so a backend installed while
        # the API is running is picked up on the next check without resetting the
        # shared successful setup used by concurrent extractions.
        setup = rarfile.tool_setup()
        token = setup.setup["check_cmd"][0]
        command = getattr(rarfile, token, str(token))
        return {
            "ok": True,
            "backend": str(command),
            "path": shutil.which(str(command)) or str(command),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": "install one of: 7zz, 7z, unrar, unar, bsdtar",
        }


def require_rar_tool() -> dict[str, str | bool]:
    """Return the usable RAR backend or raise a user-facing configuration error."""
    status = _rar_backend()
    if not status["ok"]:
        raise RuntimeError(
            "RAR 解压工具不可用；请安装并配置 7zz、7z、unrar、unar 或 bsdtar"
        )
    return status


def _path_status(path: Path, *, executable: bool = False, writable: bool = False) -> dict:
    path = Path(path)
    ok = path.is_file() if executable else path.exists()
    if executable:
        ok = ok and os.access(path, os.X_OK)
    if writable:
        ok = ok and path.is_dir() and os.access(path, os.W_OK | os.X_OK)
    return {"ok": bool(ok), "path": str(path)}


def runtime_tool_status() -> dict:
    """Check every non-network runtime component used by the API pipeline."""
    try:
        from api.services.review import _find_claude_cli

        claude = _path_status(Path(_find_claude_cli()), executable=True)
    except Exception as exc:
        claude = {"ok": False, "error": str(exc)}

    packages = {}
    for package in (
        "PyMuPDF",
        "opencv-contrib-python",
        "pandas",
        "openpyxl",
        "xlrd",
        "python-docx",
        "pyreadstat",
        "fcsparser",
        "gemmi",
        "rarfile",
        "pymssql",
    ):
        try:
            packages[package] = {"ok": True, "version": importlib.metadata.version(package)}
        except importlib.metadata.PackageNotFoundError:
            packages[package] = {"ok": False, "error": "not installed"}

    checks = {
        "rar_extractor": _rar_backend(),
        "python": _path_status(Path(PYTHON), executable=True),
        "claude_cli": claude,
        "review_evidence_script": _path_status(REVIEW_SKILL_SCRIPTS / "review_evidence.py"),
        "review_report_script": _path_status(REVIEW_REPORT_SCRIPT),
        "uploads_dir": _path_status(UPLOADS_DIR, writable=True),
        "input_dir": _path_status(INPUT_DIR, writable=True),
        "output_dir": _path_status(OUTPUT_DIR, writable=True),
        "runtime_tmp_dir": _path_status(RUNTIME_TMP_DIR, writable=True),
        "packages": packages,
    }
    ok = all(
        value.get("ok", False)
        for key, value in checks.items()
        if key != "packages"
    ) and all(value["ok"] for value in packages.values())
    return {"ok": ok, **checks}
