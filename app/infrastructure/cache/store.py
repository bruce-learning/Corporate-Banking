from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import secrets
from fastapi import HTTPException
from redis.asyncio import Redis

RESERVE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if n > tonumber(ARGV[2]) then redis.call('DECR', KEYS[1]); return 0 end
return 1
"""
SUCCESS = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0')
if n > 0 then redis.call('DECR', KEYS[1]) end
return 1
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class Cache:
    def __init__(self, settings):
        self.settings = settings
        self.client = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds)

    def key(self, kind: str, *parts: str) -> str:
        return ":".join([self.settings.cache_prefix, kind, *parts])

    def context_key(self, user_id: str, conversation_id: str) -> str:
        return self.key("context", user_id, conversation_id)

    async def allow_chat(self, user_id: str) -> bool:
        minute = int(datetime.now(timezone.utc).timestamp() // 60)
        key = self.key("rate", "chat", user_id, str(minute))
        value = await self.client.incr(key)
        if value == 1:
            await self.client.expire(key, 120)
        return value <= self.settings.chat_rate_limit_per_minute

    async def reserve_memory_embedding(self, user_id: str) -> bool:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = self.key("budget", "memory_embedding", user_id, day)
        value = await self.client.incr(key)
        if value == 1:
            await self.client.expire(key, 172800)
        if value > self.settings.memory_embedding_daily_limit:
            await self.client.decr(key)
            return False
        return True

    async def reserve_login(self, username: str) -> str:
        key = self.key("login", hashlib.sha256(username.encode()).hexdigest())
        if not await self.client.eval(RESERVE, 1, key, self.settings.login_window_seconds, self.settings.login_limit):
            raise HTTPException(429, "登录尝试过多，请稍后再试")
        return key

    async def login_success(self, key: str):
        await self.client.eval(SUCCESS, 1, key)

    @asynccontextmanager
    async def lock(self, user_id: str, conversation_id: str):
        key = self.key("lock", user_id, conversation_id)
        holder = secrets.token_urlsafe(24)
        if not await self.client.set(key, holder, nx=True, ex=self.settings.lock_ttl_seconds):
            raise HTTPException(409, "该会话正在处理其他请求，请稍后重试")
        try:
            yield
        finally:
            await self.client.eval(RELEASE, 1, key, holder)

    async def close(self):
        await self.client.aclose()
