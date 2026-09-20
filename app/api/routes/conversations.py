import time
from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.api.dependencies import Identity, current_identity, require_cache, require_store
from app.core.application.conversations import owned
from app.models.entities.storage import Conversation, Message, RequestRecord

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class CreateConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="新会话", min_length=1, max_length=120)


def serialize(item):
    return {"id": item.id, "title": item.title, "created_at": item.created_at, "updated_at": item.updated_at}


@router.post("", status_code=201)
async def create(body: CreateConversation, request: Request, identity: Identity = Depends(current_identity)):
    started = time.monotonic()
    async with require_store(request).sessions() as session:
        item = Conversation(user_id=identity.user_id, title=body.title)
        session.add(item)
        await session.flush()
        session.add(RequestRecord(request_id=request.state.request_id, user_id=identity.user_id, conversation_id=item.id,
            action="create_conversation", status="success", duration_ms=int((time.monotonic() - started) * 1000)))
        await session.commit()
        return serialize(item)


@router.get("")
async def listing(request: Request, offset: int = Query(default=0, ge=0, le=100000),
                  limit: int = Query(default=20, ge=1, le=100), identity: Identity = Depends(current_identity)):
    async with require_store(request).sessions() as session:
        items = (await session.scalars(select(Conversation).where(Conversation.user_id == identity.user_id)
                 .order_by(Conversation.updated_at.desc(), Conversation.id).offset(offset).limit(limit))).all()
        return {"items": [serialize(item) for item in items], "offset": offset, "limit": limit}


@router.get("/{conversation_id}")
async def detail(conversation_id: UUID, request: Request, identity: Identity = Depends(current_identity)):
    async with require_store(request).sessions() as session:
        return serialize(await owned(session, identity.user_id, str(conversation_id)))


@router.get("/{conversation_id}/messages")
async def messages(conversation_id: UUID, request: Request, after_sequence: int = Query(default=0, ge=0),
                   limit: int = Query(default=20, ge=1, le=100), identity: Identity = Depends(current_identity)):
    async with require_store(request).sessions() as session:
        await owned(session, identity.user_id, str(conversation_id))
        items = (await session.scalars(select(Message).where(Message.conversation_id == str(conversation_id),
                 Message.sequence > after_sequence).order_by(Message.sequence).limit(limit))).all()
        return {"items": [{"id": item.id, "sequence": item.sequence, "role": item.role,
                           "content": item.content, "state": item.state} for item in items]}


@router.delete("/{conversation_id}", status_code=204)
async def remove(conversation_id: UUID, request: Request, identity: Identity = Depends(current_identity)):
    cache = require_cache(request)
    async with cache.lock(identity.user_id, str(conversation_id)):
        async with require_store(request).sessions() as session:
            item = await owned(session, identity.user_id, str(conversation_id), for_update=True)
            memory = getattr(request.app.state, "memory_service", None)
            if memory:
                await memory.delete_conversation(identity.user_id, str(conversation_id))
            else:
                await cache.client.delete(cache.context_key(identity.user_id, str(conversation_id)))
            await session.delete(item)
            await session.commit()
    return Response(status_code=204)
