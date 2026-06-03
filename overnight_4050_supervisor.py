#!/usr/bin/env python3
"""Keep the 4050 screening/review workflow moving overnight.

This is a guard process around watch_4050_reviews.py and run_4050_chunked.py.
It does not change paper-level verdicts. If the rate guard stops screening on a
small or still-growing sample, the supervisor logs the event, raises the minimum
sample floor for the next observation window, and restarts screening. The goal is
to avoid an unattended all-night stall while still leaving audit snapshots behind.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path(__file__).resolve().parent
TMP_DIR = BASE / "data" / "tmp" / "4050_review_watcher"
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = TMP_DIR / "overnight_supervisor.log"
STATE_FILE = TMP_DIR / "overnight_supervisor_state.json"

DB_CONFIG = {
    "server": "10.119.5.44",
    "user": "yujing",
    "password": "fengxian_YJ514",
    "database": "lunwenyujing",
    "charset": "utf8",
}

THRESHOLD = float(os.environ.get("OVERNIGHT_4050_THRESHOLD", "35"))
INITIAL_MIN_TOTAL = int(os.environ.get("OVERNIGHT_4050_MIN_TOTAL", "120"))
MIN_TOTAL_STEP = int(os.environ.get("OVERNIGHT_4050_MIN_TOTAL_STEP", "120"))
MAX_MIN_TOTAL = int(os.environ.get("OVERNIGHT_4050_MAX_MIN_TOTAL", "4050"))
INTERVAL_SECONDS = int(os.environ.get("OVERNIGHT_4050_INTERVAL_SECONDS", "90"))


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "threshold": THRESHOLD,
            "min_total_for_threshold": INITIAL_MIN_TOTAL,
            "starts": [],
            "threshold_events": [],
        }
    try:
        with STATE_FILE.open(encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("threshold", THRESHOLD)
    state.setdefault("min_total_for_threshold", INITIAL_MIN_TOTAL)
    state.setdefault("starts", [])
    state.setdefault("threshold_events", [])
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def ps_rows() -> list[dict]:
    proc = subprocess.run(
        ["ps", "-eo", "pid,ppid,etimes,pcpu,pmem,rss,cmd"],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, etimes, pcpu, pmem, rss, cmd = parts
        try:
            rows.append({
                "pid": int(pid),
                "ppid": int(ppid),
                "etimes": int(etimes),
                "pcpu": float(pcpu),
                "pmem": float(pmem),
                "rss": int(rss),
                "cmd": cmd,
            })
        except ValueError:
            continue
    return rows


def has_process(pattern: str) -> bool:
    current = os.getpid()
    return any(pattern in r["cmd"] and r["pid"] != current for r in ps_rows())


def query_counts() -> dict:
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN risk_level=N'高风险' THEN 1 ELSE 0 END) high,
                      SUM(CASE WHEN risk_level=N'低风险' THEN 1 ELSE 0 END) low,
                      SUM(CASE WHEN review_result IS NOT NULL AND review_result<>N''
                                AND review_report_url IS NOT NULL AND review_report_url<>N''
                               THEN 1 ELSE 0 END) reviewed
               FROM yujing_4050"""
        )
        row = cur.fetchone()
        total = int(row["total"] or 0)
        high = int(row["high"] or 0)
        return {
            "total": total,
            "high": high,
            "low": int(row["low"] or 0),
            "reviewed": int(row["reviewed"] or 0),
            "rate": high / total * 100.0 if total else 0.0,
        }
    finally:
        conn.close()


def start_watcher(min_total: int) -> None:
    cmd = [
        sys.executable,
        "-u",
        str(BASE / "watch_4050_reviews.py"),
        "--threshold",
        str(THRESHOLD),
        "--min-total-for-threshold",
        str(min_total),
        "--review-batch-size",
        "3",
        "--max-active-review-groups",
        "0",
        "--interval",
        "60",
    ]
    out = (TMP_DIR / "main.out.log").open("a", encoding="utf-8")
    subprocess.Popen(cmd, cwd=str(BASE), stdout=out, stderr=out, start_new_session=True)
    log(f"started watcher threshold={THRESHOLD} min_total={min_total}")


def start_screening() -> None:
    cmd = [sys.executable, "-u", str(BASE / "run_4050_chunked.py"), "--chunk", "150", "--workers", "4"]
    out = (TMP_DIR / "screening_35.out.log").open("a", encoding="utf-8")
    subprocess.Popen(cmd, cwd=str(BASE), stdout=out, stderr=out, start_new_session=True)
    log("started screening chunk=150 workers=4")


def main() -> int:
    state = load_state()
    log(
        f"supervisor starting threshold={THRESHOLD} "
        f"min_total={state['min_total_for_threshold']} interval={INTERVAL_SECONDS}s"
    )
    while True:
        state = load_state()
        counts = query_counts()
        watcher_running = has_process("watch_4050_reviews.py --threshold")
        screening_running = has_process("run_4050_chunked.py") or has_process("screen_4050.py --prefer-pdf --skip-done")
        log(
            "status "
            f"total={counts['total']} high={counts['high']} low={counts['low']} "
            f"rate={counts['rate']:.2f}% reviewed={counts['reviewed']} "
            f"watcher={watcher_running} screening={screening_running} "
            f"min_total={state['min_total_for_threshold']}"
        )

        if not watcher_running:
            start_watcher(int(state["min_total_for_threshold"]))

        if not screening_running:
            over = counts["rate"] > THRESHOLD and counts["total"] >= int(state["min_total_for_threshold"])
            if over:
                event = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "counts": counts,
                    "old_min_total": int(state["min_total_for_threshold"]),
                }
                state["min_total_for_threshold"] = min(
                    MAX_MIN_TOTAL,
                    max(counts["total"] + MIN_TOTAL_STEP, int(state["min_total_for_threshold"]) + MIN_TOTAL_STEP),
                )
                event["new_min_total"] = state["min_total_for_threshold"]
                event["action"] = "continue_observation_with_larger_sample_floor"
                state["threshold_events"].append(event)
                save_state(state)
                log(
                    "threshold event: "
                    f"rate={counts['rate']:.2f}% total={counts['total']}; "
                    f"raising min_total to {state['min_total_for_threshold']} and restarting"
                )
                if not has_process("watch_4050_reviews.py --threshold"):
                    start_watcher(int(state["min_total_for_threshold"]))
                start_screening()
            else:
                start_screening()

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
