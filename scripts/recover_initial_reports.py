#!/usr/bin/env python3
"""Recover saved initial findings without repeating source analysis."""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import logging
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit, urlunsplit

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
TABLE = "yujing_audit"
OUTPUT = BASE / "data/output" / TABLE
LOG = logging.getLogger("initial-recovery")


def save_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)


def db_row(paper: dict) -> dict:
    from utils.db import get_connection

    cursor = get_connection().cursor(as_dict=True)
    try:
        cursor.execute(
            f"SELECT id,doi,title,risk_level,total_score,generation_time,report_url,"
            f"review_result,review_report_url,review_time FROM {TABLE} WHERE id=%s",
            (paper["id"],),
        )
        row = cursor.fetchone()
        if not row or row["doi"].lower() != paper["doi"].lower():
            raise ValueError("Target DB identity changed")
        return row
    finally:
        cursor.close()


def pdf_ready(findings: dict, path: Path) -> bool:
    # An old public PDF remains accessible while regeneration is incomplete.
    if not findings.get("pdf_generated") or not path.is_file():
        return False
    import fitz

    try:
        with fitz.open(path) as document:
            return document.page_count > 0 and bool(document[0].get_text().strip())
    except Exception:
        return False


def verified_write(paper: dict, findings: dict, url: str) -> dict:
    from utils.db import insert_findings

    for attempt in range(3):
        started = datetime.now()
        insert_findings(findings, chinese_reports_dir=str(OUTPUT / "chinese_reports"),
                        namespace=TABLE)
        row = db_row(paper)
        generated = row.get("generation_time")
        if isinstance(generated, str):
            generated = datetime.fromisoformat(generated)
        if (generated and generated >= started.replace(microsecond=0)
                and row["report_url"] == url
                and row["risk_level"] == findings["initial_risk_level"]
                and str(row["total_score"]) == str(findings["initial_risk_score"])):
            return row
        time.sleep(2 ** attempt)
    raise RuntimeError("DB write was not confirmed after 3 attempts")


def check_url(url: str) -> None:
    import requests

    parts = urlsplit(url)
    encoded = urlunsplit(parts._replace(path=quote(unquote(parts.path), safe="/")))
    with requests.head(encoded, timeout=(10, 30), allow_redirects=True) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Report URL returned HTTP {response.status_code}")


