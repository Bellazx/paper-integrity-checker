"""Score a review_v3 A/B run against the delivered conclusions.

    venv/bin/python scripts/review_v3_eval.py data/review_v3_ab/<run>.log data/review_v3_scholar_1021.json
"""
from __future__ import annotations

import collections
import json
import statistics
import sys
from pathlib import Path


def rows_from_log(path: Path) -> list[dict]:
    rows = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"expected"' in line and line.lstrip().startswith("{"):
            row = json.loads(line[line.index("{"):])
            rows[row["doi"]] = row  # the latest attempt wins
    return list(rows.values())


def main() -> None:
    rows = rows_from_log(Path(sys.argv[1]))
    meta = {c["doi"]: c for c in json.loads(Path(sys.argv[2]).read_text())} if len(sys.argv) > 2 else {}
    done = [r for r in rows if not r["error"]]
    errors = [r for r in rows if r["error"]]
    agree = [r for r in done if r["got"] == r["expected"]]
    # A process error is a failed review for the acceptance rate as well.
    print(f"papers {len(rows)}; completed {len(done)}; process errors {len(errors)}")
    if rows:
        print(f"agreement with delivered result: {len(agree)}/{len(rows)} = {len(agree) / len(rows):.1%} "
              f"(completed only: {len(agree)}/{max(1, len(done))} = {len(agree) / max(1, len(done)):.1%})")
    walls = [r["wall_seconds"] / 60 for r in rows]
    if walls:
        walls.sort()
        print(f"wall minutes: mean {statistics.mean(walls):.1f}, median {statistics.median(walls):.1f}, "
              f"p90 {walls[int(0.9 * (len(walls) - 1))]:.1f}, max {walls[-1]:.1f}")
    confusion = collections.Counter((r["expected"], r["got"] if not r["error"] else "error") for r in rows)
    print("confusion (expected -> got):", dict(confusion))
    strata = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        key = (meta.get(r["doi"], {}).get("initial"), r["expected"])
        strata[key][1] += 1
        strata[key][0] += int(r in agree)
    print("by (initial, delivered):", {f"{k[0]}->{k[1]}": f"{a}/{n}" for k, (a, n) in strata.items()})
    print("\nmismatches:")
    for r in rows:
        if r in agree:
            continue
        print(f"- {r['doi']} expected {r['expected']} got {r['got']} {('error: ' + r['error'][:120]) if r['error'] else ''}"
              f"\n  dims {r.get('dimension_verdicts')}\n  {r.get('workdir')}")


if __name__ == "__main__":
    main()
