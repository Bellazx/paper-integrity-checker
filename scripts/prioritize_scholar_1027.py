"""Reallocate this pair of review batches without discarding saved evidence."""
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import converge_audit_20261019_197 as audit


def main():
    targets = []
    names = {'converge_audit_20261019_197.py', 'recover_audit_197_monitor.py'}
    for process in psutil.process_iter(['pid', 'cmdline']):
        command = process.info['cmdline'] or []
        matches = [arg for arg in command if Path(arg).name in names]
        if not matches:
            continue
        # Only this project's known coordinators; never infer ownership from
        # a generic Python/Claude process name.
        if Path(process.cwd()).resolve() != audit.BASE.resolve():
            raise RuntimeError('Unexpected coordinator working directory')
        if Path(matches[0]).name == 'converge_audit_20261019_197.py' and 'run' not in command:
            continue
        targets.append((process, process.children(recursive=True)))
    if not targets and '--resume-scheduling' not in sys.argv:
        raise RuntimeError('No audit coordinator found; inspect before rescheduling')
    stopped = []
    for process, descendants in targets:
        process.terminate()
        stopped.append(process)
        for child in reversed(descendants):
            try:
                child.terminate()
                stopped.append(child)
            except psutil.NoSuchProcess:
                pass
    def alive_processes(processes):
        alive = []
        for process in processes:
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
            except psutil.NoSuchProcess:
                pass
        return alive

    deadline = time.monotonic() + 5
    alive = alive_processes(stopped)
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = alive_processes(alive)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    # Check that no detached model process from the old pool remains before
    # starting the new pool. Avoid pidfd waits, unsupported for some host PIDs.
    for process in psutil.process_iter(['pid', 'cmdline']):
        try:
            if Path(process.cwd()).is_relative_to(audit.RD / 'work'):
                if process.status() != psutil.STATUS_ZOMBIE:
                    raise RuntimeError(f'Old audit worker is still live: {process.pid}')
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    queued = 0
    for path in (audit.RD / 'state').glob('*.json'):
        state = audit.load(path)
        if state['stage'] != 'complete':
            state.update(deferred_from=state['stage'], stage='queued',
                         updated_at=datetime.now().isoformat())
            audit.save(path, state)
            queued += 1
    with (audit.RD / 'supervisor.log').open('a') as stream:
        process = subprocess.Popen(
            [str(audit.PY), '-u', str(audit.BASE / 'scripts/converge_audit_20261019_197.py'),
             'run', '--workers', '8'], cwd=audit.BASE, stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    print(f'Audit coordinator pid={process.pid}, workers=8; saved unfinished states={queued}')


if __name__ == '__main__':
    main()
