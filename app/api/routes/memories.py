from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import Identity, current_identity


router = APIRouter(prefix="/api/v1/memories", tags=["memories"])


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=500)
    status: str = Field(default="active", pattern=r"^(active|disabled)$")


def service(request: Request):
    value = getattr(request.app.state, "memory_service", None)
    if value is None:
        raise HTTPException(503, "记忆服务尚未初始化")
    return value


def serialize(item):
    return {"id": item.id, "content": item.document, "metadata": item.metadata}


@router.get("/summaries")
async def summaries(request: Request, limit: int = Query(default=50, ge=1, le=100),
                    offset: int = Query(default=0, ge=0), identity: Identity = Depends(current_identity)):
    rows = await service(request).list_memories("summary", identity.user_id, limit, offset)
    return {"items": [serialize(item) for item in rows], "limit": limit, "offset": offset}


@router.get("/profiles")
async def profiles(request: Request, limit: int = Query(default=50, ge=1, le=100),
                   offset: int = Query(default=0, ge=0), identity: Identity = Depends(current_identity)):
    rows = await service(request).list_memories("profile", identity.user_id, limit, offset)
    return {"items": [serialize(item) for item in rows], "limit": limit, "offset": offset}


@router.patch("/profiles/{memory_id}")
async def update_profile(memory_id: str, body: ProfileUpdate, request: Request,
                         identity: Identity = Depends(current_identity)):
    try:
        item = await service(request).update_profile(identity.user_id, memory_id, body.content, body.status)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return serialize(item)


@router.delete("/profiles/{memory_id}", status_code=204)
async def delete_profile(memory_id: str, request: Request, identity: Identity = Depends(current_identity)):
    if not await service(request).delete_memory_id("profile", identity.user_id, memory_id):
        raise HTTPException(404, "用户画像不存在")
    return Response(status_code=204)


@router.delete("/summaries/{memory_id}", status_code=204)
async def delete_summary(memory_id: str, request: Request, identity: Identity = Depends(current_identity)):
    if not await service(request).delete_memory_id("summary", identity.user_id, memory_id):
        raise HTTPException(404, "历史摘要不存在")
    return Response(status_code=204)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation_memories(conversation_id: str, request: Request,
                                       identity: Identity = Depends(current_identity)):
    await service(request).delete_conversation(identity.user_id, conversation_id)
    return Response(status_code=204)


@router.delete("/all", status_code=204)
async def delete_all(request: Request, identity: Identity = Depends(current_identity)):
    await service(request).delete_all(identity.user_id)
    return Response(status_code=204)
