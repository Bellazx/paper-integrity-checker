#!/usr/bin/env python3
"""Correct mis-extracted paper titles for finished scholar reviews and republish.

    venv/bin/python scripts/review_v3_fix_scholar_titles.py --round ROUND --scholar NAME \
        --titles titles.json [DOI ...]

``titles.json`` maps DOI -> correct title (taken from the paper's first page).
The yujing_scholar row's title is updated, the round's review result gets
``_verified_paper_title`` (the renderer prints it instead of the initial
report's extracted title) and the review PDF is re-rendered with the normal
publication checks and DB read-back.  Review conclusions are not changed.
Only the DOIs given on the command line are republished (default: all).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from scripts.review_v3_apply_scholar_adjudication import ROUNDS, TABLE, _norm, _work_name, publish  # noqa: E402


def update_title(doi: str, scholar: str, title: str) -> None:
    import pymssql
    from api.config import DB_CONFIG
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {TABLE} SET title=%s WHERE doi=%s AND scholar=%s", (title[:500], doi, scholar))
        cursor.execute(f"SELECT title FROM {TABLE} WHERE doi=%s AND scholar=%s", (doi, scholar))
        rows = cursor.fetchall()
        if not rows or any(r[0] != title[:500] for r in rows):
            raise RuntimeError(f"title read-back failed for {doi}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", required=True)
    parser.add_argument("--scholar", required=True)
    parser.add_argument("--titles", required=True, type=Path)
    parser.add_argument("dois", nargs="*")
    args = parser.parse_args()
    titles = json.loads(args.titles.read_text(encoding="utf-8"))
    titles = titles.get(args.scholar, titles)
    round_dir = ROUNDS / args.round
    wanted = {_norm(d) for d in args.dois} if args.dois else {_norm(d) for d in titles}
    done = []
    for doi, title in titles.items():
        if _norm(doi) not in wanted:
            continue
        row_path = round_dir / "results" / f"{_work_name(doi)}.json"
        row = json.loads(row_path.read_text())
        row["_verified_paper_title"] = title
        row_path.write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
        update_title(row["doi"], args.scholar, title)
        done.append(row)
        print(doi, "->", title, flush=True)
    if done:
        publish(done, round_dir, args.scholar)
        print("republished", [r["doi"] for r in done], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
