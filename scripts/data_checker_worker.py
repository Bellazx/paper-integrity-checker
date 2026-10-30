#!/usr/bin/env python3
"""Run source-data checks in a disposable process.

Some spreadsheet/PDF native libraries can terminate the interpreter with a
native crash.  Keeping this worker separate lets the paper pipeline finish and
record a diagnostic instead of losing the whole batch worker.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from modules.data_checker import check_data_with_validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("data_dirs", nargs="+")
    args = parser.parse_args()

    anomalies, warnings = check_data_with_validation(args.data_dirs)
    Path(args.output).write_text(
        json.dumps({"anomalies": anomalies, "warnings": warnings}, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
