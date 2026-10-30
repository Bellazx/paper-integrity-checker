"""Nonblocking process locks for shared review artifacts (Linux deployment)."""
import contextlib
import fcntl
import os
from pathlib import Path


@contextlib.contextmanager
def exclusive_review(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f'Review already running: {path}') from exc
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    # Do not unlink: another process may already hold the same inode.
