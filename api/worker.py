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
                findings = await run_detection_single(
                    input_dir=paper.input_dir,
                    output_dir=paper.output_dir,
                    skip_refs=skip_refs,
                    author_type=author_type,
                    table_name=table_name,
                    doi=paper.doi,
                )

                report_json = Path(paper.output_dir) / "report.json"
                paper.report_json = str(report_json) if report_json.exists() else ""

                if not paper.doi and "paper" in findings:
                    paper.doi = findings["paper"].get("doi", "")

                risk_level = "低风险"
                if "summary" in findings:
                    from modules.chinese_report_generator import _compute_overall_risk
                    overall = _compute_overall_risk(findings)
                    risk_level = overall.get("level", "低风险")

                paper.status = "detected"
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
                    )
                    review.setdefault("trigger", "auto_detection")
                    review_results.append(review)
                except Exception as e:
                    log.error("Review failed for %s: %s", paper.doi, e)
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
                )

        await asyncio.gather(*(_review_one(p) for p in high_risk_papers))

    # Stage 3: Generate Reports
    report_paths = []
    if review_results:
        tm.update_task(
            task_id,
            status=TaskStatus.GENERATING_REPORT,
            stage="generating_report",
            progress={"current": 0, "total": 1, "stage": "report"},
        )

        try:
            task_dir = TASKS_DIR / task_id
            report_paths = await generate_reports(review_results, task_dir, table_name)
        except Exception as e:
            log.error("Report generation failed: %s", e)
            tm.update_task(
                task_id,
                status=TaskStatus.FAILED,
                error=f"Report generation failed: {e}",
            )
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
        "confirmed_high": confirmed_high,
        "downgraded": downgraded,
        "reports_generated": len(report_paths),
        "review_skipped": skip_review,
    }

    tm.update_task(
        task_id,
        status=TaskStatus.COMPLETED,
        stage="done",
        result=result,
        papers=papers,
    )

    log.info(
        "Task %s completed: %d papers, %d high-risk detected, %d confirmed, %d downgraded",
        task_id, len(papers), len(high_risk_papers), confirmed_high, downgraded,
    )
