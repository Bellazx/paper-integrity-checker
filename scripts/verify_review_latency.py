"""Fresh end-to-end replay of three documented 5400-second timeouts; no DB writes."""
import asyncio
import argparse
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--resume-job', type=Path)
parser.add_argument('--papers', type=int, nargs='+', default=[2, 4, 10])
ARGS = parser.parse_args()
if ARGS.resume_job:
    JOB = ARGS.resume_job.resolve()
    assert JOB.is_dir() and JOB.parent == BASE / 'data/output'
    NAMESPACE = JOB.name
else:
    NAMESPACE = 'review_latency_verification_' + datetime.now().strftime('%Y%m%d_%H%M%S')
    JOB = BASE / 'data/output' / NAMESPACE
    JOB.mkdir(parents=True, exist_ok=False)
PREFIX = 'recovery_' if ARGS.resume_job else ''
os.environ['REVIEW_STATE_DIR'] = str(JOB / 'state')
os.environ['REVIEW_RESUME_RESULTS'] = '0'
os.environ['REVIEW_PAPER_TIMEOUT_SECONDS'] = '5400'
os.environ['REVIEW_COVERAGE_GAP_POLICY'] = 'strict'
os.environ['REVIEW_COVERAGE_FAILURE_POLICY'] = 'strict'
os.environ.pop('REVIEW_SKIP_REPORT_COVERAGE', None)

from api.services import review
from api.services.review_validation import validate

# Isolate evidence caches as well as model sessions and final results.
review.REVIEW_DIR = JOB / 'review_v2'
INITIAL = BASE / 'data/output/yujing_scholar/huangbiwei_1029_initial'
STATUS = {'started_at': datetime.now().isoformat(), 'pid': os.getpid(),
          'model': review.REVIEW_MODEL, 'baseline_timeout_seconds': 5400,
          'fresh_review': not bool(ARGS.resume_job), 'database_writes': False, 'papers': {}}


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def flush():
    save(JOB / f'{PREFIX}status.json', STATUS)


async def one(meta, record):
    number = meta['number']
    entry = {'title': meta['title'], 'doi': record['doi'], 'state': 'reviewing',
             'started_at': datetime.now().isoformat()}
    STATUS['papers'][str(number)] = entry
    flush()
    started = time.monotonic()
    try:
        input_dir = JOB / 'inputs' / str(number)
        if not ARGS.resume_job:
            await asyncio.to_thread(shutil.copytree, record['input_dir'], input_dir)
        assert input_dir.is_dir()
        result = await review.run_review_single(record['doi'], record['report_json'],
                                               str(input_dir), str(JOB / 'review_v2'), resume=bool(ARGS.resume_job))
        entry['review_seconds'] = round(time.monotonic() - started, 2)
        save(JOB / f'{PREFIX}paper_{number}_result.json', result)
        evidence_path = result.get('_evidence_path')
        evidence = json.loads(Path(evidence_path).read_text()) if evidence_path else {}
        validated, gaps = validate(result, evidence)
        save(JOB / f'{PREFIX}paper_{number}_validation.json', {'gaps': gaps, 'result': validated})
        entry.update(result=result.get('result'), validation_gaps=gaps,
                     evidence_fallback=evidence.get('_fallback_reason'))
        if gaps or review._is_review_error(result):
            entry.update(state='failed', error=result.get('_review_error') or gaps)
            return
        render_input = JOB / f'{PREFIX}paper_{number}_render.json'
        save(render_input, [validated])
        entry['state'] = 'rendering'
        flush()
        with (JOB / f'{PREFIX}paper_{number}_render.log').open('wb') as log:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(BASE / 'scripts/generate_review_report.py'),
                '--results', str(render_input), '--output', str(JOB / 'review_v2'),
                '--namespace', NAMESPACE, '--no-db', cwd=BASE,
                stdout=log, stderr=asyncio.subprocess.STDOUT,
                env=review.numeric_subprocess_env())
            try:
                rc = await asyncio.wait_for(proc.wait(), 600)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError('PDF generation exceeded 600 seconds')
        entry.update(state='complete' if rc == 0 else 'render_failed', render_returncode=rc)
    except Exception as exc:
        logging.exception('Replay failed for paper %s', number)
        entry.update(state='failed', error=str(exc))
    finally:
        entry.update(total_seconds=round(time.monotonic() - started, 2),
                     finished_at=datetime.now().isoformat())
        flush()


async def main():
    manifest = json.loads((INITIAL / 'manifest.json').read_text())
    records = {r['doi']: r for r in json.loads((INITIAL / 'results.json').read_text())}
    assert set(ARGS.papers) <= {2, 4, 10}
    selected = [m for m in manifest if m['number'] in ARGS.papers]
    assert len(selected) == len(ARGS.papers)
    save(JOB / f'{PREFIX}manifest.json', selected)
    flush()
    await asyncio.gather(*(one(m, records[m['identifier']]) for m in selected))
    STATUS['finished_at'] = datetime.now().isoformat()
    STATUS['state'] = ('complete' if all(p['state'] == 'complete'
                                       for p in STATUS['papers'].values()) else 'needs_attention')
    flush()
    return 0 if STATUS['state'] == 'complete' else 1


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(JOB / f'{PREFIX}run.log'), logging.StreamHandler()])
    print(f'VERIFICATION_DIR={JOB}', flush=True)
    raise SystemExit(asyncio.run(main()))
