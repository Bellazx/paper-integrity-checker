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
import logging
from pathlib import Path

from api.config import MAX_CONCURRENT_TASKS, MAX_REVIEW_WORKERS, REVIEW_DIR, TASKS_DIR
from api.models import PaperInfo, TaskStatus
from modules.chinese_report_generator import doi_to_slug
from api.services.task_manager import TaskManager
from api.services.detect import run_detection_single
from api.services.review import run_review_single
from api.services.report import generate_reports
from api.services.detection_reports_db import upsert_chushen, update_review

log = logging.getLogger(__name__)

CHUSHEN_BASE_URL = "http://10.119.9.99/chinese_reports"
REVIEW_BASE_URL = "http://10.119.9.99/review_reports"
REPORT_NAMESPACE = "detection_reports"

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


def _chushen_report_url(findings: dict) -> str:
    """First-pass detection PDF URL — matches the filename main.py writes via
    modules.chinese_report_generator._make_filename(doi, title)."""
    from modules.chinese_report_generator import _make_filename
    paper = findings.get("paper", {})
    filename = _make_filename(paper.get("doi", "unknown"), paper.get("title", "unknown"))
    return f"{CHUSHEN_BASE_URL}/{REPORT_NAMESPACE}/{filename}"


def _review_report_url(doi: str) -> str:
    from modules.chinese_report_generator import doi_to_slug
    return f"{REVIEW_BASE_URL}/{REPORT_NAMESPACE}/review_{doi_to_slug(doi)}.pdf"


