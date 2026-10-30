from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import openpyxl

from api.services.review import _claude_subprocess_env


def test_claude_subprocess_env_disables_maintenance_traffic() -> None:
    env = _claude_subprocess_env()

    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["PAPER_REVIEW_GUARD_READONLY_CELL"] == "1"
    assert any(Path(item).name == "reviewer_runtime" for item in env["PYTHONPATH"].split(os.pathsep))


def test_readonly_cell_guard_rejects_quadratic_access(tmp_path: Path) -> None:
    workbook_path = tmp_path / "wide.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for _ in range(100):
        sheet.append([1])
    workbook.save(workbook_path)

    env = _claude_subprocess_env()
    env["PAPER_REVIEW_READONLY_CELL_LIMIT"] = "256"
    env["PAPER_REVIEW_READONLY_CELL_ROW_COST_LIMIT"] = "100"
    script = f"""
import openpyxl
sheet = openpyxl.load_workbook({str(workbook_path)!r}, read_only=True).active
assert sheet.cell(40, 1).value == 1
assert sheet.cell(50, 1).value == 1
try:
    sheet.cell(20, 1)
except RuntimeError as exc:
    assert 'iter_rows' in str(exc)
else:
    raise AssertionError('read-only cell scan guard did not activate')
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
