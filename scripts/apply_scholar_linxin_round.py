#!/usr/bin/env python3
"""Render Judge reports and write final review results for 林欣."""
from __future__ import annotations
import importlib.util
from pathlib import Path

BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts/apply_scholar_review_round.py"
ROUND_DIR = BASE / "data/yujing_scholar_linxin_round_1"
spec = importlib.util.spec_from_file_location("linxin_apply_shared", SOURCE)
if not spec or not spec.loader:
    raise RuntimeError(f"无法加载写回模块: {SOURCE}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROUND_DIR = ROUND_DIR
module.BATCH_DIR = ROUND_DIR / "batches"
module.JUDGE_DIR = ROUND_DIR / "results_judge"
module.TRACKER = ROUND_DIR / "monitors/tracker_judge.json"
module.APPLIED_PATH = ROUND_DIR / "monitors/applied.json"

if __name__ == "__main__":
    raise SystemExit(module.main())
