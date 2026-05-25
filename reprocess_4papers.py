#!/usr/bin/env python3
"""Reprocess 4 OriginalPaper that had empty reports."""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from main import _analyze_one
from utils.db import insert_findings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reprocess4")

PAPERS = [
    ("Nature0514", "10.1038__s41556-025-01685-y", "0514"),
    ("Nature0514", "10.1038__s41567-021-01350-9", "0514"),
    ("Nature-2",   "10.1038__s41597-024-03567-8", "Nature-2"),
    ("Nature-2",   "10.1038__s41598-021-85000-3", "Nature-2"),
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
        log.info("OK: %s (issues=%d)", doi_dir, findings["summary"]["total_issues"])
        ok += 1
    except Exception as e:
        log.error("FAILED: %s: %s", doi_dir, e)
        fail += 1

print(f"\nDone: {ok} OK, {fail} failed")
