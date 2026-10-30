#!/usr/bin/env python3
"""Render judge results and write the 6-paper round to yujing_4050_202607."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND_DIR = BASE / "data" / "yujing_4050_sep30_comm_highrisk_round_1"
PDF_DIR = BASE / "data" / "output" / "yujing_4050_202607" / "review_v2"
REPORT_SCRIPT = BASE / "scripts" / "generate_review_report.py"
REPORT_PYTHON = BASE / "venv" / "bin" / "python"
PUBLIC_HOST = "202.120.57.100"
DB = dict(server="10.119.5.44", user="yujing", password="fengxian_YJ514",
          database="lunwenyujing", charset="utf8", timeout=30)


def load_module():
    source = BASE / "scripts" / "apply_scholar_review_round.py"
    spec = importlib.util.spec_from_file_location("sep30_apply_shared", source)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载写回模块: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROUND_DIR = ROUND_DIR
    module.BATCH_DIR = ROUND_DIR / "batches"
    module.JUDGE_DIR = ROUND_DIR / "results_judge"
    module.TRACKER = ROUND_DIR / "monitors" / "tracker_judge.json"
    module.APPLIED_PATH = ROUND_DIR / "monitors" / "applied.json"
    module.PDF_DIR = PDF_DIR

    def render(paper: dict, result: dict, batch_num: int) -> Path:
        final = module.enrich_for_report(paper, result)
        chunk_dir = ROUND_DIR / "apply_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk = chunk_dir / f"batch_{batch_num:04d}.json"
        chunk.write_text(json.dumps([final], ensure_ascii=False, indent=2), encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "PAPER_INTEGRITY_ROOT": str(BASE),
            "PAPER_INTEGRITY_INPUT": str(BASE / "data" / "input"),
            "PAPER_INTEGRITY_OUTPUT": str(BASE / "data" / "output"),
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
        })
        subprocess.run([
            str(REPORT_PYTHON), str(REPORT_SCRIPT), "--results", str(chunk),
            "--output", str(PDF_DIR), "--namespace", "yujing_4050_202607",
            "--table", "yujing_4050_202607", "--no-db",
        ], cwd=BASE, env=env, check=True)
        pdf = PDF_DIR / f"review_{module.doi_slug(paper['doi'])}.pdf"
        if not pdf.is_file():
            raise RuntimeError(f"未生成报告: {pdf}")
        return pdf

    def update_db(paper: dict, result: str) -> None:
        url = f"http://{PUBLIC_HOST}/review_reports/yujing_4050_202607/review_{module.doi_slug(paper['doi'])}.pdf"
        conn = pymssql.connect(**DB)
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE yujing_4050_202607
                   SET review_result=%s, review_report_url=%s, review_time=%s, status=2
                   WHERE id=%s AND doi=%s""",
                (result, url, datetime.now(), paper["id"], paper["doi"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"数据库写回行数异常: id={paper['id']} rows={cur.rowcount}")
            conn.commit()
        finally:
            conn.close()

    module.render = render
    module.update_db = update_db
    return module


if __name__ == "__main__":
    raise SystemExit(load_module().main())
