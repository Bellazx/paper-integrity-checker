#!/usr/bin/env python3
"""Retry settled Phillip timeout failures once with the whole-paper deadline.

The original batch may still be running. Only its persisted failures are
eligible; retry results and publication files never overwrite its outputs.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from api.services import review
from api.services.review_lock import exclusive_review
from scripts.review_phillip_pending import INITIAL, OUTPUT, PENDING, RESULTS
from scripts.run_scholar_batch import run_reviews, render_and_update_reviews

STATE = OUTPUT / 'phillip_timeout_retry_status.json'
log = logging.getLogger(__name__)


def load(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def timeout_failure(item):
    return (item.get('trigger') == 'review_error'
            and any(word in str(item.get('_review_error') or item.get('reason') or '').lower()
                    for word in ('timeout', 'deadline', '超时')))


def rearm(item):
    """Keep substantive stages but give the authorized retry new CLI budgets."""
    args = (item['doi'], item['report_json'], item['input_dir'])
    path = review._review_state_path(
        *args, review._review_artifacts_dir(str(OUTPUT / 'review_v2')))
    with exclusive_review(path.with_suffix('.lock')):
        state = review._load_review_state(path, *args)
        if not timeout_failure(state.get('stages', {}).get('final', {})):
            raise RuntimeError('Paper no longer has a settled timeout failure')
        run_id = uuid.uuid4().hex
        save(path.with_name(path.stem + '_' + run_id + '.history.json'), state)
        state['cli_run_id'] = run_id
        review._save_review_state(path, state)
    return run_id


async def retry_one(item, statuses):
    doi = item['doi']
    slug = doi.replace('/', '_')
    target = OUTPUT / f'review_results_Phillip_1028_timeout_retry_{slug}.json'
    try:
        run_id = rearm(item)
        statuses[doi].update(state='running', run_id=run_id, started_at=time.time(),
                             phase_timeout_seconds=None, paper_timeout_seconds=5400)
        save(STATE, statuses)
        log.info('Restart %s: no phase timeout, paper deadline=5400s; saved drafts retained', doi)
        results = await run_reviews([item], OUTPUT, 1, scholar='Phillip',
                                    result_path=target, resume=True)
        result = results[0]
        if (result.get('trigger') == 'review_error' or result.get('_coverage_gaps')
                or result.get('_coverage_error')):
            statuses[doi].update(state='incomplete', error=result.get('_review_error')
                                 or result.get('_coverage_gaps') or result.get('_coverage_error'))
        else:
            statuses[doi].update(state='publishing', result=result.get('result'))
            save(STATE, statuses)
            code = await asyncio.to_thread(render_and_update_reviews,
                results, OUTPUT, 'Phillip', result_path=target.with_name(target.stem + '_publish.json'))
            statuses[doi]['state'] = 'published' if code == 0 else 'publication_failed'
    except Exception as exc:
        log.exception('Retry failed for %s', doi)
        statuses[doi].update(state='error', error=str(exc))
    finally:
        statuses[doi]['finished_at'] = time.time()
        save(STATE, statuses)


async def main():
    os.environ['REVIEW_PAPER_TIMEOUT_SECONDS'] = '5400'
    os.environ.pop('REVIEW_SUBPROCESS_TIMEOUT_SECONDS', None)
    records = {r['doi']: r for r in load(INITIAL, []) if r.get('doi') in PENDING}
    statuses = load(STATE, {})
    tasks = []
    watch_deadline = time.monotonic() + 4200
    while True:
        settled = {r['doi']: r for r in load(RESULTS, []) if r.get('doi') in PENDING}
        for doi, result in settled.items():
            if doi not in statuses and timeout_failure(result):
                statuses[doi] = {'state': 'queued'}
                save(STATE, statuses)
                tasks.append(asyncio.create_task(retry_one(records[doi], statuses)))
        if set(settled) == PENDING:
            break
        if time.monotonic() >= watch_deadline:
            log.error('Original batch has not settled; stopping watcher without duplicating active papers')
            break
        await asyncio.sleep(15)
    await asyncio.gather(*tasks)
    return 0 if all(r['state'] == 'published' for r in statuses.values()) else 2


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with exclusive_review(STATE.with_suffix('.lock')):
        raise SystemExit(asyncio.run(main()))
