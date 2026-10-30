#!/usr/bin/env python3
"""Resume the five Phillip 1028 reviews that were not accepted by the renderer.

This intentionally reuses the already completed initial-screening records and
does not rerun screening or touch the five rows that already have a final
review_result in yujing_scholar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from scripts.run_scholar_batch import render_and_update_reviews, run_reviews
from api.services.review_lock import exclusive_review
OUTPUT = BASE / "data/output/yujing_scholar"
INITIAL = OUTPUT / "screening_results_Phillip_1028_initial.json"
RESULTS = OUTPUT / "review_results_Phillip_1028_pending.json"
PENDING = {
    "10.1002/jcsm.12843",
    "10.1002/jcsm.13005",
    "10.1007/s11357-022-00541-3",
    "10.1007/s11357-022-00651-y",
    "10.3390/nu17213410",
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    records = json.loads(INITIAL.read_text(encoding="utf-8"))
    selected = [r for r in records if r.get("doi", "").lower() in {d.lower() for d in PENDING}]
    if len(selected) != len(PENDING):
        found = {r.get("doi", "").lower() for r in selected}
        raise SystemExit(f"Expected {len(PENDING)} pending records, found {len(found)}: {sorted(found)}")
    reviewed = asyncio.run(run_reviews(
        selected,
        OUTPUT,
        workers=len(selected),
        scholar="Phillip",
        summary_tag="1028_pending",
        result_path=RESULTS,
        resume=True,
    ))
    publishable = [r for r in reviewed if r.get("trigger") != "review_error"
                   and not r.get("_coverage_error") and not r.get("_coverage_gaps")]
    return_code = 0 if len(publishable) == len(reviewed) else 2
    if publishable:
        # One renderer failure must not prevent other validated papers from
        # being published. Keep the full result checkpoint for recovery.
        for item in publishable:
            try:
                slug = item['doi'].replace('/', '_')
                code = render_and_update_reviews(
                    [item], OUTPUT, "Phillip", summary_tag="1028_pending",
                    result_path=RESULTS.with_name(RESULTS.stem + '_' + slug + '_publish.json'),
                )
                if code == 2:
                    return_code = 2
            except Exception:
                logging.exception('Publication failed for %s', item['doi'])
                return_code = 2
    return return_code


if __name__ == "__main__":
    with exclusive_review(RESULTS.with_suffix('.lock')):
        raise SystemExit(main())
