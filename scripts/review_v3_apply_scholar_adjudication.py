#!/usr/bin/env python3
"""Apply adjudicated changes to finished v3 scholar reviews and republish them.

    venv/bin/python scripts/review_v3_apply_scholar_adjudication.py \
        --round scholar_1101_liguangfeng_v3 --scholar 李光锋 --edits edits.json [--dry-run]

``edits.json`` maps a DOI to the finals records that change::

    {"10.1021/jacs.5c20730": {
        "figure::m_ja5c20730_0006.png": {"category": "M1", "finding": "...", "why": "..."},
        "some::record": {"status": "cleared", "basis": "核对原文后问题不成立。"},
        "other::record": {"finding": "corrected wording only"}}}

A record keeps every field that the edit does not name.  The paper's verdict is
recomputed from the edited finals, the report is rewritten by the v3 writer
under the normal publication checks, the PDF is rendered and the yujing_scholar
row is updated with read-back (same path as review_v3.batch --apply).  The
round's results file and state are updated so later exports see the final
version.  Earlier finals/final are kept under before_adjudication/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ["REVIEW_ENGINE"] = "v3"

from review_v3 import config, engine  # noqa: E402
from review_v3.categories import severity  # noqa: E402

ROUNDS = BASE / "data" / "review_v3_batches"
TABLE = "yujing_scholar"


def _norm(doi: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(doi or "").strip(), flags=re.I).lower()


def _work_name(doi: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", doi)[:80]


def apply_edits(finals: dict, edits: dict) -> list[str]:
    notes = []
    for record_id, change in edits.items():
        if record_id not in finals:
            if not record_id.startswith("new:") or change.get("status") == "cleared":
                raise KeyError(f"finals has no record {record_id!r}")
            # A clarification the audit added; it needs its own id, category and text.
            for field in ("category", "finding", "why"):
                if not change.get(field):
                    raise KeyError(f"new record {record_id!r} needs {field!r}")
            finals[record_id] = {"id": record_id, "kind": "added", "shard": "data",
                                 "dimension": change.get("dimension", "data"), "status": "issue",
                                 "title": change.get("title", change["finding"][:60]),
                                 "location": change.get("location", ""), "evidence": []}
        record = dict(finals[record_id])
        if change.get("status") == "cleared":
            record.update(status="cleared", basis=change.get("basis") or "核对原文后问题不成立。")
            notes.append(f"{record_id} -> cleared")
        else:
            if "category" in change:
                code = change["category"]
                record.update(category=code, severity=severity(code) or "minor")
                notes.append(f"{record_id} -> {code}")
            else:
                notes.append(f"{record_id} -> wording")
            for field in ("finding", "why", "title"):
                if field in change:
                    record[field] = change[field]
        record["adjudicated"] = True
        finals[record_id] = record
    return notes


async def rewrite(doi: str, row: dict, finals: dict, verdicts: dict) -> dict:
    run = Path(row["_review_workdir"])
    state = json.loads((run / "state.json").read_text())
    review = engine.Review(doi, row["_report_json"], row["_input_dir"], run, None)
    review.evidence = json.loads(Path(state["evidence_path"]).read_text())
    review.dossier = json.loads((run / "dossier" / "index.json").read_text())
    review.shards = json.loads((run / "shards.json").read_text())
    review.read_dirs = review._read_dirs()
    if verdicts["result"] == "高风险":
        finals = await review.capture(finals)
    result, gaps = await review.write(finals, verdicts)
    if result is None:
        raise RuntimeError(f"{doi}: report failed publication checks: {gaps[:4]}")
    # Provenance only: the writer already set the audit fields (_coverage_*) for the new finals.
    result.update({k: row[k] for k in ("_report_json", "_input_dir", "_evidence_path", "_review_metrics", "_verified_paper_title") if k in row})
    result.update(_engine="v3", _review_workdir=str(run), _adjudicated=True)
    keep = run / "before_adjudication"
    keep.mkdir(exist_ok=True)
    for name in ("finals.json", "final.json"):
        if (run / name).is_file() and not (keep / name).exists():
            shutil.copy2(run / name, keep / name)
    finals_doc = json.loads((run / "finals.json").read_text())
    finals_doc.update(finals=finals, verdicts=verdicts, adjudicated=True)
    (run / "finals.json").write_text(json.dumps(finals_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    (run / "final.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def publish(results: list[dict], round_dir: Path, scholar: str, namespace: str = "", no_db: bool = False) -> None:
    """Render PDFs and update yujing_scholar rows with read-back (as review_v3.batch --apply).

    Local-only batches (no database row) pass their own report ``namespace`` and ``no_db``.
    """
    from api.config import REVIEW_DIR, PYTHON, REVIEW_REPORT_SCRIPT
    namespace = re.sub(r"[^A-Za-z0-9_\-]", "_", namespace or TABLE)
    output = REVIEW_DIR / namespace
    results_path = round_dir / "publish" / "adjudicated.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    command = [str(PYTHON), str(REVIEW_REPORT_SCRIPT), "--results", str(results_path), "--output", str(output),
               "--table", TABLE, "--namespace", namespace]
    if scholar:
        command += ["--scholar", scholar]
    if no_db:
        command.append("--no-db")
    proc = subprocess.run(command, cwd=str(config.BASE_DIR), capture_output=True, text=True,
                          env={**os.environ, "PAPER_INTEGRITY_ROOT": str(config.BASE_DIR),
                               "REVIEW_COVERAGE_GAP_POLICY": "strict"})
    if proc.returncode != 0:
        raise RuntimeError(f"报告生成失败 rc={proc.returncode}: {proc.stderr[-800:]}")


def main() -> int:
    global TABLE
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", required=True)
    parser.add_argument("--scholar", default="", help="scope the DB update to this scholar (yujing_scholar rounds)")
    parser.add_argument("--table", default=TABLE, help="yujing_* table of the round (default yujing_scholar)")
    parser.add_argument("--edits", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="show the verdict each paper would get")
    parser.add_argument("--namespace", default="", help="report directory under review_v2/ (default: the table)")
    parser.add_argument("--no-db", action="store_true", help="only re-render PDFs; the batch has no database rows")
    args = parser.parse_args()
    TABLE = args.table
    round_dir = ROUNDS / args.round
    state_path = round_dir / "state.json"
    state = json.loads(state_path.read_text())
    edits = json.loads(args.edits.read_text(encoding="utf-8"))
    done = []
    for doi, changes in edits.items():
        row_path = round_dir / "results" / f"{_work_name(doi)}.json"
        row = json.loads(row_path.read_text())
        run = Path(row["_review_workdir"])
        finals = json.loads((run / "finals.json").read_text())["finals"]
        notes = apply_edits(finals, changes.get("records") or changes)
        verdicts = engine.Review.verdicts(finals)
        print(doi, row["result"], "->", verdicts["result"], "|", "; ".join(notes), flush=True)
        if args.dry_run:
            continue
        result = asyncio.run(rewrite(doi, row, finals, verdicts))
        row_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        record = state["papers"][_norm(doi)]
        record.update(result=result["result"], status="reviewed", adjudicated=True)
        done.append(result)
    if not done:
        return 0
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    publish(done, round_dir, args.scholar, args.namespace, args.no_db)
    for result in done:
        state["papers"][_norm(result["doi"])]["status"] = "rendered" if args.no_db else "published"
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print("published", [r["doi"] for r in done], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
