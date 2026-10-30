from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from api.config import MAX_CONCURRENT_TASKS, MAX_REVIEW_WORKERS, TASKS_DIR
from api.models import PaperInfo, TaskRecord, TaskStatus
from api.services.task_manager import TaskManager
from api.services.detect import run_detection_single
from api.services.review import run_review_single
from api.services.report import generate_reports
from modules.doi_utils import choose_authoritative_doi, normalize_doi

log = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)


async def run_pipeline(
    task_id: str,
    tm: TaskManager,
    papers: list[PaperInfo],
    config: dict,
):
    async with _semaphore:
        try:
            await _run_pipeline_inner(task_id, tm, papers, config)
        except Exception as e:
            log.exception("Pipeline failed for task %s", task_id)
            tm.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


async def _run_pipeline_inner(
    task_id: str,
    tm: TaskManager,
    papers: list[PaperInfo],
    config: dict,
):
    table_name = config.get("table_name", "yujing_quanliang")
    skip_refs = config.get("skip_refs", False)
    author_type = config.get("author_type", "")
    skip_review = config.get("skip_review", False)
    max_workers = min(max(1, int(config.get("max_workers", 4) or 4)), MAX_REVIEW_WORKERS)
    # Detection spawns a multi-process main.py per paper, so cap its fan-out lower than
    # review to avoid N×(--workers) subprocesses overwhelming the box.
    detect_workers = min(max_workers, 2)
    report_namespace = f'{table_name}/{task_id}'

    # Stage 1: Detection (concurrent, capped at detect_workers)
    tm.update_task(
        task_id,
        status=TaskStatus.DETECTING,
        stage="detecting",
        progress={"current": 0, "total": len(papers), "stage": "detection"},
    )

    high_risk_papers = []
    detect_sem = asyncio.Semaphore(detect_workers)
    detect_done = 0

    async def _detect_one(paper):
        nonlocal detect_done
        async with detect_sem:
            try:
                saved = Path(paper.output_dir) / 'report.json'
                findings = None
                if paper.status in {'detected', 'review_error', 'persist_error'} and saved.is_file():
                    findings = json.loads(saved.read_text())
                if findings is None:
                    findings = await run_detection_single(
                        input_dir=paper.input_dir,
                        output_dir=paper.output_dir,
                        skip_refs=skip_refs,
                        author_type=author_type,
                        table_name=table_name,
                        doi=paper.doi,
                        report_namespace=report_namespace,
                    )
                if findings.get('error') or not findings.get('paper') or 'summary' not in findings:
                    raise RuntimeError('Detection did not produce valid findings')

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
                        "Keeping supplied DOI for task %s paper %s: supplied=%s detected=%s",
                        task_id,
                        paper.submission_no,
                        paper.doi,
                        normalize_doi((findings.get("paper") or {}).get("doi", "")),
                    )

                risk_level = "低风险"
                if "summary" in findings:
                    from modules.chinese_report_generator import _compute_overall_risk
                    overall = _compute_overall_risk(findings)
                    risk_level = overall.get("level", "低风险")

                paper.status = "detected"
                paper.error = ""
                if risk_level == "高风险":
                    high_risk_papers.append(paper)

            except Exception as e:
                log.error("Detection failed for %s: %s", paper.doi_slug, e)
                paper.status = "error"
                paper.error = str(e)

            # asyncio is single-threaded: counter increment + append are safe between awaits.
            detect_done += 1
            tm.update_task(
                task_id,
                progress={"current": detect_done, "total": len(papers), "stage": "detection"},
                papers=papers,
            )

    await asyncio.gather(*(_detect_one(p) for p in papers))

    detected_ok = [p for p in papers if p.status == "detected"]
    detected_fail = [p for p in papers if p.status == "error"]

    # Stage 2: AI Review (concurrent, capped at max_workers)
    review_results = []
    if not skip_review and high_risk_papers:
        tm.update_task(
            task_id,
            status=TaskStatus.REVIEWING,
            stage="reviewing",
            progress={"current": 0, "total": len(high_risk_papers), "stage": "review"},
        )

        review_sem = asyncio.Semaphore(max_workers)
        review_done = 0

        async def _review_one(paper):
            nonlocal review_done
            async with review_sem:
                try:
                    review = await run_review_single(
                        doi=paper.doi,
                        report_json_path=paper.report_json,
                        input_dir=paper.input_dir,
                        output_dir=paper.output_dir,
                        resume=True,
                    )
                    review.setdefault("trigger", "auto_detection")
                    review["_input_dir"] = paper.input_dir
                    review["_report_json"] = paper.report_json
                    review_results.append(review)
                    if review.get('trigger') == 'review_error':
                        paper.status = 'review_error'
                        paper.error = review.get('reason') or 'Review failed'
                except Exception as e:
                    log.error("Review failed for %s: %s", paper.doi, e)
                    paper.status = 'review_error'
                    paper.error = str(e)
                    review_results.append({
                        "doi": paper.doi,
                        "result": "高风险",
                        "trigger": "review_error",
                        "image_review": f"复核失败：{e}",
                        "data_review": f"复核失败：{e}",
                        "ref_review": "",
                        "verdict": "建议高风险",
                        "reason": str(e),
                    })

                review_done += 1
                tm.update_task(
                    task_id,
                    progress={"current": review_done, "total": len(high_risk_papers), "stage": "review"},
                    papers=papers,
                )

        await asyncio.gather(*(_review_one(p) for p in high_risk_papers))

    # Stage 3: Generate Reports
    report_paths = []
    failed_reviews = [r for r in review_results if r.get('trigger') == 'review_error']
    review_results = [r for r in review_results if r.get('trigger') != 'review_error']
    if review_results:
        tm.update_task(
            task_id,
            status=TaskStatus.GENERATING_REPORT,
            stage="generating_report",
            progress={"current": 0, "total": 1, "stage": "report"},
        )

        task_dir = TASKS_DIR / task_id
        try:
            report_paths = await generate_reports(review_results, task_dir, table_name,
                                                  report_namespace=report_namespace)
        except Exception as exc:
            log.error("Report generation failed: %s", exc)
            tm.update_task(task_id, status=TaskStatus.FAILED,
                           error=f"Report generation failed: {exc}")
            return

    # Done
    confirmed_high = sum(1 for r in review_results if r.get("result") == "高风险")
    downgraded = sum(1 for r in review_results if r.get("result") == "低风险")

    result = {
        "total_papers": len(papers),
        "detected_ok": len(detected_ok),
        "detected_fail": len(detected_fail),
        "high_risk_detected": len(high_risk_papers),
        "reviewed": len(review_results),
        "review_failed": len(failed_reviews),
        "confirmed_high": confirmed_high,
        "downgraded": downgraded,
        "reports_generated": len(report_paths),
        "review_skipped": skip_review,
    }

    failures = []
    if detected_fail:
        failures.append(f'{len(detected_fail)} paper(s) failed detection: ' + '; '.join(p.error for p in detected_fail)[:1500])
    if failed_reviews:
        failures.append(f'{len(failed_reviews)} paper(s) failed review: ' + '; '.join(str(r.get('reason', '')) for r in failed_reviews)[:1500])
    if not papers:
        failures.append('No papers were available for processing')
    tm.update_task(
        task_id,
        status=TaskStatus.FAILED if failures else TaskStatus.COMPLETED,
        stage="failed" if failures else "done",
        error='; '.join(failures) if failures else None,
        result=result,
        papers=papers,
    )

    log.info(
        "Task %s completed: %d papers, %d high-risk detected, %d confirmed, %d downgraded",
        task_id, len(papers), len(high_risk_papers), confirmed_high, downgraded,
    )
