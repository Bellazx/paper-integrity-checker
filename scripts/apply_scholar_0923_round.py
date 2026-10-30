#!/usr/bin/env python3
"""Render judge reports and write final review results for the 0923 round."""
from __future__ import annotations

import importlib.util
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "apply_scholar_review_round.py"
ROUND_DIR = BASE / "data" / "yujing_scholar_0923_round_1"


def load_module():
    spec = importlib.util.spec_from_file_location("scholar_0923_apply_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载写回模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUND_DIR = ROUND_DIR
    module.BATCH_DIR = ROUND_DIR / "batches"
    module.JUDGE_DIR = ROUND_DIR / "results_judge"
    module.TRACKER = ROUND_DIR / "monitors" / "tracker_judge.json"
    module.APPLIED_PATH = ROUND_DIR / "monitors" / "applied.json"
    return module


if __name__ == "__main__":
    raise SystemExit(load_module().main())