async def run_detection_reports_pipeline(
    task_id: str,
    tm: TaskManager,
    papers: list[PaperInfo],
    config: dict,
):
    async with _semaphore:
        try:
            await _run_inner(task_id, tm, papers, config)
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

    # ── Stage 1: detection (no yujing write) + 初审 row ──────────────────
    tm.update_task(
        task_id,
        status=TaskStatus.DETECTING,
        stage="detecting",
        progress={"current": 0, "total": len(papers), "stage": "detection"},
    )

    # submission_no -> chushen_result, for building the final summary
    high_risk_papers: list[PaperInfo] = []

    for i, paper in enumerate(papers):
        try:
            findings = await run_detection_single(
                input_dir=paper.input_dir,
                output_dir=paper.output_dir,
                author_type=author_type,
                table_name="yujing_quanliang",  # unused: write_db=False forces --no-db
                write_db=False,
                doi=paper.doi,
                report_namespace=REPORT_NAMESPACE,
            )

            # main.py ran with --no-db, so its pdf_generated guard never fired here.
            # A missing PDF means the chushen report URL would 404 — treat as error.
            if not findings.get("pdf_generated", True):
                raise RuntimeError("PDF generation failed; skipping detection_reports write")

            report_json = Path(paper.output_dir) / "report.json"
            paper.report_json = str(report_json) if report_json.exists() else ""

            if not paper.doi and "paper" in findings:
                paper.doi = findings["paper"].get("doi", "")

            from modules.chinese_report_generator import _compute_overall_risk
            chushen_result = "低风险"
            if "summary" in findings:
                chushen_result = _compute_overall_risk(findings).get("level", "低风险")

            chushen_url = _chushen_report_url(findings)

            upsert_chushen(
                submission_no=paper.submission_no,
                fold_name=paper.fold_name,
                task_id=task_id,
                findings=findings,
                chushen_result=chushen_result,
                chushen_report_url=chushen_url,
                author_type_override=author_type,
            )

            paper.status = "detected"
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
    if high_risk_papers:
        tm.update_task(
            task_id,
            status=TaskStatus.REVIEWING,
            stage="reviewing",
            progress={"current": 0, "total": len(high_risk_papers), "stage": "review"},
        )

        review_sem = asyncio.Semaphore(max_workers)
        review_done = 0
        review_by_key: dict[tuple[str, str], dict] = {}

        async def _review_one(paper: PaperInfo):
            nonlocal review_done
            async with review_sem:
                try:
                    review = await run_review_single(
                        doi=paper.doi,
                        report_json_path=paper.report_json,
                        input_dir=paper.input_dir,
                        output_dir=paper.output_dir,
                    )
                    review.setdefault("trigger", "auto_detection")
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
                review_by_key[(paper.submission_no, paper.fold_name)] = review
                review_done += 1
                tm.update_task(
                    task_id,
                    progress={"current": review_done, "total": len(high_risk_papers), "stage": "review"},
                )

        await asyncio.gather(*(_review_one(paper) for paper in high_risk_papers))
        review_results = [
            review_by_key[(paper.submission_no, paper.fold_name)]
            for paper in high_risk_papers
            if (paper.submission_no, paper.fold_name) in review_by_key
        ]

    # ── Stage 3: generate review PDFs (no yujing write) + 复审 row ────────
    generated_paths: set[str] = set()
    if review_results:
        tm.update_task(
            task_id,
            status=TaskStatus.GENERATING_REPORT,
            stage="generating_report",
            progress={"current": 0, "total": 1, "stage": "report"},
        )

        try:
            task_dir = TASKS_DIR / task_id
            report_paths = await generate_reports(
                review_results,
                task_dir,
                table_name=REPORT_NAMESPACE,
                write_db=False,
                report_namespace=REPORT_NAMESPACE,
            )
        except Exception as e:
            log.error("Report generation failed: %s", e)
            tm.update_task(task_id, status=TaskStatus.FAILED, error=f"Report generation failed: {e}")
            return

        # Write 复审 result per high-risk paper (status -> 2).
        generated_paths = set(report_paths)
        for paper, review in zip(high_risk_papers, review_results):
            review_result = review.get("result", "高风险")
            expected_path = str(REVIEW_DIR / REPORT_NAMESPACE / f"review_{doi_to_slug(paper.doi or '')}.pdf")
            if expected_path not in generated_paths:
                log.error("Skipping detection_reports 复审 write for %s: review PDF was not generated", paper.doi)
                continue
            try:
                update_review(
                    submission_no=paper.submission_no,
                    fold_name=paper.fold_name,
                    review_result=review_result,
                    review_report_url=_review_report_url(paper.doi),
                )
            except Exception as e:
                log.error("detection_reports 复审 write failed for %s: %s", paper.doi, e)

    # ── Done: build summary + per-paper detection_reports records ─────────
    review_by_sub = {
        (p.submission_no, p.fold_name): r
        for p, r in zip(high_risk_papers, review_results)
        if str(REVIEW_DIR / REPORT_NAMESPACE / f"review_{doi_to_slug(p.doi or '')}.pdf") in generated_paths
    }
    high_keys = {(p.submission_no, p.fold_name) for p in high_risk_papers}

    records = []
    for p in detected_ok:
        key = (p.submission_no, p.fold_name)
        review = review_by_sub.get(key)
        is_chushen_high = key in high_keys
        records.append({
            "submission_no": p.submission_no,
            "fold_name": p.fold_name or None,
            "doi": p.doi,
            "chushen_result": "高风险" if is_chushen_high else "低风险",
            "chushen_report_url": _chushen_report_url_from_paper(p),
            "review_result": review.get("result") if review else None,
            "review_report_url": _review_report_url(p.doi) if review else None,
            "status": 2 if review else 0,
        })

    persisted_reviews = list(review_by_sub.values())
    confirmed_high = sum(1 for r in persisted_reviews if r.get("result") == "高风险")
    downgraded = sum(1 for r in persisted_reviews if r.get("result") == "低风险")

    result = {
        "total_papers": len(papers),
        "detected_ok": len(detected_ok),
        "detected_fail": len(detected_fail),
        "high_risk_detected": len(high_risk_papers),
        "reviewed": len(persisted_reviews),
        "confirmed_high": confirmed_high,
        "downgraded": downgraded,
        "reports_generated": len(generated_paths) if review_results else 0,
        "detection_reports": records,
    }

    tm.update_task(
        task_id,
        status=TaskStatus.COMPLETED,
        stage="done",
        result=result,
        papers=papers,
    )

    log.info(
        "detection_reports task %s completed: %d papers, %d high-risk, %d confirmed, %d downgraded",
        task_id, len(papers), len(high_risk_papers), confirmed_high, downgraded,
    )


def _chushen_report_url_from_paper(paper: PaperInfo) -> str:
    """Rebuild the 初审 PDF URL from the paper's persisted report.json (for the summary).
    Falls back to a doi-slug name if report.json is unavailable."""
    import json
    if paper.report_json:
        try:
            with open(paper.report_json) as f:
                return _chushen_report_url(json.load(f))
        except Exception:
            pass
    return f"{CHUSHEN_BASE_URL}/{REPORT_NAMESPACE}/{doi_to_slug(paper.doi) or 'unknown'}.pdf"
