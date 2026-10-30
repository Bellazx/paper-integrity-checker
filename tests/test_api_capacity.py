import asyncio

from api.services.api_capacity import WorkLimiter


def test_120_jobs_start_without_waiting_and_excess_job_waits():
    async def scenario():
        limiter = WorkLimiter(120)
        started = 0
        released = asyncio.Event()

        async def work():
            nonlocal started
            started += 1
            await released.wait()

        jobs = [asyncio.create_task(limiter.run(work)) for _ in range(121)]
        await asyncio.sleep(0)
        assert started == limiter.active == 120
        assert limiter.waiting == 1
        released.set()
        await asyncio.gather(*jobs)
        assert started == 121
        assert limiter.active == limiter.waiting == 0

    asyncio.run(scenario())


def test_failed_and_cancelled_jobs_return_capacity():
    async def scenario():
        limiter = WorkLimiter(1)
        block = asyncio.Event()
        first = asyncio.create_task(limiter.run(block.wait))
        second = asyncio.create_task(limiter.run(block.wait))
        await asyncio.sleep(0)
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        assert limiter.active == 1 and limiter.waiting == 0
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert limiter.active == limiter.waiting == 0

        async def fail():
            raise RuntimeError('failed')

        results = await asyncio.gather(limiter.run(fail), return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        assert limiter.active == 0
        block.set()
        assert await limiter.run(block.wait) is True

    asyncio.run(scenario())
