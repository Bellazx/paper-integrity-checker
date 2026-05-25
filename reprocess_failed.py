#!/usr/bin/env python3
"""Reprocess papers that failed due to disk-full."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from main import _analyze_one
from utils.db import insert_findings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reprocess")

PAPERS = [
    ("Nature0514", "10.1038__s41467-020-15578-1", "0514"),
    ("Nature0514", "10.1038__s41467-020-17860-8", "0514"),
    ("Nature0514", "10.1038__s41467-020-20527-z", "0514"),
    ("Nature0514", "10.1038__s41467-021-23723-7", "0514"),
    ("Nature0514", "10.1038__s41467-021-24203-8", "0514"),
    ("Nature0514", "10.1038__s41467-022-33080-8", "0514"),
    ("Nature0514", "10.1038__s41467-022-34238-0", "0514"),
    ("Nature-2", "10.1038__s41467-022-35250-0", "Nature-2"),
    ("Nature0514", "10.1038__s41467-023-36433-z", "0514"),
    ("nature-3", "10.1038__s41467-023-38331-w", "nature-3"),
    ("Nature0514", "10.1038__s41467-023-39759-w", "0514"),
    ("Nature0514", "10.1038__s41467-023-41699-4", "0514"),
    ("Nature0514", "10.1038__s41467-024-48100-y", "0514"),
    ("nature-3", "10.1038__s41467-024-48240-1", "nature-3"),
    ("nature-3", "10.1038__s41467-024-49022-5", "nature-3"),
    ("Nature-2", "10.1038__s41467-024-49801-0", "Nature-2"),
    ("Nature-2", "10.1038__s41467-024-49926-2", "Nature-2"),
    ("nature-3", "10.1038__s41467-024-49969-5", "nature-3"),
    ("nature-3", "10.1038__s41467-024-52300-x", "nature-3"),
    ("Nature-2", "10.1038__s41467-025-57628-6", "Nature-2"),
    ("Nature-2", "10.1038__s41557-024-01552-7", "Nature-2"),
    ("Nature-2", "10.1038__s41566-024-01503-1", "Nature-2"),
]

BASE = Path(__file__).resolve().parent

ok = 0
fail = 0
for input_batch, doi_dir, output_batch in PAPERS:
    input_dir = str(BASE / "data" / "input" / input_batch / doi_dir)
    output_dir = str(BASE / "data" / "output" / output_batch / doi_dir)
    cn_dir = str(BASE / "data" / "output" / output_batch / "chinese_reports")
    html_path = str(Path(input_dir) / "article.html") if (Path(input_dir) / "article.html").exists() else None

    try:
        log.info("Processing %s", doi_dir)
        findings = _analyze_one(input_dir, output_dir, skip_refs=False, chinese_reports_dir=cn_dir, author_type="通讯作者")
        insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path)
        log.info("OK: %s (score=%s)", doi_dir, findings["summary"]["total_issues"])
        ok += 1
    except Exception as e:
        log.error("FAILED: %s: %s", doi_dir, e)
        fail += 1

print(f"\nDone: {ok} OK, {fail} failed")
