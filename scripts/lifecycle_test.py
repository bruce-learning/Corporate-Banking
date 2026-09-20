"""真实 TCP、进程重启与依赖故障检查；仅操作项目实例及自建测试用户。"""
import asyncio
import json
import os
import secrets
import socket
import subprocess
import time
from uuid import uuid4

import httpx
from sqlalchemy import delete

import native
from app.config import Settings
from app.core.application.auth import hash_password
from app.core.application.conversations import append_message
from app.core.memory.context import recent_context
from app.infrastructure.cache.store import Cache
from app.infrastructure.database.store import Store
from app.models.entities.storage import User


async def storage_action(settings, action, **values):
    store, cache = Store(settings.database_url.get_secret_value()), Cache(settings)
    try:
        if action == "seed":
            async with store.sessions() as session:
                user = User(username=values["username"], password_hash=await hash_password(values["password"]), role="user")
                session.add(user)
                await session.commit()
                return user.id
        if action == "message":
            return await append_message(store, cache, values["user_id"], values["conversation_id"],
                                        "user", "仅用于持久化验证的合成消息", str(uuid4()))
        if action == "context":
            await cache.client.delete(cache.context_key(values["user_id"], values["conversation_id"]))
            return await recent_context(store, cache, values["user_id"], values["conversation_id"])
        if action == "cleanup":
            async with store.sessions() as session:
                await session.execute(delete(User).where(User.id == values["user_id"]))
                await session.commit()
            keys = [key async for key in cache.client.scan_iter(settings.cache_prefix + ":*")]
            if keys:
                await cache.client.delete(*keys)
    finally:
        await cache.close()
        await store.close()


def main():
    deployed = Settings()
    test_database = deployed.database_url.get_secret_value().rsplit("/", 1)[0] + "/bank_test"
    assert test_database.endswith("/bank_test")
    config = Settings(_env_file=None, database_url=test_database, redis_url=deployed.redis_url,
                      app_env="test", cache_prefix="bank:lifecycle:" + uuid4().hex)
    password = secrets.token_urlsafe(24)
    username = "lifecycle_" + uuid4().hex[:16]
    user_id = asyncio.run(storage_action(config, "seed", username=username, password=password))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environment = os.environ.copy()
    environment.update(DATABASE_URL=test_database, REDIS_URL=config.redis_url.get_secret_value(),
        CACHE_PREFIX=config.cache_prefix, APP_ENV="test", APP_HOST="127.0.0.1", APP_PORT=str(port),
        ENABLE_PAID_MODEL_CALLS="false")
    process = None
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
    logfile = native.VAR / "logs/lifecycle-api.log"
    log_descriptor = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    log = os.fdopen(log_descriptor, "a")
    checks = []

    def start():
        nonlocal process
        process = subprocess.Popen([str(native.ROOT / ".venv/bin/python"), str(native.ROOT / "scripts/run_api.py")],
            cwd=native.ROOT, env=environment, stdout=log, stderr=log)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("临时验证 API 启动失败")
            try:
                if client.get("/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        raise TimeoutError("临时验证 API 未就绪")

    def stop():
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=8)

    try:
        start()
        assert client.get("/api/v1/health/dependencies").status_code == 200
        response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        response = client.post("/api/v1/conversations", json={"title": "生命周期合成测试"}, headers=headers)
        assert response.status_code == 201
        identifier = response.json()["id"]
        asyncio.run(storage_action(config, "message", user_id=user_id, conversation_id=identifier))
        stop(); start()
        assert client.get(f"/api/v1/conversations/{identifier}/messages", headers=headers).json()["items"][0]["sequence"] == 1
        checks.append("real_api_restart_preserves_auth_and_messages")

        native.stop_redis(deployed)
        assert client.get("/api/v1/health/dependencies").status_code == 503
        assert client.post("/api/v1/auth/login", json={"username": username, "password": password}).status_code == 503
        assert client.delete(f"/api/v1/conversations/{identifier}", headers=headers).status_code == 503
        native.start_redis(deployed)
        assert client.get("/api/v1/health/dependencies").status_code == 200
        assert len(asyncio.run(storage_action(config, "context", user_id=user_id, conversation_id=identifier))) == 1
        checks.append("real_redis_outage_refuses_unprotected_login_and_delete_then_recovers")

        native.stop_pg()
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/health/dependencies").status_code == 503
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 503
        assert client.post("/api/v1/conversations", headers=headers, json={}).status_code == 503
        native.start_pg()
        assert client.get("/api/v1/health/dependencies").status_code == 200
        response = client.get(f"/api/v1/conversations/{identifier}/messages", headers=headers)
        assert response.status_code == 200 and len(response.json()["items"]) == 1
        checks.append("real_postgresql_outage_and_restart_preserve_data")
        assert client.delete(f"/api/v1/conversations/{identifier}", headers=headers).status_code == 204
        assert client.get(f"/api/v1/conversations/{identifier}", headers=headers).status_code == 404
        checks.append("real_tcp_delete_completed")
        result = {"status": "passed", "checks": checks, "paid_calls": 0}
        (native.VAR / "run/lifecycle-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        stop()
        native.start_pg()
        native.start_redis(deployed)
        asyncio.run(storage_action(config, "cleanup", user_id=user_id))
        client.close()
        log.close()


if __name__ == "__main__":
    main()