def recover_one(paper: dict, run_dir: str) -> dict:
    run = Path(run_dir)
    handler = logging.FileHandler(run / "logs" / f"{paper['paper']}.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    started = time.monotonic()
    try:
        from modules.chinese_report_generator import (
            _compute_overall_risk, _make_filename, _sanitize_paper_dict,
            generate_chinese_pdf,
        )
        from utils.db import _make_report_url, get_connection
        from utils.excel_metadata import find_batch_excel, load_batch_excel, merge_excel_metadata

        report = OUTPUT / "init" / paper["paper"] / "report.json"
        findings = json.loads(report.read_text())
        if findings["paper"]["doi"].lower() != paper["doi"].lower():
            raise ValueError("Saved findings DOI differs from selected input")
        row = db_row(paper)
        if any(row.get(key) for key in ("review_result", "review_report_url", "review_time")):
            raise ValueError("Review fields appeared since selection; preserve them for manual reconciliation")
        cursor = get_connection().cursor()
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE doi=%s", (paper["doi"],))
            if cursor.fetchone()[0] != 1:
                raise ValueError("DOI no longer identifies one DB row")
        finally:
            cursor.close()

        old_title = findings["paper"].get("title", "")
        old_path = OUTPUT / "chinese_reports" / _make_filename(paper["doi"], old_title)
        ready = pdf_ready(findings, old_path)
        if ready:
            pdf = old_path
        else:
            source = Path(paper["source"])
            excel = find_batch_excel(source.parent)
            if excel:
                findings = merge_excel_metadata(findings, load_batch_excel(excel))
            findings["paper"] = _sanitize_paper_dict(findings["paper"])
            import fitz

            original = Path(findings["paper"]["filepath"])
            if not original.resolve().is_relative_to(source.resolve()):
                raise ValueError("Saved source PDF is outside selected paper directory")
            with fitz.open(original) as document:
                first_pages = "\n\n".join(document[i].get_text() for i in range(min(4, len(document))))
            staging = run / "staging" / paper["paper"]
            staging.mkdir(parents=True, exist_ok=True)
            generated, metadata = generate_chinese_pdf(findings, str(staging), first_pages)
            if not generated:
                raise RuntimeError("Initial PDF regeneration failed")
            findings["paper"]["authors_full"] = metadata.get("authors_full", [])
            findings["paper"]["affiliations"] = metadata.get("affiliations", [])
            findings["pdf_generated"] = True
            findings["pdf_skipped_low_risk"] = False
            if not pdf_ready(findings, Path(generated)):
                raise RuntimeError("Generated PDF failed readability validation")
            pdf = OUTPUT / "chinese_reports" / Path(generated).name
            Path(generated).replace(pdf)
            staging.rmdir()

        risk = _compute_overall_risk(findings)
        findings["initial_risk_level"] = risk["level"]
        findings["initial_risk_score"] = risk["score"]
        save_json(report, findings)
        url = _make_report_url(findings, namespace=TABLE)
        if unquote(url.rsplit("/", 1)[-1]) != pdf.name:
            raise ValueError("PDF filename differs from DB report URL")
        row = verified_write(paper, findings, url)
        check_url(row["report_url"])
        return {"paper": paper["paper"], "id": paper["id"], "status": "success",
                "action": "db_only" if ready else "regenerate_pdf_and_db",
                "risk_level": row["risk_level"], "report_url": row["report_url"],
                "generation_time": str(row["generation_time"]),
                "elapsed_seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:
        LOG.exception("Recovery failed: %s", paper["paper"])
        return {"paper": paper["paper"], "id": paper["id"], "status": "failed",
                "error": str(exc), "elapsed_seconds": round(time.monotonic() - started, 1)}
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--supervise", action="store_true")
    args = parser.parse_args()
    os.chdir(BASE)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    manifest = json.loads(args.manifest.read_text())
    if manifest["table"] != TABLE:
        raise ValueError("This recovery must only write yujing_audit")
    run = args.manifest.parent / "recovery_20261026"
    run.mkdir(exist_ok=True)
    if args.supervise:
        workers = args.workers
        for restart in range(4):
            command = [sys.executable, "-u", str(Path(__file__).resolve()),
                       "--manifest", str(args.manifest.resolve()),
                       "--workers", str(workers), "--rounds", str(args.rounds)]
            LOG.info("Supervisor starting worker manager: workers=%d restart=%d", workers, restart)
            child = subprocess.run(command, cwd=BASE, stdin=subprocess.DEVNULL)
            status_path = run / "status.json"
            status = json.loads(status_path.read_text()) if status_path.exists() else {}
            if child.returncode == 0 or status.get("finished"):
                return child.returncode
            workers = max(1, workers // 2)
            LOG.error("Worker manager exited %s; restart with %d workers", child.returncode, workers)
            time.sleep(10)
        return 1
    with (run / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (run / "logs").mkdir(exist_ok=True)
        selection_path = run / "selection.json"
        if selection_path.exists():
            papers = json.loads(selection_path.read_text())
        else:
            threshold = datetime.fromisoformat(manifest["launched_at"]).replace(tzinfo=None)
            from utils.db import get_connection

            target_ids = {p["id"] for p in manifest["papers"]}
            cursor = get_connection().cursor(as_dict=True)
            try:
                cursor.execute(
                    f"SELECT id,doi,title,risk_level,total_score,generation_time,report_url,"
                    f"review_result,review_report_url,review_time FROM {TABLE} WHERE id BETWEEN %s AND %s",
                    (min(target_ids), max(target_ids)),
                )
                rows = {r["id"]: r for r in cursor.fetchall() if r["id"] in target_ids}
            finally:
                cursor.close()
            papers = []
            snapshots = []
            for paper in manifest["papers"]:
                row = rows[paper["id"]]
                if row["doi"].lower() != paper["doi"].lower():
                    raise ValueError("Target DB identity changed")
                date = row["generation_time"]
                if isinstance(date, str):
                    date = datetime.fromisoformat(date)
                if date and date >= threshold:
                    continue
                papers.append(paper)
                snapshots.append(row)
                before = run / "before" / paper["paper"]
                before.mkdir(parents=True, exist_ok=True)
                source = OUTPUT / "init" / paper["paper"] / "report.json"
                (before / "report.json").write_bytes(source.read_bytes())
            save_json(run / "db_before.json", snapshots)
            save_json(selection_path, papers)
        status_path = run / "status.json"
        previous = json.loads(status_path.read_text()) if status_path.exists() else {}
        results = previous.get("results", {})

        def checkpoint(round_no):
            good = sum(r["status"] == "success" for r in results.values())
            save_json(status_path, {"updated_at": datetime.now().isoformat(),
                      "pid": os.getpid(), "workers": args.workers, "round": round_no,
                      "selected": len(papers), "success": good,
                      "remaining": len(papers) - good, "results": results})

        # Resolve completed PDFs first; these require no model requests.
        from modules.chinese_report_generator import _make_filename

        for paper in papers:
            if results.get(paper["paper"], {}).get("status") == "success":
                continue
            findings = json.loads((OUTPUT / "init" / paper["paper"] / "report.json").read_text())
            pdf = OUTPUT / "chinese_reports" / _make_filename(paper["doi"], findings["paper"].get("title", ""))
            if pdf_ready(findings, pdf):
                results[paper["paper"]] = recover_one(paper, str(run))
                checkpoint(0)
                LOG.info("DB reconciliation: %s %s", paper["paper"], results[paper["paper"]]["status"])
        for round_no in range(1, args.rounds + 1):
            pending = [p for p in papers if results.get(p["paper"], {}).get("status") != "success"]
            checkpoint(round_no)
            if not pending:
                break
            LOG.info("Round %d: %d pending, %d independent workers", round_no, len(pending), args.workers)
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=args.workers, mp_context=multiprocessing.get_context("spawn"),
                # Python 3.12 can stall replenishing workers after their task limit.
            ) as pool:
                futures = {pool.submit(recover_one, p, str(run)): p for p in pending}
                for future in concurrent.futures.as_completed(futures):
                    paper = futures[future]
                    results[paper["paper"]] = future.result()
                    checkpoint(round_no)
                    LOG.info("%s: %s", paper["paper"], results[paper["paper"]]["status"])
            if round_no < args.rounds:
                time.sleep(15)
        remaining = [r for r in results.values() if r["status"] != "success"]
        save_json(run / "failed_papers.json", remaining)
        status = json.loads(status_path.read_text())
        status["finished"] = True
        save_json(status_path, status)
        LOG.info("Recovery finished: %d successful, %d failed", len(papers) - len(remaining), len(remaining))
        return 1 if remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
