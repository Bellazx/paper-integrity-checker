#!/usr/bin/env python3
"""Snapshot all submissions, then exercise production stages offline from SQL.

prepare performs SELECT only. run must be launched inside the database network
guard and never uses TaskManager or database persistence services.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
log = logging.getLogger('submission-test')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def prepare(run_dir, ssh_socket):
    import pymssql
    from api.config import DB_CONFIG, resolve_upload_dir
    run_dir.mkdir(parents=True, exist_ok=False)
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor(as_dict=True)
        cursor.execute('''SELECT id,submission_no,task_id,sub_mode,file_name,saved_name,
            file_size,status,papers_json,report_language FROM dbo.paper_submissions ORDER BY id''')
        rows = cursor.fetchall()
    finally:
        conn.close()
    save(run_dir / 'source_rows.json', rows)
    remote = subprocess.run(['ssh', '-S', ssh_socket, 'root@10.119.9.99',
        "find /opt/paper-integrity-checker/data/uploads -type f -name 'upload.*'"],
        text=True, capture_output=True, check=True).stdout.splitlines()
    jobs = []
    for row in rows:
        job = dict(row)
        candidates = sorted(resolve_upload_dir(row['submission_no']).glob('upload.*'))
        remote_candidates = [p for p in remote if Path(p).parent.name == row['submission_no']]
        sources = candidates or remote_candidates
        if len(sources) != 1:
            job.update(status='missing_material', error=f'Expected one archive, found {len(sources)}')
        else:
            source = sources[0]
            target = run_dir / 'archives' / str(row['id']) / Path(source).name
            target.parent.mkdir(parents=True)
            if candidates:
                shutil.copy2(source, target)
            else:
                subprocess.run(['scp', '-q', '-o', f'ControlPath={ssh_socket}',
                    'root@10.119.9.99:' + str(source), str(target)], check=True)
            if target.stat().st_size != row['file_size']:
                job.update(status='material_mismatch', error='Archive size differs from submission metadata')
            else:
                job.update(status='queued', archive=str(target), archive_sha256=sha256(target),
                           material_origin=str(source))
        jobs.append(job)
        save(run_dir / 'manifest.json', {'table': 'dbo.paper_submissions',
            'snapshot_at': datetime.now().isoformat(), 'row_count': len(rows), 'jobs': jobs})
        print(row['id'], job['status'], flush=True)
    project = run_dir / 'project'
    project.mkdir()
    for name in ('api', 'core', 'modules', 'utils', 'scripts', 'static', 'skills'):
        shutil.copytree(ROOT / name, project / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for source in ROOT.glob('*.py'):
        shutil.copy2(source, project / source.name)
    if (ROOT / '.env').exists():
        shutil.copy2(ROOT / '.env', project / '.env')
    (project / 'venv').symlink_to(ROOT / 'venv', target_is_directory=True)
    hashes = {str(p.relative_to(project)): sha256(p) for p in project.rglob('*.py')}
    save(run_dir / 'code_hashes.json', hashes)
    print('PREPARED', json.dumps(collections.Counter(j['status'] for j in jobs)), flush=True)


def check_guard_cgroup():
    memberships = Path('/proc/self/cgroup').read_text().splitlines()
    if not any('net_cls' in line.split(':')[1].split(',') and
               line.split(':')[2] == '/paper-submissions-test' for line in memberships):
        raise RuntimeError('Test is not inside the required database-fenced cgroup')
    class_id = Path('/sys/fs/cgroup/net_cls/paper-submissions-test/net_cls.classid')
    if int(class_id.read_text().strip()) != 0x519928:
        raise RuntimeError('Unexpected network guard class')


def check_database_is_blocked():
    # Fail closed if the kernel guard was omitted. No SQL command is issued.
    from api.config import DB_CONFIG
    check_guard_cgroup()
    with socket.socket() as connection:
        connection.settimeout(3)
        try:
            connection.connect((DB_CONFIG['server'], 1433))
        except OSError as exc:
            if exc.errno not in (1, 13, 113):
                raise RuntimeError(f'DB must be blocked by policy, not merely unreachable: {exc}') from exc
            return {'database_network_blocked': True, 'errno': exc.errno,
                    'checked_at': datetime.now().isoformat()}
    raise RuntimeError('Refusing test: database connection is reachable')


def validate_pdf(path):
    import fitz
    path = Path(path)
    with path.open('rb') as source:
        if source.read(5) != b'%PDF-':
            raise RuntimeError(f'Invalid PDF header: {path}')
    with fitz.open(path) as pdf:
        if pdf.page_count < 1:
            raise RuntimeError(f'Empty PDF: {path}')
        return {'path': str(path), 'pages': pdf.page_count, 'bytes': path.stat().st_size}


async def run(run_dir, concurrency):
    from api.config import OUTPUT_DIR, REVIEW_DIR
    from api.services.detect import run_detection_single
    from api.services.review import run_review_single
    from api.services.report import generate_reports, _review_report_slug
    from api.services.zip_handler import extract_single, extract_batch
    from modules.chinese_report_generator import _compute_overall_risk
    from modules.english_report_generator import generate_english_initial_pdf, generate_english_review_pdf
    if ROOT != run_dir / 'project':
        raise RuntimeError('Run must use the isolated project snapshot')
    guard = check_database_is_blocked()
    save(run_dir / 'database_guard.json', guard)
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    states = {str(j['id']): {'id': j['id'], 'submission_no': j['submission_no'],
              'status': j['status'], 'error': j.get('error')} for j in manifest['jobs']}
    semaphore = asyncio.Semaphore(concurrency)

    def checkpoint():
        save(run_dir / 'progress.json', {'updated_at': datetime.now().isoformat(),
            'total': len(states), 'concurrency': concurrency,
            'counts': dict(collections.Counter(s['status'] for s in states.values())),
            'database_writes_enabled': False, 'submissions': list(states.values())})

    async def one(job):
        state = states[str(job['id'])]
        if job['status'] != 'queued':
            return
        async with semaphore:
            started = datetime.now().isoformat()
            state.update(status='extracting', started_at=started, papers=[])
            checkpoint()
            try:
                archive = Path(job['archive'])
                if sha256(archive) != job['archive_sha256']:
                    raise RuntimeError('Material changed after snapshot')
                task_id = f'submission_test_{job["id"]}'
                extractor = extract_batch if job['sub_mode'] == 'batch' else extract_single
                papers = await asyncio.to_thread(extractor, archive, task_id)
                if not papers:
                    raise RuntimeError('No papers extracted')
                for index, paper in enumerate(papers):
                    namespace = f'submission_tests/{job["id"]}/{index}'
                    details = {'index': index, 'reports': []}
                    state['papers'].append(details)
                    state['status'] = 'detecting'; checkpoint()
                    findings = await run_detection_single(paper.input_dir, paper.output_dir,
                        write_db=False, doi=paper.doi, report_namespace=namespace)
                    details['doi'] = (findings.get('paper') or {}).get('doi') or paper.doi
                    details['initial_risk'] = _compute_overall_risk(findings)['level']
                    if not findings.get('pdf_generated'):
                        raise RuntimeError('Initial Chinese PDF was not generated')
                    initial = list((OUTPUT_DIR / 'chinese_reports' / namespace).glob('*.pdf'))
                    if len(initial) != 1:
                        raise RuntimeError(f'Expected one initial PDF, got {len(initial)}')
                    details['reports'].append(validate_pdf(initial[0]))
                    english = await asyncio.to_thread(generate_english_initial_pdf, findings,
                                                       OUTPUT_DIR / 'english_reports' / namespace)
                    details['reports'].append(validate_pdf(english))
                    if details['initial_risk'] == '高风险':
                        state['status'] = 'reviewing'; checkpoint()
                        report_json = str(Path(paper.output_dir) / 'report.json')
                        review = await run_review_single(details['doi'], report_json,
                            paper.input_dir, paper.output_dir, resume=False)
                        if review.get('trigger') == 'review_error':
                            raise RuntimeError(review.get('reason') or 'Review failed')
                        review.update(_input_dir=paper.input_dir, _report_json=report_json,
                                      _report_slug=paper.doi_slug)
                        details['review_risk'] = review.get('result')
                        state['status'] = 'rendering'; checkpoint()
                        paths = await generate_reports([review], run_dir / 'results' / str(job['id']) / str(index),
                            write_db=False, report_namespace=namespace)
                        details['reports'].extend(validate_pdf(p) for p in paths)
                        english = await asyncio.to_thread(generate_english_review_pdf, review,
                            REVIEW_DIR / namespace, _review_report_slug(review))
                        details['reports'].append(validate_pdf(english))
                state.update(status='completed', error=None)
            except Exception as exc:
                state['failed_stage'] = state['status']
                state.update(status='failed', error=str(exc))
                log.exception('Submission %s failed', job['id'])
            finally:
                state['finished_at'] = datetime.now().isoformat()
                save(run_dir / 'results' / str(job['id']) / 'status.json', state)
                checkpoint()
    checkpoint()
    await asyncio.gather(*(one(job) for job in manifest['jobs']))
    save(run_dir / 'finished.json', {'finished_at': datetime.now().isoformat(),
         'counts': dict(collections.Counter(s['status'] for s in states.values())),
         'total': len(states), 'database_guard': check_database_is_blocked()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'run'])
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--ssh-socket', default='')
    parser.add_argument('--concurrency', type=int, default=54)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    if args.mode == 'prepare':
        prepare(args.run_dir.resolve(), args.ssh_socket)
    else:
        if args.concurrency < 1:
            parser.error('concurrency must be positive')
        asyncio.run(run(args.run_dir.resolve(), args.concurrency))


if __name__ == '__main__':
    main()
