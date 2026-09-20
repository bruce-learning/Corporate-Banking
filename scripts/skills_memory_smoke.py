"""Online smoke for lightweight Skills and three-layer memory; leaves no test user."""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
import time
from uuid import uuid4

import httpx
from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core.application.auth import hash_password
from app.infrastructure.database.store import Store
from app.infrastructure.vector.chroma import ChromaVectorStore
from app.models.entities.storage import User


async def main():
    settings = Settings()
    store = Store(settings.database_url.get_secret_value())
    vectors = ChromaVectorStore(settings)
    username = "memory_smoke_" + uuid4().hex[:10]
    password = secrets.token_urlsafe(24)
    user_id = None
    conversation_id = None
    headers = None
    try:
        async with store.sessions() as session:
            user = User(username=username, role="user", password_hash=await hash_password(password))
            session.add(user); await session.commit(); user_id = user.id
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=120) as client:
            login = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
            login.raise_for_status()
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            created = await client.post("/api/v1/conversations", headers=headers,
                                        json={"title": "Skills与记忆在线验收-完成后删除"})
            created.raise_for_status(); conversation_id = created.json()["id"]

            first = await client.post("/api/v1/chat", headers=headers, json={
                "conversation_id": conversation_id,
                "message": "我们公司是小微企业，请问企业网银有什么功能？",
            })
            first.raise_for_status(); first_body = first.json()
            assert "对公产品咨询规范" in first_body["skills_used"].get("product", [])
            assert first_body["memory_context"]["write"]["status"] == "queued"
            deadline = time.monotonic() + 30
            profile_items = []
            while time.monotonic() < deadline:
                profile_response = await client.get("/api/v1/memories/profiles", headers=headers)
                profile_response.raise_for_status(); profile_items = profile_response.json()["items"]
                if profile_items:
                    break
                await asyncio.sleep(0.25)
            assert profile_items

            second = await client.post("/api/v1/chat", headers=headers, json={
                "conversation_id": conversation_id, "message": "那它怎么收费？",
            })
            second.raise_for_status(); second_body = second.json()
            assert "费用政策与合规规范" in second_body["skills_used"].get("policy", [])
            assert second_body["memory_context"]["profile_ids"]

            for _ in range(4):
                greeting = await client.post("/api/v1/chat", headers=headers, json={
                    "conversation_id": conversation_id, "message": "你好",
                })
                greeting.raise_for_status()

            deadline = time.monotonic() + 30
            summary_items = []
            while time.monotonic() < deadline:
                summary_response = await client.get("/api/v1/memories/summaries", headers=headers)
                summary_response.raise_for_status(); summary_items = summary_response.json()["items"]
                if summary_items:
                    break
                await asyncio.sleep(0.25)
            assert summary_items

            final = await client.post("/api/v1/chat", headers=headers, json={
                "conversation_id": conversation_id, "message": "企业网银还有什么功能？",
            })
            final.raise_for_status(); final_body = final.json()
            assert final_body["memory_context"]["summary_ids"]
            assert final_body["memory_context"]["profile_ids"]
            print(json.dumps({
                "skills_product": first_body["skills_used"]["product"],
                "skills_policy": second_body["skills_used"]["policy"],
                "profile_recalled": True,
                "summary_recalled": True,
                "working_memory_messages": settings.context_message_limit,
                "degraded": final_body["degraded"],
            }, ensure_ascii=False, indent=2))
    finally:
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
                await session.execute(delete(User).where(User.id == user_id))
                await session.commit()
        await vectors.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
