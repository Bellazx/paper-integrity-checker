"""Crash-isolated source-data screening for complex papers."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DATA_SUFFIXES = {".xlsx", ".xls", ".csv", ".txt", ".tsv", ".docx", ".fcs", ".sav", ".zip", ".rar", ".pdb", ".cif"}
ISOLATION_BYTES = int(os.getenv("PAPER_DATA_ISOLATION_BYTES", str(8 * 1024 * 1024)))
ISOLATION_FILE_COUNT = int(os.getenv("PAPER_DATA_ISOLATION_FILE_COUNT", "5"))
CHECK_TIMEOUT_SECONDS = int(os.getenv("PAPER_DATA_CHECK_TIMEOUT", "900"))


def _files(data_dirs: str | Path | list[str | Path]) -> list[Path]:
    roots = [Path(data_dirs)] if not isinstance(data_dirs, list) else [Path(d) for d in data_dirs]
    result = []
    for root in roots:
        if not root.exists():
            continue
        result.extend(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in DATA_SUFFIXES
        )
    return result


def _needs_isolation(data_dirs: str | Path | list[str | Path]) -> tuple[bool, int, int]:
    files = _files(data_dirs)
    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue
    isolated = len(files) >= ISOLATION_FILE_COUNT or total_bytes >= ISOLATION_BYTES
    return isolated, len(files), total_bytes


def check_data_anomalies_safe(data_dirs: str | Path | list[str | Path]) -> tuple[list[dict], list[str]]:
    """Return anomalies and warnings, isolating unusually complex source data."""
    isolated, file_count, total_bytes = _needs_isolation(data_dirs)
    if not isolated:
        from modules.data_checker import check_data_with_validation
        issues, failed = check_data_with_validation(data_dirs)
        return issues, [f"source-data coverage incomplete: {item}" for item in failed]

    warning = (
        f"source-data checks isolated ({file_count} files, "
        f"{total_bytes / (1024 * 1024):.1f} MiB)"
    )
    log.info("Complex source data detected; %s", warning)
    with tempfile.TemporaryDirectory(prefix="paper_data_check_") as tmp:
        result_path = Path(tmp) / "anomalies.json"
        worker = Path(__file__).resolve().parent.parent / "scripts" / "data_checker_worker.py"
        dirs = [str(data_dirs)] if not isinstance(data_dirs, list) else [str(d) for d in data_dirs]
        cmd = [sys.executable, str(worker), "--output", str(result_path), *dirs]
        env = os.environ.copy()
        for key in (
            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
        ):
            env[key] = "1"
        try:
            completed = subprocess.run(
                cmd,
                cwd=str(worker.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=CHECK_TIMEOUT_SECONDS,
                env=env,
                check=False,
                text=True,
            )
        except subprocess.TimeoutExpired:
            log.warning("Isolated source-data check timed out after %ss", CHECK_TIMEOUT_SECONDS)
            return [], [warning + "; timed out"]

        if completed.returncode != 0:
            if completed.returncode < 0:
                reason = f"signal {-completed.returncode}"
            else:
                reason = f"exit code {completed.returncode}"
            log.warning("Isolated source-data check failed (%s); continuing paper analysis", reason)
            return [], [warning + f"; worker {reason}"]
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or not isinstance(result.get("anomalies"), list):
                raise ValueError("invalid worker result")
            return result["anomalies"], [warning, *[
                f"source-data coverage incomplete: {item}" for item in result.get("warnings", [])
            ]]
        except (OSError, ValueError) as exc:
            log.warning("Isolated source-data result unavailable: %s", exc)
            return [], [warning + "; result unavailable"]
