#!/usr/bin/env python3
"""Isolated three-stage review round for 韩莉钧."""
from __future__ import annotations

import importlib.util
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("scholar_round_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载批次编排模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(Path(__file__).resolve())
    module.ROUND_NAME = "yujing_scholar_hanlijun_round_1"
    module.ROUND_DIR = BASE / "data" / module.ROUND_NAME
    module.CONFIG_DIR = module.ROUND_DIR / "config"
    module.SCHOLAR = "韩莉钧"
    module.PROMPT_TAG = "scholar_hanlijun_round1"
    module.INPUT_ROOT = BASE / "data" / "input" / "scholar" / module.SCHOLAR
    module.START_BATCH = 1001000
    module.ROUND_NUM = 410
    module.EXPECTED = 20
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_hanlijun_round.py"
    return module


if __name__ == "__main__":
    raise SystemExit(load_runner().main())
