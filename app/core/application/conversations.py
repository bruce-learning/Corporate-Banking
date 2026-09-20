import time
from fastapi import HTTPException
from sqlalchemy import select, func
from app.models.entities.storage import Conversation, Message, RequestRecord, utcnow


async def owned(session, user_id: str, conversation_id: str, for_update: bool = False):
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    item = await session.scalar(query)
    if not item:
        raise HTTPException(404, "会话不存在或不可访问")
    return item


async def append_message(store, cache, user_id: str, conversation_id: str, role: str, content: str, request_id: str):
    """内部写入契约，不暴露允许用户伪造助手消息的接口。"""
    if role not in {"user", "assistant"} or not content or len(content) > 8000:
        raise ValueError("消息角色或长度不合法")
    started = time.monotonic()
    async with cache.lock(user_id, conversation_id):
        async with store.sessions() as session:
            conversation = await owned(session, user_id, conversation_id, for_update=True)
            maximum = await session.scalar(select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id))
            message = Message(conversation_id=conversation_id, sequence=(maximum or 0) + 1,
                              role=role, content=content, request_id=request_id)
            session.add(message)
            conversation.updated_at = utcnow()
            session.add(RequestRecord(request_id=request_id, user_id=user_id, conversation_id=conversation_id,
                action="append_message", status="success", duration_ms=int((time.monotonic() - started) * 1000)))
            await cache.client.delete(cache.context_key(user_id, conversation_id))
            await session.commit()
            return message.id
