from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace
from uuid import uuid4
from urllib.parse import urlsplit
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete
from app.config import PROJECT_ROOT, Settings
from app.core.application.auth import hash_password
from app.main import create_app
from app.models.entities.storage import User


class ReadyTestGateway:
    async def ready(self):
        return True

    async def search(self, domain, query, top_k=4):
        raise AssertionError("需要聊天工具结果的测试必须显式安装 FakeGateway")


@asynccontextmanager
async def client_for(application):
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture
async def runtime():
    deployed = Settings()
    if not deployed.database_url.get_secret_value() or not deployed.redis_url.get_secret_value():
        pytest.skip("集成测试需要独立 PostgreSQL 和 Redis")
    test_url = deployed.database_url.get_secret_value().rsplit("/", 1)[0] + "/bank_test"
    if urlsplit(test_url).path != "/bank_test":
        raise RuntimeError("禁止在业务数据库准备测试数据")
    prefix = "bank:test:" + uuid4().hex
    chroma_collection = "test_" + uuid4().hex
    summary_collection = "summary_" + uuid4().hex
    profile_collection = "profile_" + uuid4().hex
    config = Settings(_env_file=None, database_url=test_url, redis_url=deployed.redis_url,
                      cache_prefix=prefix, app_env="test", enable_paid_model_calls=False,
                      chroma_mode="persistent", chroma_collection=chroma_collection,
                      memory_summary_collection=summary_collection,
                      memory_profile_collection=profile_collection,
                      chroma_path=PROJECT_ROOT / "var" / "test-chroma")
    application = create_app(config, mcp_gateway=ReadyTestGateway())
    async with client_for(application) as client:
        users = {}
        async with application.state.store.sessions() as session:
            for name, role in [("alice", "user"), ("bob", "user"), ("manager", "admin")]:
                item = User(username=name + "_" + uuid4().hex[:10], role=role,
                            password_hash=await hash_password("Synthetic-test-password-123"))
                session.add(item)
                await session.flush()
                users[name] = item
            await session.commit()
        value = SimpleNamespace(app=application, client=client, users=users, settings=config,
                                store=application.state.store, cache=application.state.cache)
        try:
            yield value
        finally:
            async with value.store.sessions() as session:
                await session.execute(delete(User).where(User.id.in_([user.id for user in users.values()])))
                await session.commit()
            keys = [key async for key in value.cache.client.scan_iter(prefix + ":*")]
            if keys:
                await value.cache.client.delete(*keys)
            for collection_name in [chroma_collection, summary_collection, profile_collection]:
                await asyncio.to_thread(
                    application.state.vector_store.client.delete_collection,
                    name=collection_name,
                )


async def login(runtime, name="alice"):
    response = await runtime.client.post("/api/v1/auth/login", json={
        "username": runtime.users[name].username, "password": "Synthetic-test-password-123"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def conversation(runtime, headers):
    response = await runtime.client.post("/api/v1/conversations", headers=headers, json={"title": "合成测试"})
    assert response.status_code == 201
    return response.json()["id"]
