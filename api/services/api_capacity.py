"""Shared expensive-work budget for user-facing API batches and single papers."""
import asyncio

from api.config import API_MAX_CONCURRENT_PAPERS, API_MAX_CONCURRENT_TASKS, MAX_PENDING_TASKS


class WorkLimiter:
    def __init__(self, limit: int):
        if limit < 1:
            raise ValueError('Work capacity must be positive')
        self.limit = limit
        self.active = 0
        self.waiting = 0
        self._semaphore = asyncio.BoundedSemaphore(limit)

    async def run(self, operation, *args, **kwargs):
        return await self._run(None, operation, *args, **kwargs)

    async def run_until(self, queue_deadline, operation, *args, **kwargs):
        """Queue time counts against the same deadline as the operation."""
        return await self._run(queue_deadline, operation, *args, **kwargs)

    async def _run(self, queue_deadline, operation, *args, **kwargs):
        from api.services.review_runtime import time_left
        self.waiting += 1
        try:
            if queue_deadline is None:
                await self._semaphore.acquire()
            else:
                await asyncio.wait_for(self._semaphore.acquire(), time_left(queue_deadline, None))
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            return await operation(*args, **kwargs)
        finally:
            self.active -= 1
            self._semaphore.release()


api_work = WorkLimiter(API_MAX_CONCURRENT_PAPERS)


def capacity_status() -> dict:
    return {
        'max_concurrent_tasks': API_MAX_CONCURRENT_TASKS,
        'max_concurrent_papers': api_work.limit,
        'max_pending_tasks': MAX_PENDING_TASKS,
        'active_paper_stages': api_work.active,
        'waiting_paper_stages': api_work.waiting,
    }
