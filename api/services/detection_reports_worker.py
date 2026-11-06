"""Pipeline for the user-facing submit flow — writes results to detection_reports.

Mirrors api.worker.run_pipeline (detection → AI review → report generation) but targets
the detection_reports table instead of yujing_*:
  - detection runs with write_db=False (no yujing write); the 初审 result is written here
    via detection_reports_db.upsert_chushen (status=0)
  - report generation runs with write_db=False; the 复审 result is written here via
    detection_reports_db.update_review (status=2)

The detection/review/PDF machinery itself is fully reused from api.services.*.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from api.config import (
    API_MAX_CONCURRENT_TASKS, API_PAPER_DEADLINE_SECONDS, API_REPORT_RESERVE_SECONDS,
    DETECT_TIMEOUT_SECONDS, MAX_REVIEW_WORKERS, OUTPUT_DIR, REVIEW_DIR, TASKS_DIR,
    sanitize_report_namespace,
)
from api.models import PaperInfo, TaskStatus
from modules.chinese_report_generator import doi_to_slug
from modules.doi_utils import choose_authoritative_doi, normalize_doi
from api.services.task_manager import TaskManager
from api.services.detect import run_detection_single
from api.services.review import run_review_single
from api.services.report import generate_reports, generate_english_report
from api.services.report_access import verify_public_pdf
from api.services.api_capacity import api_work
from api.services.review_runtime import deadline_after, time_left
from api.services.submission_policy import blocking_write
from api.services.submission_policy import api_dry_run
from api.services.detection_reports_db import (
    upsert_chushen,
    update_review_result,
    update_review,
    verify_chushen_write,
    verify_review_write,
    update_english_reports,
)
from core.pipeline import initial_pdf_skipped

log = logging.getLogger(__name__)

PUBLIC_BASE_URL = os.environ.get("PAPER_PUBLIC_BASE_URL", "http://202.120.57.100").rstrip("/")
CHUSHEN_BASE_URL = os.environ.get(
    "PAPER_CHUSHEN_BASE_URL", f"{PUBLIC_BASE_URL}/chinese_reports"
).rstrip("/")
REVIEW_BASE_URL = os.environ.get(
    "PAPER_REVIEW_BASE_URL", f"{PUBLIC_BASE_URL}/review_reports"
).rstrip("/")
REPORT_NAMESPACE = "detection_reports"

_semaphore = asyncio.Semaphore(API_MAX_CONCURRENT_TASKS)


def _is_review_error(review: dict) -> bool:
    return review.get("trigger") == "review_error"


def _task_report_namespace(task_id: str) -> str:
    return f"{REPORT_NAMESPACE}/{task_id}"


def _paper_report_namespace(task_id: str, paper: PaperInfo) -> str:
    namespace = _task_report_namespace(task_id)
    if paper.fold_name:
        # DOI is not unique within an uploaded batch. Preserve each folder's
        # reports even when title/DOI (and thus PDF basename) are identical.
        namespace += '/paper_' + hashlib.sha256(paper.fold_name.encode()).hexdigest()[:16]
    return namespace


def _chushen_report_url(findings: dict, namespace: str = REPORT_NAMESPACE) -> str:
    """First-pass detection PDF URL — matches the filename main.py writes via
    modules.chinese_report_generator._make_filename(doi, title)."""
    if initial_pdf_skipped(findings):
        return ""
    from modules.chinese_report_generator import _make_filename
    paper = findings.get("paper", {})
    filename = _make_filename(paper.get("doi", "unknown"), paper.get("title", "unknown"))
    return f"{CHUSHEN_BASE_URL}/{namespace}/{filename}"


def _safe_report_slug(value: str) -> str:
    slug = doi_to_slug(value or "")
    if not slug:
        raise ValueError("Cannot generate review report slug from empty value")
    return slug


def _review_report_slug(paper: PaperInfo, review: dict | None = None) -> str:
    """Task-local review PDF key.

    DOI is still preferred when valid. For Chinese/no-DOI papers, fall back to
    the extracted paper key (`doi_slug`), which is already unique inside a task.
    This avoids multiple no-DOI high-risk reviews colliding as review_.pdf.
    """
    doi = _review_doi(paper, review)
    fallback = paper.doi_slug or paper.fold_name or paper.submission_no
    return _safe_report_slug(doi or fallback)


def _review_report_url_for_paper(paper: PaperInfo, review: dict | None, namespace: str = REPORT_NAMESPACE) -> str:
    return f"{REVIEW_BASE_URL}/{namespace}/review_{_review_report_slug(paper, review)}.pdf"


def _english_initial_report_url(findings: dict, namespace: str) -> str:
    from modules.english_report_generator import initial_report_filename

    filename = initial_report_filename(findings)
    return f"{PUBLIC_BASE_URL}/english_reports/{namespace}/{filename}"


def _english_review_report_url(paper: PaperInfo, review: dict | None, namespace: str) -> str:
    from modules.english_report_generator import review_report_filename

    return f"{REVIEW_BASE_URL}/{namespace}/{review_report_filename(_review_report_slug(paper, review))}"


def _review_doi(paper: PaperInfo, review: dict | None = None) -> str:
    doi = normalize_doi(paper.doi)
    if doi:
        return doi
    if review:
        doi = normalize_doi(review.get("doi", ""))
        if doi:
            return doi
    return ""


async def _render_review_pair(paper: PaperInfo, review: dict, task_dir: Path, deadline):
    """Both requested languages share one immutable review and run concurrently."""
    namespace = review['_report_namespace']
    jobs = {}
    if paper.language in {"zh", "both"}:
        jobs["zh"] = api_work.run_until(deadline,
            generate_reports, [review], task_dir / "zh", table_name=REPORT_NAMESPACE,
            write_db=False, report_namespace=namespace, deadline=deadline)
    if paper.language in {"en", "both"}:
        jobs["en"] = api_work.run_until(deadline,
            generate_english_report, review, REVIEW_DIR / namespace, task_dir / "en",
            kind="review", slug=_review_report_slug(paper, review), deadline=deadline)
    started = time.monotonic()
    timings = {}

    async def timed(language, job):
        begin = time.monotonic()
        try:
            return await job
        finally:
            timings[language] = round(time.monotonic() - begin, 3)

    results = await asyncio.gather(*(timed(language, job) for language, job in jobs.items()), return_exceptions=True)
    review['_report_metrics'] = {'languages': list(jobs), 'wall_seconds': round(time.monotonic() - started, 3),
                                 'language_seconds': timings}
    return dict(zip(jobs, results))


async def run_detection_reports_pipeline(
    task_id: str,
    tm: TaskManager,
    papers: list[PaperInfo],
    config: dict,
):
    async with _semaphore:
        try:
            await _run_inner(task_id, tm, [p for p in papers if not p.superseded_by], config)
        except Exception as e:
            log.exception("detection_reports pipeline failed for task %s", task_id)
            tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


async def _run_inner(
    task_id: str,
    tm: TaskManager,
    papers: list[PaperInfo],
    config: dict,
):
    author_type = config.get("author_type", "")
    max_workers = min(max(1, int(config.get("max_workers", 4) or 4)), MAX_REVIEW_WORKERS)
    report_namespace = _task_report_namespace(task_id)

    # ── Stage 1: detection (no yujing write) + 初审 row ──────────────────
    tm.update_task(
        task_id,
        status=TaskStatus.DETECTING,
        stage="detecting",
        progress={"current": 0, "total": len(papers), "stage": "detection"},
    )

    # submission_no -> chushen_result, for building the final summary
    high_risk_papers: list[PaperInfo] = []
    english_initial_urls: dict[tuple[str, str], str] = {}
    english_initial_errors: dict[tuple[str, str], str] = {}
    initial_results = {}
    # Each paper's shared clock (API_PAPER_DEADLINE_SECONDS) starts with its
    # own detection; review and report rendering use what is left of it.
    paper_deadlines: dict[tuple[str, str], float | None] = {}

    for i, paper in enumerate(papers):
        paper_deadlines[(paper.submission_no, paper.fold_name)] = deadline_after(API_PAPER_DEADLINE_SECONDS)
        try:
            paper_namespace = _paper_report_namespace(task_id, paper)
            saved = Path(paper.output_dir) / 'report.json'
            findings = None
            if paper.status in {'detected', 'review_error', 'persist_error'} and saved.is_file():
                findings = json.loads(saved.read_text())
                log.info('Resuming task %s after completed detection for %s', task_id, paper.doi)
            if findings is None:
                paper.initial_report_namespace = paper_namespace
                tm.update_task(task_id, papers=papers)
                findings = await api_work.run_until(paper_deadlines[(paper.submission_no, paper.fold_name)],
                    run_detection_single,
                    input_dir=paper.input_dir,
                    output_dir=paper.output_dir,
                    author_type=author_type,
                    table_name="yujing_quanliang",  # unused: write_db=False forces --no-db
                    write_db=False,
                    doi=paper.doi,
                    report_namespace=paper_namespace,
                    timeout=time_left(paper_deadlines[(paper.submission_no, paper.fold_name)],
                                      DETECT_TIMEOUT_SECONDS),
                    language=paper.language,
                    deadline=paper_deadlines[(paper.submission_no, paper.fold_name)],
                )
            if findings.get('error') or not findings.get('paper') or 'summary' not in findings:
                raise RuntimeError('Detection did not produce a valid findings report')

            # A high-risk screening has no PDF of its own: it goes on to review
            # and the review report is delivered.
            if not findings.get("pdf_generated", True) and not initial_pdf_skipped(findings):
                raise RuntimeError("PDF generation failed; skipping detection_reports write")

            report_json = Path(paper.output_dir) / "report.json"
            paper.report_json = str(report_json) if report_json.exists() else ""

            paper_doi, changed, conflict = choose_authoritative_doi(
                paper.doi,
                (findings.get("paper") or {}).get("doi", ""),
            )
            if changed:
                paper.doi = paper_doi
            if conflict:
                log.info(
                    "Keeping supplied DOI for submission %s: supplied=%s detected=%s",
                    paper.submission_no,
                    paper.doi,
                    normalize_doi((findings.get("paper") or {}).get("doi", "")),
                )

            from modules.chinese_report_generator import _compute_overall_risk
            chushen_result = "低风险"
            if "summary" in findings:
                chushen_result = _compute_overall_risk(findings).get("level", "低风险")

            chushen_url = _chushen_report_url(findings, report_namespace)
            if paper.initial_report_namespace:
                chushen_url = _chushen_report_url(findings, paper.initial_report_namespace)
            if paper.language == "en":
                chushen_url = ""
            if not api_dry_run():
                await verify_public_pdf(chushen_url)
            initial_results[(paper.submission_no, paper.fold_name)] = chushen_result

            # Keep the established Chinese report and generate an English sibling
            # when requested.  This is deliberately outside main.py so the legacy
            # detection CLI remains unchanged for batch/offline callers.
            if paper.language in {"en", "both"} and not initial_pdf_skipped(findings):
                try:
                    from modules.english_report_generator import initial_report_filename
                    english_namespace = paper_namespace
                    english_dir = OUTPUT_DIR / "english_reports" / sanitize_report_namespace(english_namespace)
                    # New detection children already render both languages in
                    # parallel. Older checkpoints need only the missing sibling.
                    saved_english = findings.get('_english_initial_pdf')
                    original_namespace = paper.initial_report_namespace or paper_namespace
                    original_path = (OUTPUT_DIR / 'english_reports' / sanitize_report_namespace(original_namespace)
                                     / initial_report_filename(findings))
                    if (saved_english and Path(saved_english).resolve() == original_path.resolve()
                            and original_path.is_file()):
                        english_namespace = original_namespace
                    else:
                        await api_work.run_until(paper_deadlines[(paper.submission_no, paper.fold_name)],
                            generate_english_report, findings, english_dir,
                            TASKS_DIR / task_id / 'initial' / str(i), kind="initial",
                            deadline=paper_deadlines[(paper.submission_no, paper.fold_name)])
                    key = (paper.submission_no, paper.fold_name)
                    english_initial_urls[key] = _english_initial_report_url(findings, english_namespace)
                    if not api_dry_run():
                        await verify_public_pdf(english_initial_urls[key])
                except Exception as e:
                    key = (paper.submission_no, paper.fold_name)
                    english_initial_urls.pop(key, None)
                    english_initial_errors[key] = str(e)
                    log.exception("English initial report failed for %s", paper.doi)

            if not api_dry_run():
                await blocking_write(
                    upsert_chushen,
                    submission_no=paper.submission_no,
                    fold_name=paper.fold_name,
                    task_id=task_id,
                    findings=findings,
                    chushen_result=chushen_result,
                    chushen_report_url=chushen_url,
                    author_type_override=author_type,
                )
                verified, verify_reason = await asyncio.to_thread(
                    verify_chushen_write,
                    task_id=task_id,
                    submission_no=paper.submission_no,
                    fold_name=paper.fold_name,
                    chushen_report_url=chushen_url,
                )
                if not verified:
                    raise RuntimeError(f"Initial report DB verification failed: {verify_reason}")

            paper.status = "detected"
            paper.error = ""
            if chushen_result == "高风险":
                high_risk_papers.append(paper)

        except Exception as e:
            log.error("Detection failed for %s: %s", paper.doi_slug, e)
            paper.status = "error"
            paper.error = str(e)

        tm.update_task(
            task_id,
            progress={"current": i + 1, "total": len(papers), "stage": "detection"},
            papers=papers,
        )

    detected_ok = [p for p in papers if p.status == "detected"]
    detected_fail = [p for p in papers if p.status == "error"]

    # ── Stage 2: AI review (high-risk only) ──────────────────────────────
    # review_results[i] corresponds to high_risk_papers[i] (appended in order).
    review_results: list[dict] = []
    review_by_key: dict[tuple[str, str], dict] = {}
    review_failures: dict[tuple[str, str], dict] = {}
    review_persisted_keys: set[tuple[str, str]] = set()
    persistence_failures: dict[tuple[str, str], str] = {}
    rendered_by_key = {}
    if high_risk_papers:
        tm.update_task(
            task_id,
            status=TaskStatus.REVIEWING,
            stage="reviewing",
            progress={"current": 0, "total": len(high_risk_papers), "stage": "review"},
        )

        review_sem = asyncio.Semaphore(max_workers)
        review_done = 0

        async def _review_one(paper: PaperInfo):
            nonlocal review_done
            async with review_sem:
                try:
                    paper_deadline = paper_deadlines.get((paper.submission_no, paper.fold_name))
                    review = await api_work.run(
                        run_review_single,
                        doi=paper.doi,
                        report_json_path=paper.report_json,
                        input_dir=paper.input_dir,
                        output_dir=paper.output_dir,
                        resume=True,
                        deadline=(paper_deadline - API_REPORT_RESERVE_SECONDS
                                  if paper_deadline is not None else None),
                        shared_api_budget=True,
                    )
                    review.setdefault("trigger", "auto_detection")
                    review["doi"] = _review_doi(paper, review)
                    review["_report_slug"] = _review_report_slug(paper, review)
                    review["_input_dir"] = paper.input_dir
                    review["_report_json"] = paper.report_json
                    review['_report_namespace'] = _paper_report_namespace(task_id, paper)
                except Exception as e:
                    log.error("Review failed for %s: %s", paper.doi, e)
                    review = {
                        "doi": paper.doi,
                        "result": "高风险",
                        "trigger": "review_error",
                        "image_review": f"复核失败：{e}",
                        "data_review": f"复核失败：{e}",
                        "ref_review": "",
                        "verdict": "建议高风险",
                        "reason": str(e),
                    }
                key = (paper.submission_no, paper.fold_name)
                if _is_review_error(review):
                    reason = review.get("reason") or "AI review process failed"
                    paper.status = "review_error"
                    paper.error = reason
                    review_failures[key] = review
                    log.error("Review did not produce a substantive result for %s: %s", paper.doi, reason)
                else:
                    review_by_key[key] = review
                    # Render as soon as this paper finishes; a slower paper in
                    # the same batch must not consume this one's report budget.
                    rendered_by_key[key] = await _render_review_pair(
                        paper, review, TASKS_DIR / task_id / 'reports' / str(high_risk_papers.index(paper)),
                        paper_deadlines.get(key))
                    if not api_dry_run():
                        try:
                            matched = await blocking_write(
                                update_review_result,
                                task_id=task_id,
                                submission_no=paper.submission_no,
                                fold_name=paper.fold_name,
                                review_result=review.get("result", "高风险"),
                            )
                            if not matched:
                                log.warning("Provisional review result was not persisted for %s", paper.doi)
                        except Exception:
                            # Provisional visibility must never turn a valid
                            # review into a failed review; final persistence
                            # still requires a verified public PDF.
                            log.exception("Provisional review write failed for %s", paper.doi)
                review_done += 1
                tm.update_task(
                    task_id,
                    progress={"current": review_done, "total": len(high_risk_papers), "stage": "review"},
                    papers=papers,
                )

        await asyncio.gather(*(_review_one(paper) for paper in high_risk_papers))
        review_results = [
            review_by_key[(paper.submission_no, paper.fold_name)]
            for paper in high_risk_papers
            if (paper.submission_no, paper.fold_name) in review_by_key
        ]

    # ── Stage 3: generate review PDFs (no yujing write) + 复审 row ────────
    generated_paths: set[str] = set()
    english_review_urls: dict[tuple[str, str], str] = {}
    english_review_errors: dict[tuple[str, str], str] = {}
    english_persisted_keys = set()
    if review_results:
        tm.update_task(
            task_id,
            status=TaskStatus.GENERATING_REPORT,
            stage="generating_report",
            progress={"current": 0, "total": 1, "stage": "report"},
        )

        task_dir = TASKS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / 'review_results.json').write_text(json.dumps(review_results, ensure_ascii=False, indent=2))
        for paper in high_risk_papers:
            paper_namespace = _paper_report_namespace(task_id, paper)
            key = (paper.submission_no, paper.fold_name)
            review = review_by_key.get(key)
            if not review:
                continue
            review_result = review.get("result", "高风险")
            doi_for_review = _review_doi(paper, review)
            rendered = rendered_by_key.get(key, {})
            required = {"zh", "en"} if paper.language == "both" else {paper.language}
            errors = []
            for language in required:
                value = rendered.get(language)
                if isinstance(value, BaseException) or not value:
                    reason = f"{language} report generation failed: {value or 'no PDF'}"
                    errors.append(reason)
                    if language == "en":
                        english_review_errors[key] = reason
                elif language == "zh":
                    expected = str(REVIEW_DIR / paper_namespace / f"review_{_review_report_slug(paper, review)}.pdf")
                    if expected not in value:
                        errors.append(f"Missing Chinese PDF: {expected}")
                    else:
                        generated_paths.update(value)
                else:
                    english_review_urls[key] = _english_review_report_url(paper, review, paper_namespace)
                    generated_paths.add(str(value))
            if errors:
                reason = "; ".join(errors)
                paper.status, paper.error = "persist_error", reason
                persistence_failures[key] = reason
                continue
            try:
                if key in english_review_urls and not api_dry_run():
                    await verify_public_pdf(english_review_urls[key])
                    # In English-only mode the Chinese URL is intentionally
                    # empty. Persist/verify the requested URL before status=2
                    # can make a frontend stop polling.
                    await blocking_write(update_english_reports, paper.submission_no,
                                         paper.fold_name, task_id, english_initial_urls.get(key),
                                         english_review_urls[key])
                    english_persisted_keys.add(key)
                review_url = (_review_report_url_for_paper(paper, review, paper_namespace)
                              if paper.language in {"zh", "both"} else "")
                if not api_dry_run():
                    await verify_public_pdf(review_url)
                    matched = await blocking_write(
                        update_review,
                        task_id=task_id,
                        submission_no=paper.submission_no,
                        fold_name=paper.fold_name,
                        review_result=review_result,
                        review_report_url=review_url,
                    )
                    if not matched:
                        reason = "review DB row was not found; review_report_url was not persisted"
                        paper.status = "persist_error"
                        paper.error = reason
                        persistence_failures[key] = reason
                        log.error("detection_reports 复审 write did not match a row for %s: %s", doi_for_review, reason)
                        continue
                    verified, verify_reason = await asyncio.to_thread(
                        verify_review_write,
                        task_id=task_id,
                        submission_no=paper.submission_no,
                        fold_name=paper.fold_name,
                        review_result=review_result,
                        review_report_url=review_url,
                    )
                    if not verified:
                        reason = f"review DB verification failed: {verify_reason}"
                        paper.status = "persist_error"
                        paper.error = reason
                        persistence_failures[key] = reason
                        log.error("detection_reports 复审 verification failed for %s: %s", doi_for_review, verify_reason)
                        continue
                review_persisted_keys.add(key)
            except Exception as e:
                reason = f"review DB write failed: {e}"
                paper.status = "persist_error"
                paper.error = reason
                persistence_failures[key] = reason
                log.error("detection_reports 复审 write failed for %s: %s", doi_for_review, e)

    if not api_dry_run():
        for paper in papers:
            key = (paper.submission_no, paper.fold_name)
            if key in english_persisted_keys:
                continue
            initial_url, review_url = english_initial_urls.get(key), english_review_urls.get(key)
            if initial_url or review_url:
                try:
                    await blocking_write(update_english_reports, paper.submission_no,
                                         paper.fold_name, task_id, initial_url, review_url)
                except Exception as exc:
                    if initial_url:
                        english_initial_errors[key] = f'English report DB persistence failed: {exc}'
                    if review_url:
                        english_review_errors[key] = f'English report DB persistence failed: {exc}'

    # ── Done: build summary + per-paper detection_reports records ─────────
    review_by_sub = {
        key: r
        for key, r in review_by_key.items()
        if key in review_persisted_keys
    }
    high_keys = {(p.submission_no, p.fold_name) for p in high_risk_papers}

    records = []
    for p in detected_ok:
        key = (p.submission_no, p.fold_name)
        # Only expose a review result after the PDF and the database write have
        # both been verified.  ``review_by_key`` may contain a substantive LLM
        # result even when update_review/verify_review_write failed; returning
        # that result here made a paper look reviewed while its DB row was
        # still status=0.
        review = review_by_sub.get(key)
        raw_review = review
        is_chushen_high = key in high_keys
        review_failure = review_failures.get(key) if is_chushen_high else None
        persist_failure = persistence_failures.get(key) if is_chushen_high else None
        # ``detection_reports.status`` is a database lifecycle code (0 means
        # "initial screening written", 2 means "review written"), not a
        # success/failure flag.  Keep the numeric value for compatibility but
        # expose an unambiguous per-paper state for API consumers.  In
        # particular, an initial low-risk paper is complete even though it has
        # no review_result/review_report_url and therefore keeps status=0.
        if p.status == "error":
            processing_status = "failed"
            processing_status_text = "检测失败"
            processing_completed = False
        elif review_failure or persist_failure:
            processing_status = "review_failed"
            processing_status_text = "复审失败"
            processing_completed = False
        elif key in english_initial_errors or key in english_review_errors:
            processing_status = "report_failed"
            processing_status_text = "英文报告生成或访问失败"
            processing_completed = False
        elif review:
            processing_status = "completed_review"
            processing_status_text = "检测完成（已复审）"
            processing_completed = True
        elif is_chushen_high:
            processing_status = "review_pending"
            processing_status_text = "初审完成，等待复审"
            processing_completed = False
        else:
            processing_status = "completed_initial"
            processing_status_text = "检测完成（初审低风险）"
            processing_completed = True
        records.append({
            "submission_no": p.submission_no,
            "fold_name": p.fold_name or None,
            "doi": p.doi,
            "chushen_result": initial_results.get(key, "低风险"),
            "chushen_report_url": (_chushen_report_url_from_paper(p, p.initial_report_namespace or report_namespace)
                                    if p.language in {"zh", "both"} else None),
            "review_result": raw_review.get("result") if raw_review else None,
            "review_report_url": (_review_report_url_for_paper(p, review, _paper_report_namespace(task_id, p))
                                  if review and p.language in {"zh", "both"} else None),
            "english_chushen_report_url": english_initial_urls.get(key),
            "english_review_report_url": english_review_urls.get(key),
            "english_report_error": english_initial_errors.get(key) or english_review_errors.get(key),
            "status": 2 if review else 0,
            "processing_status": processing_status,
            "processing_status_text": processing_status_text,
            "processing_completed": processing_completed,
            "review_error": (
                review_failure.get("reason") if review_failure else persist_failure
            ),
        })

    persisted_reviews = list(review_by_sub.values())
    review_failed = len(review_failures)
    confirmed_high = sum(1 for r in persisted_reviews if r.get("result") == "高风险")
    downgraded = sum(1 for r in persisted_reviews if r.get("result") == "低风险")

    result = {
        "total_papers": len(papers),
        "detected_ok": len(detected_ok),
        "detected_fail": len(detected_fail),
        "high_risk_detected": sum(value == "高风险" for value in initial_results.values()),
        "reviewed": len(persisted_reviews),
        "review_failed": review_failed,
        "persistence_failed": len(persistence_failures),
        "confirmed_high": confirmed_high,
        "downgraded": downgraded,
        "reports_generated": len(generated_paths) if review_results else 0,
        "english_reports_generated": len(english_initial_urls) + len(english_review_urls),
        "detection_reports": records,
    }

    english_failures = set(english_initial_errors) | set(english_review_errors)
    status = TaskStatus.FAILED if (detected_fail or review_failed or persistence_failures or english_failures) else TaskStatus.COMPLETED
    error = None
    if detected_fail:
        error = f"Detection/initial report persistence failed for {len(detected_fail)} paper(s)"
    if review_failed:
        review_error = f"AI review failed for {review_failed} high-risk paper(s); no final review PDF was generated for failed reviews"
        causes = list(dict.fromkeys(str(r.get('reason') or '未知复审错误') for r in review_failures.values()))
        if causes:
            review_error += ': ' + '; '.join(causes)[:1500]
        error = f"{error}; {review_error}" if error else review_error
    if persistence_failures:
        persist_error = (
            f"Report DB persistence failed for {len(persistence_failures)} high-risk paper(s); "
            "task is not complete until review_report_url is written"
        )
        error = f"{error}; {persist_error}" if error else persist_error
    if english_failures:
        english_error = f"English report generation/access failed for {len(english_failures)} paper(s)"
        error = f"{error}; {english_error}" if error else english_error

    tm.update_task(
        task_id,
        status=status,
        stage="done" if status == TaskStatus.COMPLETED else "failed",
        result=result,
        error=error,
        papers=papers,
    )

    log.info(
        "detection_reports task %s finished status=%s: %d papers, %d high-risk, %d confirmed, %d downgraded",
        task_id, status.value, len(papers), len(high_risk_papers), confirmed_high, downgraded,
    )


def _chushen_report_url_from_paper(paper: PaperInfo, namespace: str = REPORT_NAMESPACE) -> str:
    """Rebuild the 初审 PDF URL from the paper's persisted report.json (for the summary).
    Falls back to a doi-slug name if report.json is unavailable."""
    import json
    if paper.report_json:
        try:
            with open(paper.report_json) as f:
                return _chushen_report_url(json.load(f), namespace)
        except Exception:
            pass
    fallback = paper.doi or paper.doi_slug or paper.fold_name or paper.submission_no
    return f"{CHUSHEN_BASE_URL}/{namespace}/{_safe_report_slug(fallback)}.pdf"
