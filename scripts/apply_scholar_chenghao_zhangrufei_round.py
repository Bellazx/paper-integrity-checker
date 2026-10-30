#!/usr/bin/env python3
"""Render and write final results for the 程颢/张如飞 scholar round."""
from __future__ import annotations

import importlib.util
from pathlib import Path

BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND_DIR = BASE / "data" / "yujing_scholar_chenghao_zhangrufei_round_1"
PDF_DIR = BASE / "data" / "output" / "yujing_scholar" / "review_v2"


def load_module():
    source = BASE / "scripts" / "apply_scholar_review_round.py"
    spec = importlib.util.spec_from_file_location("chenghao_zhangrufei_apply_shared", source)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUND_DIR = ROUND_DIR
    module.BATCH_DIR = ROUND_DIR / "batches"
    module.JUDGE_DIR = ROUND_DIR / "results_judge"
    module.TRACKER = ROUND_DIR / "monitors" / "tracker_judge.json"
    module.APPLIED_PATH = ROUND_DIR / "monitors" / "applied.json"
    module.PDF_DIR = PDF_DIR
    return module


if __name__ == "__main__":
    raise SystemExit(load_module().main())
