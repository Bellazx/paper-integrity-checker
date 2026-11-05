#!/usr/bin/env python3
"""Rewrite finished v3 reports of the 0901 testset in the current report format.

    venv/bin/python scripts/review_v3_reformat_reports.py [--high-only] [--workers 12] [DOI ...]

Each paper keeps its recorded conclusions (finals.json, including applied
adjudication). Risk issues without a screenshot get one (capture sessions, then
the code fallback); the writer then writes the report under the publication
checks. The previous final.json is kept as final.before_reformat.json. Rendered
PDFs and the comparison table row are updated at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ["REVIEW_ENGINE"] = "v3"

from review_v3 import engine  # noqa: E402

TABLE = "yujing_testset_0901_0907_nature_nc_v3cmp"
OUT = BASE / "data" / "output" / TABLE


def _complete(result: dict) -> bool:
    """Every numbered risk issue has a screenshot or a workbook range."""
    import re
    have = {(str(e.get("dimension") or "data"), int(e.get("issue_index") or 0))
            for e in (result.get("evidence_images") or []) + (result.get("excel_evidence_regions") or [])}
    for dim, field in engine.DIMENSION_FIELD.items():
        for number in re.findall(r"[（(](\d+)[）)]\s*问题位置", str(result.get(field) or "")):
            if (dim, int(number)) not in have:
                return False
    return True


async def rewrite(row: dict, gate: asyncio.Semaphore, reuse: bool = False) -> dict | None:
    doi, run = row["doi"], Path(row["_review_workdir"])
    if reuse and (run / "final.before_reformat.json").is_file():
        # Rewritten by an earlier, interrupted pass: keep it when complete.
        done = json.loads((run / "final.json").read_text())
        if _complete(done):
            print(json.dumps({"doi": doi, "result": done["result"], "reused": True,
                              "screenshots": len(done.get("evidence_images") or [])}, ensure_ascii=False), flush=True)
            return done
    async with gate:
        try:
            doc = json.loads((run / "finals.json").read_text())
            state = json.loads((run / "state.json").read_text())
            review = engine.Review(doi, row["_report_json"], row["_input_dir"], run, None)
            review.evidence = json.loads(Path(state["evidence_path"]).read_text())
            review.dossier = json.loads((run / "dossier" / "index.json").read_text())
            review.shards = json.loads((run / "shards.json").read_text())
            review.read_dirs = review._read_dirs()
            finals, verdicts = doc["finals"], doc["verdicts"]
            if verdicts["result"] == "高风险":
                finals = await review.capture(finals)
            result, gaps = await review.write(finals, verdicts)
            if result is None:
                print(json.dumps({"doi": doi, "error": gaps[:3]}, ensure_ascii=False), flush=True)
                return None
            keep = {k: row[k] for k in ("_report_json", "_input_dir", "_review_workdir", "_evidence_path",
                                        "_adjudicated", "_review_metrics") if row.get(k) is not None}
            result.update(keep)
            if not (run / "final.before_reformat.json").exists() and (run / "final.json").is_file():
                shutil.copy2(run / "final.json", run / "final.before_reformat.json")
            doc["finals"] = finals
            (run / "finals.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            (run / "final.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            shots = len(result.get("evidence_images") or [])
            print(json.dumps({"doi": doi, "result": result["result"], "source": result.get("_report_source", "writer"),
                              "screenshots": shots}, ensure_ascii=False), flush=True)
            return result
        except Exception as exc:  # recorded; the batch goes on
            print(json.dumps({"doi": doi, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
            return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dois", nargs="*")
    parser.add_argument("--high-only", action="store_true")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--reuse", action="store_true", help="keep reports an interrupted pass already rewrote")
    args = parser.parse_args()
    path = OUT / "review_results_v3.json"
    rows = {r["doi"]: r for r in json.loads(path.read_text())}
    todo = [r for r in rows.values() if r.get("_review_workdir") and (not args.dois or r["doi"] in args.dois)
            and (not args.high_only or r.get("result") == "高风险")]

    async def run_all():
        gate = asyncio.Semaphore(max(1, args.workers))
        return await asyncio.gather(*(rewrite(r, gate, args.reuse) for r in todo))

    done = [r for r in asyncio.run(run_all()) if r]
    for result in done:
        rows[result["doi"]] = result
    merged = sorted(rows.values(), key=lambda r: str(r.get("doi") or ""))
    for name in ("review_results_v3.json", "review_progress_v3.json"):
        (OUT / name).write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"rewritten {len(done)}/{len(todo)}", flush=True)
    if not done:
        return 1
    from scripts import run_scholar_batch as batch
    batch.TABLE = TABLE
    rc = batch.render_and_update_reviews(done, OUT, "", result_path=OUT / "review_results_v3_reformat.json")
    print("renderer rc", rc, flush=True)
    return 0 if rc == 0 and len(done) == len(todo) else 2


if __name__ == "__main__":
    raise SystemExit(main())
