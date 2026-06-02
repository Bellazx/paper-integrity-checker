from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from api.config import (
    PYTHON, REVIEW_REPORT_SCRIPT, REVIEW_DIR,
    DEFAULT_TABLE, REPORT_TIMEOUT_SECONDS, sanitize_report_namespace,
)

log = logging.getLogger(__name__)


async def generate_reports(
    review_results: list[dict],
    task_dir: Path,
    table_name: str = DEFAULT_TABLE,
    write_db: bool = True,
    report_namespace: str | None = None,
) -> list[str]:
    results_path = task_dir / "review_results.json"
    results_path.write_text(json.dumps(review_results, ensure_ascii=False, indent=2))

    namespace = sanitize_report_namespace(report_namespace or table_name)
    output_dir = REVIEW_DIR / namespace
    output_dir.mkdir(parents=True, exist_ok=True)
    from modules.chinese_report_generator import doi_to_slug
    expected = {}
    for r in review_results:
        slug = doi_to_slug(r.get("doi") or "")
        if not slug:
            continue
        p = output_dir / f"review_{slug}.pdf"
        expected[str(p)] = p.stat().st_mtime_ns if p.exists() else None

    cmd = [
        str(PYTHON), str(REVIEW_REPORT_SCRIPT),
        "--results", str(results_path),
        "--output", str(output_dir),
        "--table", table_name,
        "--namespace", namespace,
    ]
    # detection_reports flow handles its own DB write; tell the report script to only
    # render PDFs (+ coverage check + nginx copy) and skip the yujing review-result UPDATE.
    if not write_db:
        cmd.append("--no-db")

    log.info("Generating review reports: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/opt/paper-integrity-checker",
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=REPORT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"Report generation timed out after {REPORT_TIMEOUT_SECONDS}s")

    out_text = stdout.decode(errors="replace")
    log.info("Report generation output: %s", out_text[-1000:])

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"Report generation failed (rc={proc.returncode}): {err[-500:]}")

    # Return only the PDFs this task produced — REVIEW_DIR is shared across all tasks,
    # so globbing it would pollute the count with historical reports. A pre-existing
    # same-DOI report only counts if its mtime changed during this invocation.
    pdfs = []
    for p_str, old_mtime in expected.items():
        p = Path(p_str)
        if not p.exists():
            continue
        new_mtime = p.stat().st_mtime_ns
        if old_mtime is None or new_mtime > old_mtime:
            pdfs.append(str(p))
    if len(pdfs) != len(expected):
        missing = sorted(set(expected) - set(pdfs))
        log.warning("Review report generation produced %d/%d expected PDFs; missing/stale=%s",
                    len(pdfs), len(expected), missing[:5])
    return pdfs
