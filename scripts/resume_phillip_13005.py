"""Resume the retained second-review session, preserving its phase budget."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from api.services.review_lock import exclusive_review
from scripts.review_phillip_pending import INITIAL, OUTPUT
from scripts.run_scholar_batch import run_reviews, render_and_update_reviews

RESULT = OUTPUT / 'review_results_Phillip_13005_resume_1029.json'
STATUS = OUTPUT / 'review_Phillip_13005_resume_1029_status.json'


def status(**values):
    temp = STATUS.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(updated_at=time.time(), **values), ensure_ascii=False, indent=2))
    temp.replace(STATUS)


def main():
    os.environ['REVIEW_PAPER_TIMEOUT_SECONDS'] = '5400'
    item = next(r for r in json.loads(INITIAL.read_text()) if r['doi'] == '10.1002/jcsm.13005')
    status(state='running', phase_timeout_seconds=None, paper_timeout_seconds=5400)
    # Do not change cli_run_id or checkpoints: the first pass is cached and
    # the interrupted verifier continues the same session in focused recovery.
    results = asyncio.run(run_reviews([item], OUTPUT, 1, scholar='Phillip',
                                     result_path=RESULT, resume=True))
    result = results[0]
    if (result.get('trigger') == 'review_error' or result.get('_coverage_gaps')
            or result.get('_coverage_error')):
        status(state='incomplete', error=result.get('_review_error') or
               result.get('_coverage_gaps') or result.get('_coverage_error'))
        return 2
    status(state='publishing', result=result.get('result'))
    code = render_and_update_reviews(results, OUTPUT, 'Phillip',
        result_path=RESULT.with_name(RESULT.stem + '_publish.json'))
    status(state='published' if code == 0 else 'publication_failed', result=result.get('result'))
    return code


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with exclusive_review(RESULT.with_suffix('.lock')):
        try:
            code = main()
        except Exception as exc:
            status(state='error', error=str(exc))
            raise
        raise SystemExit(code)
