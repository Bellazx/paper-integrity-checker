#!/usr/bin/env python3
"""Apply adjudication verdicts to finished v3 reviews of the 0901 testset and
publish the rewritten reports.

    venv/bin/python scripts/review_v3_apply_adjudication.py [--dry-run] DOI [DOI ...]

For each paper the adjudicated outcome of every substantive item replaces the
recorded one: items found not to hold are cleared, items that hold only as a
minor issue take the adjudicated category with the verified original values as
their finding. Items named in UPGRADES become S1 (source-data block pasted into
another group's place, user decision 2026-11-02); other records of the same
fact are cleared as duplicates. The report is then written by the v3 writer
under the normal publication checks, rendered, and the comparison table row is
updated. Earlier finals/final are kept under before_adjudication/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ["REVIEW_ENGINE"] = "v3"

from review_v3 import engine  # noqa: E402
from review_v3.categories import severity  # noqa: E402

TABLE = "yujing_testset_0901_0907_nature_nc_v3cmp"
OUT = BASE / "data" / "output" / TABLE
ADJ = OUT / "adjudication"

# doi -> (record kept as S1, records of the same fact, finding, why)
UPGRADES = {
    "10.1038/s41467-026-77169-w": (
        "source_data_audit::rectangular_matrix_reuse::0",
        ["new:fig2d-source-sheet-duplicates-fig2e", "new:fig2d-fig2e-source-sheets-identical",
         "reuse_group::reuse_group_2::count=1", "new:fig2d-fig2e-source-sheet-duplicate"],
        "随文源数据 41467_2026_77169_MOESM3_ESM.xlsx 中工作表 Fig2d 与 Fig2e 的 2031 行×4 列逐格相同（首行均为 0.1222、"
        "5.6909e-06、0.1227、8.2211e-09，末行均为 20300.0066、1.4022e-05、20300.0067、8.6314e-09），内容是 Fig. 2e 的"
        "保持特性时间—电流序列；而正文 Fig. 2d 是阵列 LRS/HRS 电阻态的累积概率分布，两图是不同测量。",
        "Fig. 2d 的数据载体被另一张图的数据替代，电阻态分布这一结果在随文数据中无法核对，需作者提供 Fig. 2d 的真实数据。"),
    "10.1038/s41467-026-77410-6": (
        "group::source_data_audit::rectangular_matrix_reuse::source_data::41467_2026_77410_MOESM7_ESM.xlsx",
        ["reuse_group::reuse_group_3::count=5", "reuse_group::reuse_group_4::count=5", "reuse_group::reuse_group_5::count=5",
         "reuse_group::reuse_group_6::count=5", "reuse_group::reuse_group_7::count=5",
         "new:supp-fig6a-tapestry-probability-source-missing"],
        "随文源数据 41467_2026_77410_MOESM7_ESM.xlsx 中工作表『Supplementary Fig. 6a』（Tapestry 验证队列）与"
        "『Supplementary Fig. 5a』（MCB 1B 验证队列）的 1245 个数据行、34 列逐格相同；该表只有 1245 行，容纳不下 Tapestry "
        "对照组的 1421 人，而印出的 Supplementary Fig. 5A 与 6A 明显不同。",
        "Tapestry 队列的验证结果在随文数据中被另一队列的数据替代，无法核对，需作者提供 Supplementary Fig. 6A 的真实数据。"),
}

NUMBERS = re.compile(r"问题\s*([0-9][0-9/、,，\-–和与及\s]*)")
CODE = re.compile(r"\b([SM](?:10|[1-9]))\b")


def _numbers(text: str) -> set[int]:
    out: set[int] = set()
    for match in NUMBERS.finditer(text):
        part = match.group(1)
        span = re.match(r"\s*(\d+)\s*[-–]\s*(\d+)", part)
        if span:
            out.update(range(int(span.group(1)), int(span.group(2)) + 1))
        out.update(int(n) for n in re.findall(r"\d+", part))
    return out


def _clean(text: str) -> str:
    """Adjudication prose minus references to the review itself."""
    kept = [s for s in re.split(r"(?<=[。；])", str(text or "")) if s.strip()
            and not re.search(r"v3|V3|问题\s*\d|判决|裁决|定级|降为|升为", s)]
    return engine._plain("".join(kept)).strip()


def apply(doi: str, row: dict) -> tuple[dict, dict, list[str]]:
    run = Path(row["_review_workdir"])
    finals = json.loads((run / "finals.json").read_text())["finals"]
    verdict = json.loads((ADJ / doi.replace("/", "_") / "verdict.json").read_text())
    subs = [r for r in finals.values() if r.get("status") == "issue" and r.get("severity") == "substantive"]
    notes = []
    for number, record in enumerate(subs, start=1):
        mapped = [i for i in verdict.get("issues") or [] if number in _numbers(str(i.get("v3_issue") or ""))]
        coded = [(i, CODE.search(str(i.get("category") or ""))) for i in mapped]
        coded = [(i, m.group(1)) for i, m in coded if m]
        holding = [(i, c) for i, c in coded if i.get("verified")] or coded
        if holding:
            item, code = holding[0]
            finding = _clean(item.get("evidence")) or record.get("finding")
            why = _clean(item.get("reason")) or record.get("why")
            finals[record["id"]] = {**record, "category": code, "severity": severity(code) or "minor",
                                    "finding": finding, "why": why, "adjudicated": True}
            notes.append(f"{record['id']} -> {code}")
        elif mapped:
            basis = _clean(" ".join(str(i.get("evidence") or "") + str(i.get("reason") or "") for i in mapped))
            finals[record["id"]] = {**record, "status": "cleared", "basis": basis or "核对原文后问题不成立。",
                                    "adjudicated": True}
            notes.append(f"{record['id']} -> cleared")
        else:
            notes.append(f"{record['id']} -> unmapped (kept)")
    if doi in UPGRADES:
        keep, same, finding, why = UPGRADES[doi]
        finals[keep] = {**finals[keep], "status": "issue", "category": "S1", "severity": "substantive",
                        "finding": finding, "why": why, "evidence": [], "adjudicated": True}
        for other in same:
            if other in finals:
                finals[other] = {**finals[other], "status": "cleared", "basis": f"与 {keep} 为同一事实。",
                                 "adjudicated": True}
        notes.append(f"{keep} -> S1 (+{len(same)} same fact)")
    return finals, engine.Review.verdicts(finals), notes


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
    result.update(_engine="v3", _report_json=row["_report_json"], _input_dir=row["_input_dir"],
                  _review_workdir=str(run), _adjudicated=True)
    if row.get("_evidence_path"):
        result["_evidence_path"] = row["_evidence_path"]
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dois", nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="show the mapping only")
    args = parser.parse_args()
    results_path = OUT / "review_results_v3.json"
    rows = {r["doi"]: r for r in json.loads(results_path.read_text())}
    done = []
    for doi in args.dois:
        finals, verdicts, notes = apply(doi, rows[doi])
        print(doi, verdicts["result"], "; ".join(notes), flush=True)
        if args.dry_run:
            continue
        done.append(asyncio.run(rewrite(doi, rows[doi], finals, verdicts)))
    if not done:
        return 0
    for result in done:
        rows[result["doi"]] = result
    merged = sorted(rows.values(), key=lambda r: str(r.get("doi") or ""))
    for name in ("review_results_v3.json", "review_progress_v3.json"):
        (OUT / name).write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    from scripts import run_scholar_batch as batch
    batch.TABLE = TABLE
    rc = batch.render_and_update_reviews(done, OUT, "", result_path=OUT / "review_results_v3_adjudicated.json")
    print("renderer rc", rc, flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
