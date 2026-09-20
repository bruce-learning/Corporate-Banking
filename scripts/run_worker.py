"""External Redis Streams worker for memory and ingestion jobs."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core.application.ingestion import recover_interrupted_jobs
from app.core.application.worker import JobProcessor
from app.core.memory.service import MemoryService
from app.infrastructure.cache.store import Cache
from app.infrastructure.database.store import Store
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.queue.redis_stream import JobQueue
from app.infrastructure.vector.chroma import ChromaVectorStore


async def run():
    settings = Settings()
    store, cache = Store(settings.database_url.get_secret_value()), Cache(settings)
    model, vectors = AliyunModelClient(settings), ChromaVectorStore(settings)
    memory = MemoryService(store, cache, vectors, model, settings)
    queue = JobQueue(store, cache, settings)
    processor = JobProcessor(queue, memory, store, model, vectors, settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stopping.set)
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    try:
        await queue.ensure_group()
        await queue.recover_running()
        await recover_interrupted_jobs(store)
        while not stopping.is_set():
            rows = await cache.client.xreadgroup(
                queue.group, consumer, {queue.stream: ">"}, count=10, block=500
            )
            for _, messages in rows:
                for message_id, fields in messages:
                    await processor.process_one(fields.get("job_id"))
                    await cache.client.xack(queue.stream, queue.group, message_id)
            await processor.process_one()
            if not rows:
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=settings.worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass
    finally:
        await model.aclose(); await vectors.close(); await cache.close(); await store.close()


if __name__ == "__main__":
    asyncio.run(run())
