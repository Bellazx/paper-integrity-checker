#!/usr/bin/env python3
"""Build deterministic evidence bundles for the 0923 scholar round."""
from __future__ import annotations

import importlib.util
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "build_scholar_0918_pan_evidence.py"
ROUND_DIR = BASE / "data" / "yujing_scholar_0923_round_1"


def load_module():
    spec = importlib.util.spec_from_file_location("scholar_0923_evidence", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载证据构建模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUND_DIR = ROUND_DIR
    module.TRACKER = ROUND_DIR / "monitors" / "evidence_tracker.json"
    module.EXPECTED = 26
    return module


if __name__ == "__main__":
    raise SystemExit(load_module().main())
