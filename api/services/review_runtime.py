"""Resource and wall-clock budgets shared by API and batch review workers."""
import contextvars
import asyncio
import os
import time


paper_deadline = contextvars.ContextVar('review_paper_deadline', default=None)
api_review_budget = contextvars.ContextVar('api_review_budget', default=False)
_evidence_gate = None
_evidence_loop = None


async def limited_evidence(operation, *args, **kwargs):
    """Limit memory-heavy evidence builders while allowing paper concurrency."""
    global _evidence_gate, _evidence_loop
    loop = asyncio.get_running_loop()
    if _evidence_gate is None or _evidence_loop is not loop:
        _evidence_gate = asyncio.Semaphore(max(1, int(os.environ.get('REVIEW_EVIDENCE_CONCURRENCY', '2'))))
        _evidence_loop = loop
    async with _evidence_gate:
        job = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            await job
            raise


def deadline_after(seconds):
    """Monotonic deadline ``seconds`` from now; None when the limit is off."""
    return time.monotonic() + seconds if seconds and seconds > 0 else None


def time_left(deadline, cap, reserve=0.0, floor=0.0):
    """Seconds a stage may run: its own cap, shortened to what is left before
    ``deadline`` minus ``reserve``. A floor may not extend a hard deadline;
    expired work must not be launched, including on a retry."""
    if deadline is None:
        return cap
    left = deadline - reserve - time.monotonic()
    if left <= 0:
        raise TimeoutError('Whole-paper deadline exhausted')
    return min(cap, left) if cap else left


def subprocess_budget(configured):
    """Include evidence subprocesses in the caller's whole-paper deadline.

    Context is copied into asyncio.to_thread. Without this clamp cancellation
    drains a 100-minute evidence job even after the paper deadline has expired.
    """
    deadline = paper_deadline.get()
    if deadline is None:
        return configured
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Whole-paper deadline exhausted during evidence preparation')
    return min(configured, remaining)


def numeric_subprocess_env():
    """Parallelize papers, not dozens of BLAS threads inside every paper."""
    env = os.environ.copy()
    # Override inherited host defaults (often 64/128 threads). A dedicated
    # review setting is available for deliberately provisioned deployments.
    threads = str(max(1, int(env.get('REVIEW_NUMERIC_THREADS', '1'))))
    for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS'):
        env[name] = threads
    return env
