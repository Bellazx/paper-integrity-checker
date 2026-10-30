#!/usr/bin/env python3
"""Apply an already-tested API patch after the expected process drains its jobs."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request

TASKS_DIR = Path(__file__).resolve().parents[1] / 'data/tasks'


def unfinished():
    counts = {}
    for status in ('queued', 'extracting', 'detecting', 'reviewing', 'generating_report'):
        with urllib.request.urlopen(
            f'http://127.0.0.1:8001/api/task?limit=1&status={status}', timeout=10
        ) as response:
            counts[status] = int(json.load(response)['total'])
    # The old API may cache a failed status while an isolated recovery worker
    # is active. Restarting then would make startup launch a second worker.
    disk_counts = dict.fromkeys(counts, 0)
    for path in TASKS_DIR.glob('*/status.json'):
        record = json.loads(path.read_text())
        if record.get('status') in disk_counts:
            disk_counts[record['status']] += 1
    for status, count in disk_counts.items():
        counts[status] = max(counts[status], count)
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-pid', required=True, type=int)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        pid = int(subprocess.check_output(
            ['systemctl', 'show', 'paper-api.service', '--property=MainPID', '--value'], text=True).strip())
        if pid != args.expected_pid:
            print('API process changed; no restart performed.', flush=True)
            return 0
        try:
            counts = unfinished()
        except Exception as exc:
            print(f'Cannot verify idle state: {type(exc).__name__}; no restart.', flush=True)
            if args.check_only:
                return 1
            time.sleep(30)
            continue
        print(json.dumps(counts), flush=True)
        if args.check_only:
            return 0
        if not any(counts.values()):
            # Recheck to avoid deploying during a transient boundary between stages.
            time.sleep(10)
            try:
                if any(unfinished().values()):
                    continue
            except Exception:
                continue
            pid = int(subprocess.check_output(
                ['systemctl', 'show', 'paper-api.service', '--property=MainPID', '--value'], text=True).strip())
            if pid != args.expected_pid:
                return 0
            subprocess.run(['systemctl', 'restart', 'paper-api.service'], check=True)
            print('Restarted idle API to load tested patch.', flush=True)
            # systemd considers uvicorn started before its listener is ready.
            for _ in range(30):
                time.sleep(2)
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8001/api/health', timeout=3) as response:
                        if json.load(response).get('status') == 'ok':
                            print('API health verified.', flush=True)
                            return 0
                except Exception:
                    pass
            raise RuntimeError('API restarted but readiness was not confirmed')
        time.sleep(30)
    raise TimeoutError('API did not become idle within one hour; no restart performed')


if __name__ == '__main__':
    raise SystemExit(main())
