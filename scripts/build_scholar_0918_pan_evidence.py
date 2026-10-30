#!/usr/bin/env python3
"""Build deterministic evidence bundles for the later 0918 Pan scholar round."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND_DIR = BASE / "data" / "yujing_scholar_0918_pan_round_1"
BUILDER = Path("/home/library/.claude/skills/paper-batch-review/scripts/review_evidence.py")
PYTHON = BASE / "venv" / "bin" / "python"
TRACKER = ROUND_DIR / "monitors" / "evidence_tracker.json"
EXPECTED = 21


def load_item(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data[0] if isinstance(data, list) else data


def valid(path: Path, doi: str) -> bool:
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
    if valid(output, item["doi"]):
        return batch_num, "cached"
    env = os.environ.copy()
    env.update({key: "1" for key in (
        "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"
    )})
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
    if proc.returncode != 0 or not valid(output, item["doi"]):
        raise RuntimeError((proc.stdout or f"exit={proc.returncode}")[-1500:])
    return batch_num, "built"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    jobs = []
    for batch_path in (ROUND_DIR / "batches").glob("batch_*.json"):
        jobs.append((int(batch_path.stem.split("_")[1]), load_item(batch_path)))
    jobs.sort()
    if len(jobs) != EXPECTED:
        raise RuntimeError(f"证据任务应为 {EXPECTED}，实际为 {len(jobs)}")

    done: set[int] = set()
    failed: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(build_one, batch_num, item): (batch_num, item["doi"])
            for batch_num, item in jobs
        }
        for index, future in enumerate(as_completed(futures), 1):
            batch_num, doi = futures[future]
            try:
                _, mode = future.result()
                done.add(batch_num)
                print(f"[{index}/{EXPECTED}] batch_{batch_num}: {doi} {mode}", flush=True)
            except Exception as exc:
                failed[batch_num] = str(exc)
                print(f"[{index}/{EXPECTED}] batch_{batch_num}: {doi} FAILED: {exc}", flush=True)
            save_tracker(done, failed)
    print(f"evidence complete: done={len(done)} failed={len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
