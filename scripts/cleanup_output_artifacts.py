#!/usr/bin/env python3
"""CLI for safely removing stale extracted images from data/output."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from utils.output_cleanup import cleanup_output_images


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(BASE_DIR / "data" / "output"))
    parser.add_argument("--min-age-hours", type=float, default=24.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = cleanup_output_images(
        args.root,
        min_age_seconds=max(0, int(args.min_age_hours * 3600)),
        dry_run=args.dry_run,
    )
    print(json.dumps(stats.to_dict(), ensure_ascii=False, indent=2))
    return 1 if stats.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
