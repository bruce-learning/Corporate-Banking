import json
from sqlalchemy import select
from app.core.application.conversations import owned
from app.models.entities.storage import Message


async def recent_context(store, cache, user_id: str, conversation_id: str):
    # 与写入/删除共享锁，避免删除后被并发读请求重新写回缓存。
    async with cache.lock(user_id, conversation_id):
        async with store.sessions() as session:
            await owned(session, user_id, conversation_id)
            key = cache.context_key(user_id, conversation_id)
            cached = await cache.client.get(key)
            if cached:
                return json.loads(cached)
            messages = (await session.scalars(select(Message).where(Message.conversation_id == conversation_id)
                        .order_by(Message.sequence.desc()).limit(cache.settings.context_message_limit))).all()
            context = [{"role": item.role, "content": item.content} for item in reversed(messages)]
            await cache.client.set(key, json.dumps(context, ensure_ascii=False), ex=cache.settings.context_ttl_seconds)
            return context
