"""Replace accidental fresh retries with their more advanced saved sessions."""
import hashlib
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time

import psutil

from run_scholar_1027_review import BASE, ROUND, PYTHON, load, save
from scholar_1027_cli import prior_session, stable_prompt


def running(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def main():
    selected = []
    for job in (ROUND / 'papers').glob('*/*'):
        state = load(job / 'status.json', {})
        if state.get('state') != 'running':
            continue
        checkpoints = [p for p in (job / 'cli_sessions').glob('*.json') if '_response' not in p.name]
        if not checkpoints:
            continue
        latest = load(max(checkpoints, key=lambda p: p.stat().st_mtime))
        try:
            worker = psutil.Process(state['pid'])
            upgrade = ('--upgrade-old-runtime' in sys.argv
                       and worker.create_time() < datetime(2026, 10, 27, 16, 35).timestamp())
            if not upgrade and sum(load(p).get('label') == latest['label'] for p in checkpoints) < 2:
                continue
            key = str(job.relative_to(ROUND / 'papers'))
            assert key in worker.cmdline() and 'worker' in worker.cmdline()
            for child in worker.children():
                command = child.cmdline()
                if '-p' not in command or not any(flag in command for flag in ('--session-id', '--resume')):
                    continue
                prompt = command[command.index('-p') + 1]
                if prompt.startswith('接续本会话已有核查'):
                    prompt = prompt.split('\n', 1)[1]
                best = prior_session(prompt, BASE, job / 'cli_sessions')
                if best and (best != latest['session_id'] or upgrade):
                    selected.append((job, worker, prompt, best, latest, key))
        except psutil.NoSuchProcess:
            continue
    if not selected:
        print('No accidental fresh retries found')
        return
    coordinator = psutil.Process(load(ROUND / 'status.json')['pid'])
    command = coordinator.cmdline()
    assert 'run' in command and any(Path(arg).name == 'run_scholar_1027_review.py' for arg in command)
    coordinator.terminate()
    while running(coordinator):
        time.sleep(0.1)
    counts = load(ROUND / 'attempts.json', {})
    restored = []
    for job, worker, prompt, best, latest, key in selected:
        current = load(job / 'status.json', {})
        if current.get('state') != 'running' or current.get('pid') != worker.pid or not running(worker):
            continue
        children = worker.children(recursive=True)
        worker.terminate()
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        deadline = time.monotonic() + 3
        while any(running(p) for p in [worker, *children]) and time.monotonic() < deadline:
            time.sleep(0.1)
        for process in [worker, *children]:
            if running(process):
                process.kill()
        digest = hashlib.sha256(stable_prompt(prompt).encode()).hexdigest()[:20]
        save(job / 'cli_sessions' / f'{digest}.json', {
            'session_id': best, 'step': 0, 'label': latest['label'], 'doi': latest['doi'],
            'restored_from_accidental_restart': latest['session_id']})
        counts[key] = max(0, counts.get(key, 1) - 1)
        save(job / 'status.json', {'state': 'retry', 'error': 'planned checkpoint resume'})
        restored.append(key)
    save(ROUND / 'attempts.json', counts)
    with (ROUND / 'supervisor.log').open('a') as stream:
        process = subprocess.Popen([PYTHON, '-u', str(BASE / 'scripts/run_scholar_1027_review.py'),
                                    'run', '--workers', '39'], cwd=BASE, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    print('coordinator', process.pid, 'restored', restored)


if __name__ == '__main__':
    main()
