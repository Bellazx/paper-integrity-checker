#!/usr/bin/env python3
"""Apply final results for the Wang Jiulin scholar round."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "apply_scholar_review_round.py"
ROUND_DIR = BASE / "data" / "yujing_scholar_wangjiulin_round_1"
HIGH_RECHECK = ROUND_DIR / "monitors" / "highrisk_recheck.json"


def load_module():
    spec = importlib.util.spec_from_file_location("scholar_wangjiulin_apply_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载写回模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUND_DIR = ROUND_DIR
    module.BATCH_DIR = ROUND_DIR / "batches"
    module.JUDGE_DIR = ROUND_DIR / "results_judge"
    module.TRACKER = ROUND_DIR / "monitors" / "tracker_judge.json"
    module.APPLIED_PATH = ROUND_DIR / "monitors" / "applied.json"

    def approved_highrisk() -> set[int]:
        try:
            data = json.loads(HIGH_RECHECK.read_text(encoding="utf-8"))
            return {int(value) for value in data.get("approved_highrisk_batches", [])}
        except Exception:
            return set()

    def should_apply(batch_num: int, paper: dict, judge: dict) -> bool:
        if module.normalize_result(judge.get("result")) != "高风险":
            return True
        allowed = batch_num in approved_highrisk()
        if not allowed:
            print(f"hold high-risk pending independent recheck: batch_{batch_num}", flush=True)
        return allowed

    module.should_apply = should_apply
    return module


if __name__ == "__main__":
    raise SystemExit(load_module().main())
