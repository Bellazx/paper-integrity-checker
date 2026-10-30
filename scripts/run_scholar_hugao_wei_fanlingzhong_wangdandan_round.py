#!/usr/bin/env python3
"""Full rereview round for 胡高伟、樊令仲、王丹丹."""
from __future__ import annotations

import argparse
import importlib.util
import time
from pathlib import Path

BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_chenghao_zhangrufei_round.py"


def load_module():
    spec = importlib.util.spec_from_file_location("scholar_three_full_review_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUND_NAME = "yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1"
    module.ROUND_DIR = BASE / "data" / module.ROUND_NAME
    module.CONFIG_DIR = module.ROUND_DIR / "config"
    module.SCHOLARS = ("胡高伟", "樊令仲", "王丹丹")
    module.SCHOLAR_LABEL = "胡高伟、樊令仲、王丹丹"
    module.INPUT_PROMPT_PATH = "胡高伟`、`樊令仲`或`王丹丹"
    module.BATCH_DESCRIPTION = "本批48篇"
    module.START_BATCH = 1003000
    module.ROUND_NUM = 416
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_hugaowei_fanlingzhong_wangdandan_round.py"
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("feed", "watch", "r1", "r2", "judge"))
    parser.add_argument("--target", type=int, default=8)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    module = load_module()
    if args.action == "feed":
        return module.feed()
    if args.action == "watch":
        while True:
            try:
                module.feed()
            except Exception as exc:
                module.log(f"ERROR: {type(exc).__name__}: {exc}")
            time.sleep(max(10, args.interval))
    return module.run_stage(args.action, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
