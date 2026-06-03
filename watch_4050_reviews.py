#!/usr/bin/env python3
"""Monitor the yujing_4050 screening run and launch review work in small groups.

The watcher has two jobs:
1. Stop the initial-screening batch if the DB high-risk rate rises above a configured
   audit threshold.
2. Whenever at least N unreviewed high-risk papers exist, start one review worker for
   the next N papers so screening and review can overlap without starting too many
   heavy LLM jobs.

The worker mode reviews its DOI group sequentially, then renders review PDFs and updates
the target DB table through the existing generate_review_report.py script. It does not
change the review logic.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path(__file__).resolve().parent
TABLE = "yujing_4050"
INPUT_BASE = BASE / "data" / "input" / "4050-matched"
OUTPUT_BASE = BASE / "data" / "output" / TABLE
REVIEW_OUTPUT_DIR = BASE / "data" / "output" / "review_v2" / TABLE
TMP_DIR = BASE / "data" / "tmp" / "4050_review_watcher"
STATE_FILE = TMP_DIR / "state.json"
LOG_FILE = TMP_DIR / "watcher.log"
METRICS_FILE = TMP_DIR / "metrics.jsonl"
REPORT_SCRIPT = Path("/opt/.claude/skills/paper-batch-review/scripts/generate_review_report.py")

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}


def _setup_logging() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
    )


log = logging.getLogger("watch-4050")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"version": 1, "groups": {}, "in_progress": {}, "completed": {}, "failed": {}}
    try:
        with STATE_FILE.open(encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        backup = STATE_FILE.with_suffix(f".bad.{int(time.time())}.json")
        STATE_FILE.rename(backup)
        log.warning("State file was unreadable; moved to %s", backup)
        return {"version": 1, "groups": {}, "in_progress": {}, "completed": {}, "failed": {}}
    state.setdefault("version", 1)
    state.setdefault("groups", {})
    state.setdefault("in_progress", {})
    state.setdefault("completed", {})
    state.setdefault("failed", {})
    return state


def _save_state(state: dict) -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(STATE_FILE)


def _doi_from_dir(d: Path) -> str:
    doi_file = d / "doi.txt"
    if doi_file.exists():
        text = doi_file.read_text(encoding="utf-8", errors="ignore").strip()
        if text:
            return text
    name = d.name.split("_", 1)[1] if "_" in d.name else d.name
    return name.replace("__", "/")


def _find_input_dir(doi: str) -> Path | None:
    target = doi.lower()
    if not INPUT_BASE.exists():
        return None
    for d in INPUT_BASE.iterdir():
        if not d.is_dir():
            continue
        try:
            if _doi_from_dir(d).lower() == target:
                return d
        except Exception:
            continue
    return None


def _find_paths(doi: str) -> tuple[str | None, str | None]:
    input_dir = _find_input_dir(doi)
    if input_dir:
        report = OUTPUT_BASE / input_dir.name / "report.json"
        if report.exists():
            return str(report), str(input_dir)

    # Fallback for rows whose source directory name changed or was manually moved.
    target = doi.lower()
    for report in OUTPUT_BASE.glob("*/report.json"):
        try:
            with report.open(encoding="utf-8") as f:
                paper_doi = (json.load(f).get("paper", {}).get("doi") or "").lower()
            if paper_doi == target:
                return str(report), str(report.parent)
        except Exception:
            continue
    return None, str(input_dir) if input_dir else None


def _query_counts() -> dict:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(f"SELECT COUNT(*) AS total FROM {TABLE}")
        total = cur.fetchone()["total"]
        cur.execute(f"SELECT COUNT(*) AS high FROM {TABLE} WHERE risk_level=N'高风险'")
        high = cur.fetchone()["high"]
        cur.execute(f"SELECT COUNT(*) AS low FROM {TABLE} WHERE risk_level=N'低风险'")
        low = cur.fetchone()["low"]
        cur.execute(
            f"""SELECT COUNT(*) AS reviewed FROM {TABLE}
                WHERE review_result IS NOT NULL AND review_result<>N''
                  AND review_report_url IS NOT NULL AND review_report_url<>N''"""
        )
        reviewed = cur.fetchone()["reviewed"]
        cur.execute(f"SELECT MIN(generation_time) AS first_time, MAX(generation_time) AS last_time FROM {TABLE}")
        time_row = cur.fetchone()
        cur.execute(
            f"""SELECT COUNT(*) AS bad FROM {TABLE}
                WHERE risk_level=N'低风险'
                  AND (pic_risk_level=N'高风险'
                       OR data_risk_level=N'高风险'
                       OR ref_risk_level=N'高风险')"""
        )
        low_with_high_dim = cur.fetchone()["bad"]
        return {
            "total": total,
            "high": high,
            "low": low,
            "rate": (high / total * 100.0) if total else 0.0,
            "low_with_high_dim": low_with_high_dim,
            "reviewed": reviewed,
            "first_time": time_row.get("first_time"),
            "last_time": time_row.get("last_time"),
        }
    finally:
        conn.close()


def _target_total() -> int:
    try:
        return sum(1 for d in INPUT_BASE.iterdir() if d.is_dir())
    except Exception:
        return 0


def _process_snapshot() -> dict:
    proc = subprocess.run(
        ["ps", "-eo", "pid,ppid,etimes,pcpu,pmem,rss,cmd"],
        capture_output=True,
        text=True,
        check=False,
    )
    categories = {
        "screening_runner": [],
        "screening_worker": [],
        "review_watcher": [],
        "review_worker": [],
        "review_llm": [],
        "review_evidence": [],
    }
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, etimes, pcpu, pmem, rss, cmd = parts
        try:
            item = {
                "pid": int(pid),
                "ppid": int(ppid),
                "elapsed_seconds": int(etimes),
                "cpu_percent": float(pcpu),
                "mem_percent": float(pmem),
                "rss_mb": round(int(rss) / 1024, 1),
                "cmd": cmd[:240],
            }
        except ValueError:
            continue
        if "run_4050_chunked.py" in cmd:
            categories["screening_runner"].append(item)
        elif "screen_4050.py --prefer-pdf --skip-done" in cmd:
            categories["screening_worker"].append(item)
        elif "watch_4050_reviews.py --threshold" in cmd:
            categories["review_watcher"].append(item)
        elif "watch_4050_reviews.py --worker" in cmd:
            categories["review_worker"].append(item)
        elif "/usr/bin/claude" in cmd:
            categories["review_llm"].append(item)
        elif "review_evidence.py" in cmd:
            categories["review_evidence"].append(item)

    aggregate = {}
    for name, items in categories.items():
        aggregate[name] = {
            "count": len(items),
            "cpu_percent": round(sum(x["cpu_percent"] for x in items), 1),
            "rss_mb": round(sum(x["rss_mb"] for x in items), 1),
            "max_elapsed_seconds": max((x["elapsed_seconds"] for x in items), default=0),
        }
    return {"aggregate": aggregate, "processes": categories}


def _ensure_metrics_baseline(state: dict, counts: dict) -> None:
    baseline = state.setdefault("metrics_baseline", {})
    if not baseline:
        baseline.update({
            "started_at": _now(),
            "started_epoch": time.time(),
            "total": counts["total"],
            "high": counts["high"],
            "reviewed": counts["reviewed"],
        })
        _save_state(state)


def _append_metrics(counts: dict, active: dict[str, int], state: dict, args: argparse.Namespace) -> None:
    _ensure_metrics_baseline(state, counts)
    baseline = state.get("metrics_baseline", {})
    now_epoch = time.time()
    elapsed = max(1.0, now_epoch - float(baseline.get("started_epoch") or now_epoch))
    screened_since = max(0, counts["total"] - int(baseline.get("total") or 0))
    reviewed_since = max(0, counts["reviewed"] - int(baseline.get("reviewed") or 0))
    target = _target_total()
    remaining = max(0, target - counts["total"]) if target else None
    screening_per_hour = screened_since / elapsed * 3600.0
    review_per_hour = reviewed_since / elapsed * 3600.0
    eta_hours = (remaining / screening_per_hour) if remaining is not None and screening_per_hour > 0 else None
    payload = {
        "timestamp": _now(),
        "table": TABLE,
        "threshold_percent": args.threshold,
        "target_total": target,
        "screening": {
            "total_done": counts["total"],
            "remaining": remaining,
            "high": counts["high"],
            "low": counts["low"],
            "high_rate_percent": round(counts["rate"], 4),
            "low_with_high_dim": counts["low_with_high_dim"],
            "first_generation_time": str(counts.get("first_time") or ""),
            "last_generation_time": str(counts.get("last_time") or ""),
            "since_watcher_start": screened_since,
            "papers_per_hour_since_watcher_start": round(screening_per_hour, 2),
            "eta_hours_at_current_speed": round(eta_hours, 2) if eta_hours is not None else None,
        },
        "review": {
            "db_reviewed": counts["reviewed"],
            "since_watcher_start": reviewed_since,
            "papers_per_hour_since_watcher_start": round(review_per_hour, 2),
            "active_groups": len(active),
            "state_completed": len(state.get("completed", {})),
            "state_failed": len(state.get("failed", {})),
            "state_in_progress": len(state.get("in_progress", {})),
        },
        "resources": _process_snapshot(),
    }
    with METRICS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    log.info(
        "METRICS screening=%d/%s speed=%.1f/h eta=%s h | review_db=%d active_groups=%d | rss screen=%.0fMB review_llm=%.0fMB",
        counts["total"],
        target or "?",
        screening_per_hour,
        f"{eta_hours:.1f}" if eta_hours is not None else "?",
        counts["reviewed"],
        len(active),
        payload["resources"]["aggregate"]["screening_worker"]["rss_mb"],
        payload["resources"]["aggregate"]["review_llm"]["rss_mb"],
    )


def _query_pending_high_risk() -> list[dict]:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(
            f"""SELECT id, doi, risk_level, review_result, review_report_url, generation_time,
                       total_score, pic_score, data_score, ref_score,
                       pic_risk_level, data_risk_level, ref_risk_level
                FROM {TABLE}
                WHERE risk_level=N'高风险'
                  AND doi IS NOT NULL
                  AND (review_result IS NULL OR review_result=N''
                       OR review_report_url IS NULL OR review_report_url=N'')
                ORDER BY generation_time ASC, id ASC"""
        )
        return list(cur.fetchall())
    finally:
        conn.close()


def _write_audit_snapshot(counts: dict) -> Path:
    pending = _query_pending_high_risk()
    path = TMP_DIR / f"audit_high_rate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = {"created_at": _now(), "counts": counts, "pending_high_risk": pending[:50]}
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


def _stop_screening_processes() -> list[int]:
    """Terminate only the known 4050 screening commands, not this watcher."""
    current = os.getpid()
    proc = subprocess.run(
        ["ps", "-eo", "pid,cmd"],
        capture_output=True,
        text=True,
        check=False,
    )
    killed: list[int] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, _, cmd = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current:
            continue
        is_screen = "screen_4050.py --prefer-pdf --skip-done" in cmd
        is_runner = "run_4050_chunked.py" in cmd and "--chunk" in cmd
        if not (is_screen or is_runner):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            pass
    return killed


def _eligible_pending(rows: list[dict], state: dict, stale_seconds: int, max_attempts: int) -> list[dict]:
    now = time.time()
    eligible = []
    in_progress = state.get("in_progress", {})
    failed = state.get("failed", {})
    completed = state.get("completed", {})
    for row in rows:
        doi = row.get("doi")
        if not doi or doi in completed:
            continue
        started = in_progress.get(doi, {}).get("started_epoch")
        if started and now - float(started) < stale_seconds:
            continue
        attempts = int(failed.get(doi, {}).get("attempts", 0))
        if attempts >= max_attempts:
            continue
        eligible.append(row)
    return eligible


def _group_id(dois: list[str]) -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{abs(hash(tuple(dois))) % 100000:05d}"


def _launch_group(dois: list[str], state: dict) -> int:
    group_id = _group_id(dois)
    cmd = [
        sys.executable,
        "-u",
        str(BASE / "watch_4050_reviews.py"),
        "--worker",
        "--group-id",
        group_id,
        "--dois",
        *dois,
    ]
    log.info("Launching review group %s: %s", group_id, ", ".join(dois))
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE),
        stdout=(TMP_DIR / f"group_{group_id}.out.log").open("a", encoding="utf-8"),
        stderr=(TMP_DIR / f"group_{group_id}.err.log").open("a", encoding="utf-8"),
        start_new_session=True,
    )
    state["groups"][group_id] = {
        "pid": proc.pid,
        "dois": dois,
        "status": "running",
        "started_at": _now(),
        "started_epoch": time.time(),
    }
    for doi in dois:
        state["in_progress"][doi] = {
            "group_id": group_id,
            "pid": proc.pid,
            "started_at": _now(),
            "started_epoch": time.time(),
        }
    _save_state(state)
    return proc.pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _adopt_active_groups(state: dict) -> dict[str, int]:
    active: dict[str, int] = {}
    for gid, group in state.get("groups", {}).items():
        if group.get("status") != "running":
            continue
        try:
            pid = int(group.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            active[gid] = pid
    return active


def _reap_groups(active: dict[str, int], state: dict) -> None:
    for gid, pid in list(active.items()):
        if _pid_alive(pid):
            continue
        group = state.get("groups", {}).get(gid, {})
        dois = group.get("dois", [])
        status_path = TMP_DIR / f"group_{gid}_status.json"
        status_payload = {}
        if status_path.exists():
            try:
                with status_path.open(encoding="utf-8") as f:
                    status_payload = json.load(f)
            except Exception as e:
                log.warning("Could not read group status %s: %s", status_path, e)
        completed_dois = set(status_payload.get("completed", []))
        failed_dois = set(status_payload.get("failed", []))
        if not completed_dois and not failed_dois:
            failed_dois = set(dois)
        group_ok = bool(completed_dois) and not failed_dois
        group["status"] = "done" if group_ok else "failed"
        group["returncode"] = 0 if group_ok else 1
        group["finished_at"] = _now()
        for doi in dois:
            state.get("in_progress", {}).pop(doi, None)
            if doi in completed_dois:
                state.setdefault("completed", {})[doi] = {"group_id": gid, "finished_at": _now()}
                state.get("failed", {}).pop(doi, None)
            else:
                item = state.setdefault("failed", {}).setdefault(doi, {"attempts": 0})
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["last_group_id"] = gid
                item["last_failed_at"] = _now()
        log.info(
            "Review group %s finished status=%s completed=%d failed=%d",
            gid,
            group["status"],
            len(completed_dois),
            len(failed_dois),
        )
        active.pop(gid, None)
        _save_state(state)


async def _review_one(doi: str) -> dict:
    from api.services.review import run_review_single

    report_path, input_dir = _find_paths(doi)
    if not report_path or not input_dir:
        raise FileNotFoundError(f"report/input path not found for {doi}")
    log.info("Reviewing %s", doi)
    result = await run_review_single(
        doi=doi,
        report_json_path=report_path,
        input_dir=input_dir,
        output_dir=str(REVIEW_OUTPUT_DIR),
    )
    result["_report_json"] = report_path
    result["_input_dir"] = input_dir
    return result


def _is_review_error(result: dict) -> bool:
    return result.get("trigger") == "review_error"


async def run_worker(group_id: str, dois: list[str]) -> int:
    _setup_logging()
    REVIEW_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    status_path = TMP_DIR / f"group_{group_id}_status.json"
    for doi in dois:
        try:
            result = await _review_one(doi)
        except Exception as e:
            log.exception("Review failed for %s", doi)
            result = {
                "doi": doi,
                "result": "高风险",
                "trigger": "review_error",
                "image_review": f"复核过程中出现错误：{e}",
                "data_review": "复核过程中出现错误。",
                "ref_review": "复核过程中出现错误。",
                "verdict": "建议高风险",
                "reason": f"复核过程中出现错误：{e}",
            }
        results.append(result)
        result_path = TMP_DIR / f"group_{group_id}_partial.json"
        with result_path.open("w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    errors = [r for r in results if _is_review_error(r)]
    renderable = [r for r in results if not _is_review_error(r)]
    status = {
        "group_id": group_id,
        "completed": [],
        "failed": [r.get("doi") for r in errors if r.get("doi")],
        "finished_at": _now(),
    }
    if errors:
        log.error("Group %s has %d review_error result(s); failed DOIs: %s",
                  group_id, len(errors), ", ".join(status["failed"]))

    if not renderable:
        with status_path.open("w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        return 2

    results_path = TMP_DIR / f"group_{group_id}_results.json"
    with results_path.open("w", encoding="utf-8") as f:
        json.dump(renderable, f, ensure_ascii=False, indent=2)

    cmd = [
        sys.executable,
        str(REPORT_SCRIPT),
        "--results",
        str(results_path),
        "--output",
        str(REVIEW_OUTPUT_DIR),
        "--table",
        TABLE,
        "--namespace",
        TABLE,
    ]
    log.info("Generating review PDFs and updating DB: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(BASE),
        capture_output=True,
        text=True,
        timeout=int(os.environ.get("WATCH_4050_REPORT_TIMEOUT_SECONDS", "900")),
    )
    if proc.stdout:
        log.info("Report stdout:\n%s", proc.stdout[-3000:])
    if proc.returncode != 0:
        log.error("Report generation failed rc=%s stderr=%s", proc.returncode, proc.stderr[-3000:])
        status["failed"].extend([r.get("doi") for r in renderable if r.get("doi")])
        status["failed"] = list(dict.fromkeys(status["failed"]))
        with status_path.open("w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        return proc.returncode or 1
    status["completed"] = [r.get("doi") for r in renderable if r.get("doi")]
    with status_path.open("w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    return 2 if errors else 0


def run_watch(args: argparse.Namespace) -> int:
    _setup_logging()
    state = _load_state()
    active: dict[str, int] = _adopt_active_groups(state)
    log.info(
        "Starting watcher: threshold=%.2f%% batch_size=%d max_active_review_groups=%d interval=%ds",
        args.threshold,
        args.review_batch_size,
        args.max_active_review_groups,
        args.interval,
    )

    while True:
        state = _load_state()
        active.update({gid: pid for gid, pid in _adopt_active_groups(state).items() if gid not in active})
        _reap_groups(active, state)
        counts = _query_counts()
        log.info(
            "DB total=%d high=%d low=%d rate=%.2f%% low_with_high_dim=%d active_review_groups=%d",
            counts["total"],
            counts["high"],
            counts["low"],
            counts["rate"],
            counts["low_with_high_dim"],
            len(active),
        )
        _append_metrics(counts, active, state, args)

        over_threshold = counts["rate"] > args.threshold
        under_sample_floor = counts["total"] < args.min_total_for_threshold
        if over_threshold and under_sample_floor:
            log.warning(
                "High-risk rate %.2f%% > %.2f%% but total=%d < min_total_for_threshold=%d; continuing observation",
                counts["rate"],
                args.threshold,
                counts["total"],
                args.min_total_for_threshold,
            )

        if over_threshold and not under_sample_floor:
            audit = _write_audit_snapshot(counts)
            killed = _stop_screening_processes()
            log.error(
                "High-risk rate %.2f%% > %.2f%%. Stopped screening pids=%s; audit=%s",
                counts["rate"],
                args.threshold,
                killed,
                audit,
            )
            return 3

        rows = _query_pending_high_risk()
        eligible = _eligible_pending(rows, state, args.stale_seconds, args.max_attempts)
        capacity = args.max_active_review_groups - len(active)
        while capacity > 0 and len(eligible) >= args.review_batch_size:
            group = eligible[: args.review_batch_size]
            eligible = eligible[args.review_batch_size :]
            dois = [str(r["doi"]) for r in group]
            pid = _launch_group(dois, state)
            active[state["in_progress"][dois[0]]["group_id"]] = pid
            capacity -= 1

        time.sleep(args.interval)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Monitor yujing_4050 screening and launch reviews")
    ap.add_argument("--threshold", type=float, default=33.0, help="Stop screening when high-risk rate is greater than this percent")
    ap.add_argument(
        "--min-total-for-threshold",
        type=int,
        default=0,
        help="Do not stop screening for high-risk-rate threshold until at least this many rows exist",
    )
    ap.add_argument("--interval", type=int, default=60, help="Watcher polling interval in seconds")
    ap.add_argument("--review-batch-size", type=int, default=3, help="Start one review worker for each full group of N high-risk papers")
    ap.add_argument("--max-active-review-groups", type=int, default=1, help="Limit concurrent review workers")
    ap.add_argument("--stale-seconds", type=int, default=6 * 3600, help="Reclaim in-progress DOI claims after this many seconds")
    ap.add_argument("--max-attempts", type=int, default=2, help="Max review attempts per DOI before leaving it for manual retry")
    ap.add_argument("--worker", action="store_true", help="Run one review worker group")
    ap.add_argument("--group-id", default="", help="Worker group id")
    ap.add_argument("--dois", nargs="*", default=[], help="DOIs for worker mode")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.worker:
        if not args.group_id or not args.dois:
            print("--worker requires --group-id and --dois", file=sys.stderr)
            return 2
        return asyncio.run(run_worker(args.group_id, args.dois))
    return run_watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
