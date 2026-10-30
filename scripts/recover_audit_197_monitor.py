"""Resume failed items only, alongside the original 197-paper coordinator."""
import fcntl
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import converge_audit_20261019_197 as runner


def main():
    root = runner.RD
    counts_path = root / 'recovery_attempts.json'
    counts = runner.load(counts_path) if counts_path.exists() else {}
    papers = runner.load(root / 'papers.json')
    with (root / 'recovery.lock').open('a') as lock, ThreadPoolExecutor(max_workers=32) as pool:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        live = {}
        while True:
            for batch, future in list(live.items()):
                if future.done():
                    print(json.dumps(future.result(), ensure_ascii=False), flush=True)
                    del live[batch]
            complete = 0
            for paper in papers:
                batch = str(paper['batch'])
                state_path = root / 'state' / f'{batch}.json'
                state = runner.load(state_path) if state_path.exists() else {}
                complete += state.get('stage') == 'complete'
                if (state.get('stage') == 'failed' and batch not in live
                        and counts.get(batch, 0) < 2 and len(live) < 32):
                    counts[batch] = counts.get(batch, 0) + 1
                    runner.save(counts_path, counts)
                    live[batch] = pool.submit(runner.process, paper)
                    print(f'Resuming batch {batch}, recovery {counts[batch]}', flush=True)
            if complete == len(papers):
                return
            time.sleep(30)


if __name__ == '__main__':
    main()
