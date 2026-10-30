"""One-time migration of the two interrupted Dai Dawei sessions.

Keep all evidence/results/transcripts and grant one worker attempt under the
new finite transport. This is deliberately not an automatic budget reset.
"""
from datetime import datetime
import fcntl
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from scripts import run_scholar_1027_review as runner

KEYS = ('戴大伟/10.1038_s41467-022-33055-9', '戴大伟/10.1126_science.adu2392')


def main():
    marker = runner.ROUND / 'bounded_recovery_migration.json'
    with (runner.ROUND / 'bounded_recovery_migration.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = runner.load(marker, {})
        if record and record.get('phase') != 'stopping':
            raise RuntimeError('Migration already recorded; refuse another retry-budget reset')
        selected = []
        for key in KEYS:
            state = runner.load(runner.ROUND / 'papers' / key / 'status.json', {})
            if state.get('state') != 'complete':
                selected.append(key)
        if not selected:
            print('Both reviews already complete; no migration needed')
            return
        batch = runner.load(runner.ROUND / 'status.json', {})
        expected = str(Path(runner.__file__).resolve())
        if not record:
            coordinator = psutil.Process(batch['pid'])
            assert expected in coordinator.cmdline() and 'run' in coordinator.cmdline()
            coordinator.terminate()
            if runner.wait_processes_stopped([coordinator]):
                raise RuntimeError('Coordinator did not stop; no worker changes made')
        attempts = runner.load(runner.ROUND / 'attempts.json', {})
        before = {key: runner.load(runner.ROUND / 'papers' / key / 'status.json') for key in selected}
        if not record:
            runner.save(marker, {'started_at': datetime.now().isoformat(), 'keys': selected,
                                'status_before': before, 'attempts_before': attempts, 'phase': 'stopping'})
        migrated = []
        for key in selected:
            job = runner.ROUND / 'papers' / key
            state = runner.load(job / 'status.json', {})
            if state.get('state') == 'complete':
                continue
            if state.get('pid'):
                try:
                    process = psutil.Process(state['pid'])
                    if process.status() == psutil.STATUS_ZOMBIE:
                        raise psutil.NoSuchProcess(process.pid)
                    command = process.cmdline()
                    assert expected in command and 'worker' in command and key in command
                    runner.stop_worker_tree(process)
                except psutil.NoSuchProcess:
                    pass
            # Exactly one additional worker attempt, with session budgets
            # and existing first/second review checkpoints preserved.
            attempts[key] = 2
            runner.save(job / 'status.json', {'state': 'retry',
                'error': 'migrated to bounded recovery runtime', 'updated_at': datetime.now().isoformat()})
            migrated.append(key)
        runner.save(runner.ROUND / 'attempts.json', attempts)
        with (runner.ROUND / 'supervisor.log').open('a') as log:
            process = subprocess.Popen([runner.PYTHON, '-u', expected, 'run', '--workers', '39'],
                cwd=runner.BASE, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        record = runner.load(marker)
        runner.save(marker, dict(record, phase='launched', coordinator_pid=process.pid, migrated=migrated))
        print('Bounded recovery launched:', process.pid, migrated)


if __name__ == '__main__':
    main()
