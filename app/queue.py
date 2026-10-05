import asyncio
import logging
import uuid

logger = logging.getLogger("frameforge.queue")


class QueueSignal:
    def __init__(self, settings):
        self.settings = settings
        self.event = asyncio.Event()
        self.redis = None
        if settings.queue_backend == "redis":
            from redis.asyncio import Redis
            self.redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=3)

    async def notify(self, job_id):
        self.event.set()
        if self.redis:
            try:
                await self.redis.rpush("frameforge:jobs", job_id)
                await self.redis.ltrim("frameforge:jobs", -1000, -1)
            except Exception:
                logger.warning("Redis unavailable; durable SQLite queue retains job %s", job_id)

    async def wait(self):
        if self.redis:
            try:
                await self.redis.blpop("frameforge:jobs", timeout=1)
                return
            except Exception:
                logger.warning("Redis unavailable; reconciling durable jobs")
        try:
            await asyncio.wait_for(self.event.wait(), timeout=1)
        except TimeoutError:
            pass
        self.event.clear()

    async def close(self):
        if self.redis:
            await self.redis.aclose()


class Coordinator:
    def __init__(self, store, pipeline, settings, signal):
        self.store, self.pipeline, self.settings, self.signal = store, pipeline, settings, signal
        self.owner = uuid.uuid4().hex
        self.running = set()
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            self.running = {task for task in self.running if not task.done()}
            while len(self.running) < self.settings.max_concurrent_jobs:
                # Each claim gets a distinct token: a cancelled task must never
                # regain ownership when that same job is immediately retried.
                claim_owner = f"{self.owner}:{uuid.uuid4().hex}"
                job = self.store.claim(claim_owner)
                if not job:
                    break
                task = asyncio.create_task(self.pipeline.run(job["id"], claim_owner))
                self.running.add(task)
            await self.signal.wait()

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for task in self.running:
            task.cancel()
        await asyncio.gather(*self.running, return_exceptions=True)
