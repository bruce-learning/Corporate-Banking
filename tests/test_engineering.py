import asyncio
from datetime import timedelta
import logging
from uuid import uuid4
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update, func
from app.api.dependencies import digest_token
from app.core.application.auth import hasher
from app.core.application.conversations import append_message
from app.core.memory.context import recent_context
from app.infrastructure.cache.store import Cache
from app.infrastructure.database.store import Store
from app.infrastructure.llm.contracts import ChatInput, FakeModelClient, ModelMessage
from app.main import create_app
from app.models.entities.storage import AuthSession, Conversation, Message, RequestRecord, User, utcnow
from tests.conftest import client_for, conversation, login


async def test_auth_login_me_logout(runtime):
    headers = await login(runtime)
    response = await runtime.client.get("/api/v1/auth/me", headers=headers)
    assert response.json()["id"] == runtime.users["alice"].id
    assert response.headers["cache-control"] == "no-store"
    assert "x-request-id" in response.headers
    assert (await runtime.client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
    assert (await runtime.client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_wrong_and_unknown_user_same_response(runtime):
    first = await runtime.client.post("/api/v1/auth/login", json={"username": runtime.users["alice"].username, "password": "wrong"})
    other = await runtime.client.post("/api/v1/auth/login", json={"username": "unknown_" + uuid4().hex, "password": "wrong"})
    assert first.status_code == other.status_code == 401
    assert first.json() == other.json()


async def test_login_rate_limit(runtime):
    body = {"username": runtime.users["alice"].username, "password": "wrong"}
    for _ in range(5):
        assert (await runtime.client.post("/api/v1/auth/login", json=body)).status_code == 401
    assert (await runtime.client.post("/api/v1/auth/login", json=body)).status_code == 429


async def test_expired_token(runtime):
    headers = await login(runtime)
    digest = digest_token(headers["Authorization"].split()[1])
    async with runtime.store.sessions() as session:
        await session.execute(update(AuthSession).where(AuthSession.token_digest == digest).values(expires_at=utcnow() - timedelta(seconds=1)))
        await session.commit()
    assert (await runtime.client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_disabled_account(runtime):
    headers = await login(runtime)
    async with runtime.store.sessions() as session:
        await session.execute(update(User).where(User.id == runtime.users["alice"].id).values(enabled=False))
        await session.commit()
    assert (await runtime.client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_no_auth_and_management_permissions(runtime):
    assert (await runtime.client.get("/api/v1/conversations")).status_code == 401
    assert (await runtime.client.get("/api/v1/admin/status", headers=await login(runtime))).status_code == 403
    response = await runtime.client.get("/api/v1/admin/status", headers=await login(runtime, "manager"))
    assert response.status_code == 200 and response.json()["bank_chat_ready"] is True


async def test_cross_user_read_delete_and_admin_no_blanket_access(runtime):
    alice = await login(runtime)
    identifier = await conversation(runtime, alice)
    for name in ["bob", "manager"]:
        headers = await login(runtime, name)
        assert (await runtime.client.get(f"/api/v1/conversations/{identifier}", headers=headers)).status_code == 404
        assert (await runtime.client.delete(f"/api/v1/conversations/{identifier}", headers=headers)).status_code == 404
        assert (await runtime.client.get("/api/v1/conversations", headers=headers)).json()["items"] == []
    assert (await runtime.client.get(f"/api/v1/conversations/{identifier}", headers=alice)).status_code == 200


async def test_pagination_bounds_and_identity_injection(runtime):
    headers = await login(runtime)
    assert (await runtime.client.get("/api/v1/conversations?limit=101", headers=headers)).status_code == 422
    response = await runtime.client.post("/api/v1/conversations", headers=headers, json={"user_id": runtime.users["bob"].id})
    assert response.status_code == 422


async def test_secret_validation_body_limits_and_logs(runtime, caplog):
    caplog.set_level(logging.INFO, logger="bank.requests")
    secret = "must-not-be-in-validation-response"
    response = await runtime.client.post("/api/v1/auth/login", json={"username": "bad name", "password": secret})
    assert response.status_code == 422 and secret not in response.text
    response = await runtime.client.post("/api/v1/auth/login", json={"username": "abc", "password": "x" * 70000})
    assert response.status_code == 413
    headers = await login(runtime)
    await runtime.client.get("/api/v1/auth/me", headers=headers)
    assert secret not in caplog.text and headers["Authorization"].split()[1] not in caplog.text


async def test_only_hashed_password_and_token_stored(runtime):
    headers = await login(runtime)
    async with runtime.store.sessions() as session:
        stored = await session.get(AuthSession, digest_token(headers["Authorization"].split()[1]))
        user = await session.get(User, runtime.users["alice"].id)
        assert stored and stored.token_digest != headers["Authorization"].split()[1]
        assert user.password_hash.startswith("$argon2id$")
        assert hasher.verify(user.password_hash, "Synthetic-test-password-123")


async def test_messages_context_rebuild_and_delete_cascade(runtime):
    headers = await login(runtime)
    identifier = await conversation(runtime, headers)
    user_id = runtime.users["alice"].id
    await append_message(runtime.store, runtime.cache, user_id, identifier, "user", "合成咨询", str(uuid4()))
    await append_message(runtime.store, runtime.cache, user_id, identifier, "assistant", "合成回复", str(uuid4()))
    context = await recent_context(runtime.store, runtime.cache, user_id, identifier)
    assert len(context) == 2
    await runtime.cache.client.delete(runtime.cache.context_key(user_id, identifier))
    assert await recent_context(runtime.store, runtime.cache, user_id, identifier) == context
    result = await runtime.client.get(f"/api/v1/conversations/{identifier}/messages", headers=headers)
    assert [item["sequence"] for item in result.json()["items"]] == [1, 2]
    assert (await runtime.client.post(f"/api/v1/conversations/{identifier}/messages", headers=headers, json={"role": "assistant"})).status_code == 405
    assert (await runtime.client.delete(f"/api/v1/conversations/{identifier}", headers=headers)).status_code == 204
    assert not await runtime.cache.client.exists(runtime.cache.context_key(user_id, identifier))
    async with runtime.store.sessions() as session:
        assert not await session.get(Conversation, identifier)
        assert await session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == identifier)) == 0
        assert await session.scalar(select(func.count()).select_from(RequestRecord).where(RequestRecord.conversation_id == identifier)) == 0


async def test_same_conversation_conflict_and_exception_recovery(runtime):
    headers = await login(runtime)
    identifier = await conversation(runtime, headers)
    async with runtime.cache.lock(runtime.users["alice"].id, identifier):
        assert (await runtime.client.delete(f"/api/v1/conversations/{identifier}", headers=headers)).status_code == 409
    with pytest.raises(RuntimeError):
        async with runtime.cache.lock(runtime.users["alice"].id, identifier):
            raise RuntimeError("synthetic failure")
    assert (await runtime.client.delete(f"/api/v1/conversations/{identifier}", headers=headers)).status_code == 204


async def test_lock_wrong_holder_not_released_and_expiry(runtime):
    user_id, identifier = runtime.users["alice"].id, str(uuid4())
    key = runtime.cache.key("lock", user_id, identifier)
    async with runtime.cache.lock(user_id, identifier):
        await runtime.cache.client.set(key, "new-holder", ex=10)
    assert await runtime.cache.client.get(key) == "new-holder"
    await runtime.cache.client.pexpire(key, 20)
    await asyncio.sleep(0.05)
    async with runtime.cache.lock(user_id, identifier):
        pass


async def test_cache_context_permission(runtime):
    headers = await login(runtime)
    identifier = await conversation(runtime, headers)
    await recent_context(runtime.store, runtime.cache, runtime.users["alice"].id, identifier)
    with pytest.raises(HTTPException) as error:
        await recent_context(runtime.store, runtime.cache, runtime.users["bob"].id, identifier)
    assert error.value.status_code == 404


async def test_restart_app_preserves_token_and_conversation(runtime):
    headers = await login(runtime)
    identifier = await conversation(runtime, headers)
    async with client_for(create_app(runtime.settings)) as restarted:
        assert (await restarted.get(f"/api/v1/conversations/{identifier}", headers=headers)).status_code == 200


async def test_dependency_health_distinct_from_bank_ready(runtime):
    response = await runtime.client.get("/api/v1/health/dependencies")
    assert response.status_code == 200 and all(response.json()["checks"].values()), response.json()
    ready = await runtime.client.get("/api/v1/health/ready")
    assert ready.status_code == 200 and ready.json()["transaction_execution"] is False


async def test_database_unavailable_no_fake_success(runtime):
    config = runtime.settings.model_copy(update={"database_url": runtime.settings.database_url.__class__("postgresql+asyncpg://none:none@127.0.0.1:1/bank_test")})
    async with client_for(create_app(config)) as client:
        response = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer fake"})
        assert response.status_code == 503 and "none:none" not in response.text
        assert (await client.get("/api/v1/health/dependencies")).status_code == 503


async def test_redis_unavailable_no_token_issued(runtime):
    config = runtime.settings.model_copy(update={"redis_url": runtime.settings.redis_url.__class__("redis://127.0.0.1:1/0")})
    async with client_for(create_app(config)) as client:
        response = await client.post("/api/v1/auth/login", json={"username": runtime.users["alice"].username, "password": "Synthetic-test-password-123"})
        assert response.status_code == 503 and "access_token" not in response.text


async def test_fake_model_contract():
    client = FakeModelClient()
    result = await client.chat(ChatInput(messages=[ModelMessage(role="user", content="测试")]))
    assert result.backend == "fake" and result.input_tokens == result.output_tokens == 0
    assert (await client.embed(["测试"])).backend == "fake"
    await client.aclose()


async def test_streamed_body_limit_without_content_length(runtime):
    async def payload():
        yield b'{"username":"abc","password":"'
        for _ in range(8):
            yield b'x' * 10000
        yield b'"}'
    response = await runtime.client.post("/api/v1/auth/login", content=payload(),
                                         headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_live_port_occupancy_protection():
    import socket
    from scripts.native import free_port
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with pytest.raises(RuntimeError):
            free_port(listener.getsockname()[1])


async def test_request_record_correlates_with_response(runtime):
    headers = await login(runtime)
    response = await runtime.client.post("/api/v1/conversations", headers=headers, json={})
    async with runtime.store.sessions() as session:
        record = await session.scalar(select(RequestRecord).where(RequestRecord.request_id == response.headers["x-request-id"]))
        assert record and record.conversation_id == response.json()["id"]


async def test_context_window_is_bounded(runtime):
    identifier = await conversation(runtime, await login(runtime))
    user_id = runtime.users["alice"].id
    for number in range(14):
        await append_message(runtime.store, runtime.cache, user_id, identifier, "user", f"合成消息{number}", str(uuid4()))
    context = await recent_context(runtime.store, runtime.cache, user_id, identifier)
    assert len(context) == 12 and context[0]["content"] == "合成消息2"
    assert await runtime.cache.client.ttl(runtime.cache.context_key(user_id, identifier)) > 0
