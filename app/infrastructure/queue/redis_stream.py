from __future__ import annotations

from datetime import timedelta
from sqlalchemy import select

from app.models.entities.storage import BackgroundJob, utcnow


class JobQueue:
    """PostgreSQL is authoritative; Redis Streams only wakes workers."""

    def __init__(self, store, cache, settings, metrics=None):
        self.store = store
        self.cache = cache
        self.settings = settings
        self.metrics = metrics
        self.stream = cache.key("jobs", "stream")
        self.group = cache.key("jobs", "workers")

    async def enqueue(self, job_type: str, payload: dict, *, user_id: str | None = None,
                      max_attempts: int | None = None) -> BackgroundJob:
        async with self.store.sessions() as session:
            job = BackgroundJob(
                user_id=user_id, job_type=job_type, payload_json=payload,
                status="queued", max_attempts=max_attempts or self.settings.worker_max_attempts,
            )
            session.add(job); await session.commit()
        await self.notify(job.id)
        if self.metrics:
            self.metrics.increment("bank_background_jobs_enqueued_total", job_type=job_type)
        return job

    async def notify(self, job_id: str) -> bool:
        try:
            await self.cache.client.xadd(
                self.stream, {"job_id": job_id}, maxlen=self.settings.worker_stream_maxlen,
                approximate=True,
            )
            return True
        except Exception:
            return False

    async def ensure_group(self) -> None:
        try:
            await self.cache.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def claim(self, job_id: str | None = None) -> BackgroundJob | None:
        async with self.store.sessions() as session:
            statement = select(BackgroundJob).where(
                BackgroundJob.status.in_(["queued", "retrying"]),
                BackgroundJob.available_at <= utcnow(),
            )
            if job_id:
                statement = statement.where(BackgroundJob.id == job_id)
            statement = statement.order_by(BackgroundJob.created_at).with_for_update(skip_locked=True).limit(1)
            job = await session.scalar(statement)
            if not job:
                return None
            job.status = "running"
            job.attempts += 1
            job.started_at = utcnow()
            job.error_code = None
            job.error_message = None
            await session.commit()
            session.expunge(job)
            return job

    async def complete(self, job_id: str, result: dict | None = None) -> None:
        async with self.store.sessions() as session:
            job = await session.get(BackgroundJob, job_id, with_for_update=True)
            if not job:
                return
            job.status = "completed"
            job.result_json = result or {}
            job.completed_at = utcnow()
            await session.commit()
        if self.metrics:
            self.metrics.increment("bank_background_jobs_completed_total", job_type=job.job_type)

    async def fail(self, job_id: str, code: str, message: str) -> None:
        retry = False
        async with self.store.sessions() as session:
            job = await session.get(BackgroundJob, job_id, with_for_update=True)
            if not job:
                return
            retry = job.attempts < job.max_attempts
            job.status = "retrying" if retry else "failed"
            job.error_code = code[:40]
            job.error_message = message[:300]
            job.available_at = utcnow() + timedelta(seconds=min(2 ** job.attempts, 60))
            if not retry:
                job.completed_at = utcnow()
            await session.commit()
        if retry:
            await self.notify(job_id)
        if self.metrics:
            self.metrics.increment("bank_background_jobs_failed_total", job_type=job.job_type,
                                   terminal="false" if retry else "true")

    async def recover_running(self) -> int:
        async with self.store.sessions() as session:
            jobs = list((await session.scalars(select(BackgroundJob).where(
                BackgroundJob.status == "running"
            ))).all())
            for job in jobs:
                job.status = "retrying"
                job.error_code = "worker_interrupted"
                job.error_message = "Worker重启后等待重试"
                job.available_at = utcnow()
            if jobs:
                await session.commit()
        for job in jobs:
            await self.notify(job.id)
        return len(jobs)
