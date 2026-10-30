#!/usr/bin/env python3
"""Incrementally review all 39 papers from the frozen 1027 initial manifest."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
INITIAL = BASE / 'data/output/yujing_scholar/scholar_1027_rescreen_20261027'
ROUND = BASE / 'data/yujing_scholar_1027_review_20261027'
PYTHON = str(BASE / 'venv/bin/python')
# A worker must never remain live forever after a model/CLI failure.  The
# Absolute lifetime cap, independent of stage heartbeats and CPU usage.
WORKER_MAX_SECONDS = max(60, int(os.environ.get('REVIEW_WORKER_MAX_SECONDS', '7200')))
os.environ['PAPER_CLEAN_JUNK'] = '0'
os.environ['REVIEW_COVERAGE_GAP_POLICY'] = 'strict'
os.environ['REVIEW_WRITE_ONLY_LOW'] = '0'
for thread_variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                        'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    os.environ[thread_variable] = '1'
os.environ['REVIEW_COVERAGE_MEMORY_MB'] = '8192'
os.environ['REVIEW_COVERAGE_FAILURE_POLICY'] = 'strict'


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scope():
    frozen = load(ROUND / 'manifest.json')
    if frozen:
        return frozen['papers']
    papers = []
    for batch in load(INITIAL / 'manifest.json')['batches']:
        manifest = load(Path(batch['run_dir']) / 'manifest.json')
        for paper in manifest['papers']:
            source = Path(paper.get('original_source') or paper['source']).resolve()
            source.relative_to(BASE / 'data/input/scholar/1027')
            papers.append(dict(paper, source=str(source), initial_working_source=paper['source'],
                               scholar=manifest['scholar'],
                               started_at=manifest['started_at'],
                               key=f"{manifest['scholar']}/{paper['name']}"))
    if len(papers) != 39 or len({p['key'] for p in papers}) != 39:
        raise RuntimeError('Expected exactly 39 unique scholar/paper entries')
    save(ROUND / 'manifest.json', {'created_at': datetime.now().isoformat(),
         'table': 'yujing_scholar', 'selection': 'all_initial_risk_levels', 'papers': papers})
    return papers


def rows():
    import pymssql
    from utils.db import DB_CONFIG
    # This helper owns and closes its connection; never close the shared,
    # thread-local connection returned by utils.db.get_connection().
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute('SELECT id,doi,scholar,generation_time,report_url,risk_level,'
                    'review_result,review_report_url,review_time,status '
                    'FROM yujing_scholar WHERE scholar IN (%s,%s)', ('韩有承', '戴大伟'))
        found = {}
        for row in cur.fetchall():
            key = (row['scholar'], row['doi'].lower())
            if key in found:
                raise RuntimeError(f'Duplicate DB scholar/DOI: {key}')
            found[key] = row
        return found
    finally:
        conn.close()


def readiness(paper, row):
    """Require a successful *current* initial run, regardless of risk or status."""
    from urllib.parse import unquote, urlsplit
    from modules.chinese_report_generator import _make_filename
    import fitz
    if not row or not row['generation_time']:
        raise ValueError('awaiting initial DB result')
    if datetime.fromisoformat(str(row['generation_time'])) < datetime.fromisoformat(paper['started_at']):
        raise ValueError('initial DB result predates this run')
    report = BASE / 'data/output/yujing_scholar/init' / paper['name'] / 'report.json'
    if report.stat().st_mtime < datetime.fromisoformat(paper['started_at']).timestamp():
        raise ValueError('initial artifact predates this run')
    findings = load(report)
    if not findings or not findings.get('pdf_generated'):
        raise ValueError('initial PDF not complete')
    if findings['paper']['doi'].lower() != paper['doi'].lower():
        raise ValueError('initial DOI mismatch')
    filename = _make_filename(paper['doi'], findings['paper'].get('title', 'unknown'))
    if unquote(urlsplit(row['report_url']).path.rsplit('/', 1)[-1]) != filename:
        raise ValueError('initial PDF URL mismatch')
    with fitz.open(BASE / 'data/output/yujing_scholar/chinese_reports' / filename) as doc:
        if not doc.page_count or not doc[0].get_text().strip():
            raise ValueError('initial PDF unreadable')
    return report


async def review(paper, job):
    from api.services import review as engine
    snapshot = job / 'initial_report.json'
    args = (paper['doi'], str(snapshot), paper['source'], str(job / 'artifacts'))
    evidence_path = await asyncio.to_thread(engine._ensure_evidence_bundle, *args)
    evidence = load(evidence_path)
    if not evidence or evidence.get('_fallback_reason'):
        raise RuntimeError('Complete evidence preparation failed; do not review a fallback inventory')
    summary = engine._ensure_llm_evidence_bundle(paper['doi'], evidence_path)
    ev = engine.EvidencePaths(llm_summary=summary, full=evidence_path)
    first = load(job / 'reviewer1.json')
    if not first or engine._is_review_error(first):
        first = await engine._run_with_retry(*args, agent_id=1, ev=ev)
        save(job / 'reviewer1.json', first)
    if engine._is_review_error(first):
        raise RuntimeError('First review failed after bounded recovery; no substantive conclusion')
    second = {}
    if first.get('result') == '低风险':
        second = load(job / 'reviewer2.json')
        if not second or engine._is_review_error(second):
            second = await engine._run_lowrisk_verifier_with_retry(*args, first, ev=ev)
            save(job / 'reviewer2.json', second)
        if engine._is_review_error(second):
            raise RuntimeError('Second review failed after bounded recovery; no substantive conclusion')
    final = load(job / 'judge_validated.json')
    if final:
        final = engine._validate_review_result(*args[:3], final, evidence)
    saved_candidate = load(job / 'judge_candidate.json')
    candidate = saved_candidate or first
    if final and not engine._is_review_error(final):
        candidate = final
    candidate = engine._validate_review_result(*args[:3], candidate, evidence)
    synthesis_seen = bool(saved_candidate and not engine._is_review_error(saved_candidate))
    synthesis_seen = synthesis_seen or bool(final and not engine._is_review_error(final))
    for attempt in range(3):
        if (final and not engine._is_review_error(final)
                and not engine._has_unresolved_coverage(final)):
            break
        # Repair the latest substantive synthesis, not the same initial pair
        # repeatedly. Never feed a transport-error placeholder as evidence.
        gaps = candidate.get('_coverage_gaps') or []
        judged = await engine._run_judge(*args, candidate, second or first, ev=ev, coverage_gaps=gaps)
        save(job / f'judge_{attempt}.json', judged)
        final = engine._validate_review_result(*args[:3], judged, evidence)
        save(job / 'judge_validated.json', final)
        if not engine._is_review_error(final):
            candidate = final
            synthesis_seen = True
            save(job / 'judge_candidate.json', candidate)
        elif judged.get('_retry_exhausted'):
            break
    if (not final or engine._is_review_error(final) or engine._has_unresolved_coverage(final)):
        # The synthesis pass is only an adjudication layer.  When both
        # substantive passes agree and independently pass coverage, their
        # validated consensus is a complete final conclusion.  Previously a
        # max-turn error in synthesis discarded this valid consensus and left
        # the worker running indefinitely.
        substantive = [engine._validate_review_result(*args[:3], r, evidence)
                       for r in (first, second) if r and not engine._is_review_error(r)]
        if (not synthesis_seen and engine._is_review_error(final) and len(substantive) == 2
                and all(not engine._has_unresolved_coverage(r) for r in substantive)):
            labels = {r.get('result') for r in substantive}
            if len(labels) == 1:
                final = engine._consensus_fallback(paper['doi'], *substantive)
                final = engine._validate_review_result(*args[:3], final, evidence)
        if (not final or engine._is_review_error(final)
                or engine._has_unresolved_coverage(final)):
            raise RuntimeError('Final review incomplete after bounded recovery; retained for retry')
    final = engine._clean_internal_references(dict(final))
    final.update(_input_dir=paper['source'], _report_json=str(snapshot), _evidence_path=evidence_path)
    save(job / 'results.json', [final])
    return final


def work(key):
    paper = next(p for p in scope() if p['key'] == key)
    job = ROUND / 'papers' / key
    item = load(job / 'item.json')
    try:
        from api.services import review as engine
        from scripts.scholar_1027_cli import install
        install(engine, job, save, load)
        final = asyncio.run(review(paper, job))
        current = rows()[(paper['scholar'], paper['doi'].lower())]
        report = readiness(paper, current)
        if digest(report) != item['report_sha256'] or str(current['generation_time']) != item['generation_time']:
            raise RuntimeError('Initial result changed during review; publication blocked')
        command = [PYTHON, str(BASE / 'scripts/generate_review_report.py'),
                   '--results', str(job / 'results.json'), '--output', str(job / 'pdf'),
                   '--table', 'yujing_scholar', '--namespace', 'yujing_scholar',
                   '--scholar', paper['scholar']]
        subprocess.run(command + ['--no-db'], cwd=BASE, check=True, timeout=1800)
        import fitz
        pdfs = list((job / 'pdf').glob('review_*.pdf'))
        if len(pdfs) != 1:
            raise RuntimeError('Expected one final PDF')
        with fitz.open(pdfs[0]) as doc:
            text = '\n'.join(p.get_text() for p in doc)
            if not doc.page_count or '\ufffd' in text:
                raise RuntimeError('Unreadable PDF text')
            if any(term in text for term in ('原报告', '初审报告', '旧报告')):
                raise RuntimeError('Public wording check failed')
            if final['result'] == '高风险' and not any(p.get_images() for p in doc):
                raise RuntimeError('Risk PDF has no evidence screenshot')
        subprocess.run(command, cwd=BASE, check=True, timeout=1800)
        row = rows()[(paper['scholar'], paper['doi'].lower())]
        if row['status'] != 2 or row['review_result'] != final['result'] or not row['review_report_url']:
            raise RuntimeError('Database review receipt did not match result')
        import requests
        with requests.get(row['review_report_url'], stream=True, timeout=(10, 45)) as response:
            response.raise_for_status()
            if not next(response.iter_content(16), b'').startswith(b'%PDF-'):
                raise RuntimeError('Published URL did not return PDF')
        save(job / 'status.json', {'state': 'complete', 'result': final['result'],
             'pdf': str(pdfs[0]), 'url': row['review_report_url'], 'updated_at': datetime.now().isoformat()})
    except Exception as exc:
        logging.exception('Review failed: %s', key)
        save(job / 'status.json', {'state': 'retry', 'error': str(exc),
             'updated_at': datetime.now().isoformat()})
        return 1
    return 0


def wait_processes_stopped(processes, timeout=5):
    """Poll adopted host processes; pidfd_open is unsupported on some hosts."""
    import psutil
    deadline = time.monotonic() + timeout
    while True:
        alive = []
        for process in processes:
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
            except psutil.NoSuchProcess:
                pass
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)


def stop_worker_tree(process):
    """Reap CLI/tool descendants too: each CLI creates its own process group."""
    import psutil
    try:
        parent = psutil.Process(process.pid)
        parent.suspend()  # prevent it spawning another CLI while enumerating
        descendants = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    targets = [*reversed(descendants), parent]
    for target in targets:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    if wait_processes_stopped(targets):
        raise RuntimeError('Worker descendants still alive; refusing overlapping retry')
    if isinstance(process, subprocess.Popen):
        process.poll()  # reap our own child; adopted workers are reaped by init


def supervise(workers):
    papers = scope()
    live = {}
    attempts = load(ROUND / 'attempts.json', {})
    with (ROUND / 'supervisor.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A coordinator restart must preserve workers already reviewing papers.
        import psutil
        for paper in papers:
            state = load(ROUND / 'papers' / paper['key'] / 'status.json', {})
            if state.get('state') != 'running' or not state.get('pid'):
                continue
            try:
                process = psutil.Process(state['pid'])
                command = process.cmdline()
                if (str(Path(__file__).resolve()) in command and 'worker' in command
                        and paper['key'] in command):
                    live[paper['key']] = process
                    logging.info('Adopted existing worker %s pid=%d', paper['key'], process.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        while True:
            for key, process in list(live.items()):
                try:
                    running = (process.is_running() and process.status() != psutil.STATUS_ZOMBIE
                               if isinstance(process, psutil.Process) else process.poll() is None)
                except psutil.NoSuchProcess:
                    running = False
                if not running:
                    job = ROUND / 'papers' / key
                    state = load(job / 'status.json', {})
                    if state.get('state') == 'running':
                        save(job / 'status.json', {'state': 'retry',
                             'error': 'worker exited without final status',
                             'updated_at': datetime.now().isoformat()})
                    del live[key]
                    continue
                try:
                    age = time.time() - psutil.Process(process.pid).create_time()
                except psutil.NoSuchProcess:
                    continue
                if age > WORKER_MAX_SECONDS:
                    logging.error('Worker deadline exceeded key=%s pid=%d', key, process.pid)
                    stop_worker_tree(process)
                    save(ROUND / 'papers' / key / 'status.json', {
                        'state': 'retry', 'error': 'worker total deadline exceeded',
                        'updated_at': datetime.now().isoformat(),
                    })
                    # Never refund attempts: doing so makes the retry infinite.
                    del live[key]
            try:
                db = rows()
            except Exception:
                logging.exception('DB polling failed; preserving queue and retrying')
                time.sleep(30)
                continue
            counts = Counter()
            scholars = {name: Counter() for name in ('韩有承', '戴大伟')}
            waiting = []
            for paper in papers:
                key = paper['key']
                job = ROUND / 'papers' / key
                state = load(job / 'status.json', {})
                if state.get('state') == 'complete':
                    label = 'complete'
                    counts[state['result']] += 1
                elif key in live:
                    label = 'running'
                elif attempts.get(key, 0) >= 3:
                    label = 'needs_recovery'
                    save(job / 'status.json', {**state, 'state': label,
                         'error': state.get('error') or 'worker attempts exhausted',
                         'updated_at': datetime.now().isoformat()})
                else:
                    try:
                        row = db.get((paper['scholar'], paper['doi'].lower()))
                        report = readiness(paper, row)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        label = 'awaiting_initial'
                        waiting.append({'key': key, 'reason': str(exc)})
                    else:
                        label = 'queued'
                        if attempts.get(key, 0) >= 3:
                            label = 'needs_recovery'
                        elif len(live) < workers:
                            if not (job / 'item.json').exists():
                                save(job / 'initial_report.json', load(report))
                                save(job / 'item.json', dict(paper, id=row['id'],
                                     generation_time=str(row['generation_time']), report_sha256=digest(report),
                                     initial_risk=row['risk_level'], initial_row=row))
                            attempts[key] = attempts.get(key, 0) + 1
                            save(ROUND / 'attempts.json', attempts)
                            with (job / 'worker.log').open('a') as stream:
                                live[key] = subprocess.Popen([PYTHON, '-u', str(Path(__file__).resolve()),
                                     'worker', '--key', key], cwd=BASE, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                            save(job / 'status.json', {'state': 'running', 'pid': live[key].pid,
                                 'attempt': attempts[key], 'updated_at': datetime.now().isoformat()})
                            logging.info('Started %s pid=%d', key, live[key].pid)
                            label = 'running'
                counts[label] += 1
                scholars[paper['scholar']][label] += 1
            save(ROUND / 'status.json', {'updated_at': datetime.now().isoformat(), 'pid': os.getpid(),
                 'total': len(papers), 'workers': workers, 'counts': dict(counts),
                 'scholars': scholars, 'waiting': waiting, 'finished': counts['complete'] == len(papers),
                 'settled': counts['complete'] + counts['needs_recovery'] == len(papers)})
            if counts['complete'] == len(papers):
                return 0
            if counts['complete'] + counts['needs_recovery'] == len(papers):
                logging.error('Batch stopped with explicit recovery items; no workers left running')
                return 1
            time.sleep(30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('run', 'worker', 'status'))
    parser.add_argument('--workers', type=int, default=12)
    parser.add_argument('--key')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    ROUND.mkdir(parents=True, exist_ok=True)
    if args.action == 'status':
        print(json.dumps(load(ROUND / 'status.json', {}), ensure_ascii=False, indent=2))
        return 0
    if args.action == 'worker':
        return work(args.key)
    return supervise(max(1, args.workers))


if __name__ == '__main__':
    raise SystemExit(main())
