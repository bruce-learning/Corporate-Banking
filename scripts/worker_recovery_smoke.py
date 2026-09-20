"""Verify chat stays available while the async worker is stopped and catches up after restart."""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core.application.auth import hash_password
from app.infrastructure.database.store import Store
from app.infrastructure.vector.chroma import ChromaVectorStore
from app.models.entities.storage import BackgroundJob, User
from scripts.native import start_worker, stop_worker, worker_pid


async def main() -> None:
    settings = Settings()
    store = Store(settings.database_url.get_secret_value())
    vectors = ChromaVectorStore(settings)
    username = "worker_smoke_" + uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    user_id = None
    conversation_id = None
    headers = None
    stopped_for_test = False
    try:
        async with store.sessions() as session:
            user = User(username=username, role="user", password_hash=await hash_password(password))
            session.add(user)
            await session.commit()
            user_id = user.id

        stop_worker()
        stopped_for_test = True
        if worker_pid():
            raise RuntimeError("Worker 未停止，无法执行恢复验收")

        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=120) as client:
            login = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
            login.raise_for_status()
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            created = await client.post(
                "/api/v1/conversations", headers=headers,
                json={"title": "Worker恢复验收-完成后删除"},
            )
            created.raise_for_status()
            conversation_id = created.json()["id"]

            response = await client.post("/api/v1/chat", headers=headers, json={
                "conversation_id": conversation_id,
                "message": "我们公司是小微企业，请介绍企业网银的主要功能。",
            })
            response.raise_for_status()
            payload = response.json()
            write = payload["memory_context"]["write"]
            assert write["status"] == "queued"
            job_id = write["job_id"]

            # Worker 停止时请求已经返回，但长期记忆不应在 API 进程内偷偷执行。
            profiles = await client.get("/api/v1/memories/profiles", headers=headers)
            profiles.raise_for_status()
            assert profiles.json()["items"] == []

            start_worker()
            stopped_for_test = False
            deadline = time.monotonic() + 30
            profile_items = []
            while time.monotonic() < deadline:
                profiles = await client.get("/api/v1/memories/profiles", headers=headers)
                profiles.raise_for_status()
                profile_items = profiles.json()["items"]
                if profile_items:
                    break
                await asyncio.sleep(0.25)
            assert profile_items

            print(json.dumps({
                "chat_succeeded_while_worker_stopped": True,
                "memory_job_id": job_id,
                "memory_processed_after_restart": True,
                "worker_running": bool(worker_pid()),
                "degraded": payload["degraded"],
            }, ensure_ascii=False, indent=2))
    finally:
        if stopped_for_test or not worker_pid():
            start_worker()
        if conversation_id and headers:
            try:
                async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30) as client:
                    await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)
            except Exception:
                pass
        if user_id:
            await vectors.delete_memory("summary", user_id=user_id)
            await vectors.delete_memory("profile", user_id=user_id)
            async with store.sessions() as session:
                await session.execute(delete(BackgroundJob).where(BackgroundJob.user_id == user_id))
                await session.execute(delete(User).where(User.id == user_id))
                await session.commit()
        await vectors.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
