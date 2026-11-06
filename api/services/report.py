from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path

from api.config import (
    BASE_DIR, PYTHON, REVIEW_REPORT_SCRIPT, REVIEW_DIR,
    DEFAULT_TABLE, REPORT_TIMEOUT_SECONDS, sanitize_report_namespace,
)
from api.services.review_runtime import time_left
from modules.chinese_report_generator import doi_to_slug

log = logging.getLogger(__name__)


async def generate_english_report(value: dict, output_dir: Path, task_dir: Path,
                                  *, kind: str, slug: str = "", deadline: float | None = None) -> str:
    """A cancellable sibling of the Chinese renderer with the same hard budget."""
    timeout = time_left(deadline, REPORT_TIMEOUT_SECONDS)
    task_dir.mkdir(parents=True, exist_ok=True)
    source = task_dir / f"english_{kind}.json"
    source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        str(PYTHON), str(BASE_DIR / "scripts" / "generate_english_report.py"),
        "--kind", kind, "--input", str(source), "--output", str(output_dir), "--slug", slug,
        cwd=str(BASE_DIR), start_new_session=True,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PAPER_INTEGRITY_ROOT": str(BASE_DIR),
             "PAPER_LLM_DEADLINE": str(time.monotonic() + timeout - min(15, timeout / 10)),
             "REVIEW_COVERAGE_GAP_POLICY": "strict"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        await _kill_process_group(proc)
        raise
    if proc.returncode:
        raise RuntimeError(f"English report failed (rc={proc.returncode}): {stderr.decode(errors='replace')[-800:]}")
    paths = _rendered_pdf_names(stdout.decode(errors="replace"))
    if len(paths) != 1:
        raise RuntimeError("English renderer did not confirm a fresh PDF")
    path = Path(paths.pop())
    if path.parent.resolve() != output_dir.resolve() or not path.is_file():
        raise RuntimeError("English renderer returned an invalid PDF path")
    return str(path)


def _is_review_error(result: dict) -> bool:
    return result.get("trigger") == "review_error"


def _review_report_slug(result: dict) -> str:
    """Filename slug for a review PDF.

    Prefer the real DOI when present. User-facing uploads can include Chinese or
    no-DOI papers, so callers may provide ``_report_slug`` as a task-local stable
    fallback (for example the extracted folder/key). This affects only the PDF
    filename/URL; the report body still displays ``doi``.
    """
    return doi_to_slug(result.get("doi") or "") or doi_to_slug(result.get("_report_slug") or "") or "unknown"


def _rendered_pdf_names(output: str) -> set[str]:
    """Return PDF basenames reported by the renderer's stable ``[PDF]`` marker."""
    return {
        line.strip()[len("[PDF] "):].strip()
        for line in (output or "").splitlines()
        if line.strip().startswith("[PDF] ") and line.strip()[len("[PDF] "):].strip()
    }


async def _kill_process_group(proc) -> None:
    """Terminate the report script and any child worker it spawned.

    generate_review_report.py runs coverage validation in a child process. Killing
    only the parent can leave that child alive, while ProcessLookupError from a
    just-exited parent would otherwise surface as an empty API error.
    """
    pid = getattr(proc, "pid", None)
    try:
        if pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()

    wait = getattr(proc, "wait", None)
    if wait is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(wait(), timeout=10)


async def generate_reports(
    review_results: list[dict],
    task_dir: Path,
    table_name: str = DEFAULT_TABLE,
    write_db: bool = True,
    report_namespace: str | None = None,
    deadline: float | None = None,
) -> list[str]:
    """Retry transient rendering failures without repeating detection/review.

    ``deadline`` (time.monotonic) is the paper's deadline: each attempt is
    shortened to the time left, and no retry starts once it has passed.
    """
    for attempt in range(2):
        try:
            return await _generate_reports_once(
                review_results, task_dir, table_name, write_db, report_namespace,
                timeout=time_left(deadline, REPORT_TIMEOUT_SECONDS),
            )
        except (RuntimeError, TimeoutError, OSError) as exc:
            message = str(exc).lower()
            transient = isinstance(exc, (TimeoutError, ConnectionError)) or any(
                cue in message for cue in (
                    'timed out', 'timeout', 'temporarily unavailable',
                    'resource temporarily', 'rc=-9', 'connection reset',
                    'worker killed', 'out of memory',
                )
            )
            if attempt or not transient or (deadline is not None and time.monotonic() >= deadline):
                raise
            log.warning('Transient report failure; retrying rendering only: %s', exc)
            await asyncio.sleep(2)


async def _generate_reports_once(
    review_results: list[dict],
    task_dir: Path,
    table_name: str = DEFAULT_TABLE,
    write_db: bool = True,
    report_namespace: str | None = None,
    timeout: float = REPORT_TIMEOUT_SECONDS,
) -> list[str]:
    errored = [
        (r.get("doi") or "<unknown>", r.get("reason") or "review process failed")
        for r in review_results
        if _is_review_error(r)
    ]
    if errored:
        sample = "; ".join(f"{doi}: {reason}" for doi, reason in errored[:3])
        raise ValueError(f"Refusing to generate final review PDF for failed reviews: {sample}")

    results_path = task_dir / "review_results.json"
    task_dir.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(review_results, ensure_ascii=False, indent=2))

    namespace = sanitize_report_namespace(report_namespace or table_name)
    output_dir = REVIEW_DIR / namespace
    output_dir.mkdir(parents=True, exist_ok=True)
    expected = {}
    for r in review_results:
        slug = _review_report_slug(r)
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
        cwd=str(BASE_DIR),
        env={
            **os.environ,
            "PAPER_INTEGRITY_ROOT": str(BASE_DIR),
            # Resource exhaustion can be an auditable warning, but semantic
            # coverage gaps must remain fatal so an unexplained HIGH finding
            # cannot be published as a completed report.
            "REVIEW_COVERAGE_FAILURE_POLICY": os.environ.get(
                "REVIEW_COVERAGE_FAILURE_POLICY", "warn"
            ),
            # Do not inherit a caller's permissive value. The project report
            # entrypoint deliberately ignores this legacy escape hatch and
            # fails closed on every semantic gap.
            "REVIEW_COVERAGE_GAP_POLICY": "strict",
        },
        start_new_session=True,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        await _kill_process_group(proc)
        raise TimeoutError(f"Report generation timed out after {timeout:.0f}s")
    except asyncio.CancelledError:
        await _kill_process_group(proc)
        raise

    out_text = stdout.decode(errors="replace")
    log.info("Report generation output: %s", out_text[-1000:])

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"Report generation failed (rc={proc.returncode}): {err[-500:]}")

    # Return only the PDFs requested by this task — REVIEW_DIR is shared across all
    # tasks, so globbing it would pollute the count with historical reports.  The
    # renderer prints ``[PDF] filename`` after a successful render; use that signal
    # as a fallback because a filesystem timestamp can remain unchanged when a
    # report is replaced atomically on filesystems with coarse timestamp precision.
    rendered_names = _rendered_pdf_names(out_text)
    pdfs = []
    for p_str, old_mtime in expected.items():
        p = Path(p_str)
        if not p.exists():
            continue
        new_mtime = p.stat().st_mtime_ns
        if old_mtime is None or new_mtime > old_mtime or p.name in rendered_names:
            pdfs.append(str(p))
    if len(pdfs) != len(expected):
        missing = sorted(set(expected) - set(pdfs))
        log.warning("Review report generation produced %d/%d expected PDFs; missing/stale=%s",
                    len(pdfs), len(expected), missing[:5])
        raise RuntimeError(f"Missing or stale review PDFs: {missing}")
    return pdfs
