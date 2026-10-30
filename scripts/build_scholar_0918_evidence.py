#!/usr/bin/env python3
"""Build and validate deterministic evidence bundles for scholar 0918."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND_DIR = BASE / "data" / "yujing_scholar_0918_round_1"
BUILDER = Path("/home/library/.claude/skills/paper-batch-review/scripts/review_evidence.py")
PYTHON = BASE / "venv" / "bin" / "python"
TRACKER = ROUND_DIR / "monitors" / "evidence_tracker.json"


def load_item(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data[0] if isinstance(data, list) else data


def is_valid(path: Path, doi: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            str(data.get("doi") or "").lower() == doi.lower()
            and isinstance(data.get("coverage_manifest", {}).get("must_address"), list)
        )
    except Exception:
        return False


def save_tracker(done: set[int], failed: dict[int, str]) -> None:
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "done": sorted(done),
        "failed": {str(key): value for key, value in sorted(failed.items())},
    }
    TRACKER.parent.mkdir(parents=True, exist_ok=True)
    tmp = TRACKER.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, TRACKER)


def build_one(batch_num: int, item: dict) -> tuple[int, str]:
    output = Path(item["evidence_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    if is_valid(output, item["doi"]):
        return batch_num, "cached"
    env = os.environ.copy()
    env.update({
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    })
    proc = subprocess.run(
        [
            str(PYTHON), str(BUILDER), "--doi", item["doi"],
            "--input-dir", item["input_dir"], "--report-json", item["report_json"],
            "--out", str(output),
        ],
        cwd=BASE,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0 or not is_valid(output, item["doi"]):
        raise RuntimeError((proc.stdout or f"exit={proc.returncode}")[-1500:])
    return batch_num, "built"


def priority(batch_num: int, in_flight: set[int]) -> tuple[int, int]:
    if (ROUND_DIR / "results" / f"batch_{batch_num:04d}.json").is_file():
        return 0, batch_num
    if batch_num in in_flight:
        return 1, batch_num
    return 2, batch_num


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    tracker_r1 = ROUND_DIR / "monitors" / "tracker.json"
    try:
        in_flight = {int(value) for value in json.loads(tracker_r1.read_text()).get("in_flight", [])}
    except Exception:
        in_flight = set()
    jobs = []
    for batch_path in (ROUND_DIR / "batches").glob("batch_*.json"):
        batch_num = int(batch_path.stem.split("_")[1])
        jobs.append((batch_num, load_item(batch_path)))
    jobs.sort(key=lambda pair: priority(pair[0], in_flight))
    if len(jobs) != 185:
        raise RuntimeError(f"证据任务应为 185，实际为 {len(jobs)}")

    done: set[int] = set()
    failed: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(build_one, batch_num, item): (batch_num, item["doi"])
                   for batch_num, item in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            batch_num, doi = futures[future]
            try:
                _, mode = future.result()
                done.add(batch_num)
                print(f"[{index}/185] batch_{batch_num}: {doi} {mode}", flush=True)
            except Exception as exc:
                failed[batch_num] = str(exc)
                print(f"[{index}/185] batch_{batch_num}: {doi} FAILED: {exc}", flush=True)
            save_tracker(done, failed)
    print(f"evidence complete: done={len(done)} failed={len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
