from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.api.dependencies import Identity, current_identity, require_cache, require_store
from app.models.entities.storage import ChatTrace


router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: UUID
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def non_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


def trace_payload(trace: ChatTrace) -> dict:
    return {
        "request_id": trace.request_id, "conversation_id": trace.conversation_id,
        "intent": trace.intent, "confidence": trace.confidence, "domains": trace.domains,
        "risk_flags": trace.risk_flags, "strategy_scores": trace.strategy_scores,
        "strategy_weights": trace.strategy_weights, "tool_calls": trace.tool_calls,
        "evidence_ids": trace.evidence_ids, "conflicts": trace.conflicts,
        "degraded": trace.degraded, "degradation_reasons": trace.degradation_reasons,
        "generation_mode": trace.generation_mode, "model": trace.model,
        "original_query": trace.original_query, "rewritten_query": trace.rewritten_query,
        "execution_mode": trace.execution_mode, "plan_steps": trace.plan_steps,
        "react_steps": trace.react_steps,
        "citation_valid": trace.citation_valid, "citation_coverage": trace.citation_coverage,
        "skills_used": trace.skills_used, "memory_context": trace.memory_context,
        "status": trace.status, "latency_ms": trace.latency_ms, "created_at": trace.created_at,
    }


@router.post("")
async def chat(body: ChatRequest, request: Request, identity: Identity = Depends(current_identity)):
    if request.app.state.chat_service is None:
        require_store(request)
        require_cache(request)
        raise RuntimeError("聊天服务未初始化")
    if not await require_cache(request).allow_chat(identity.user_id):
        from fastapi import HTTPException
        raise HTTPException(429, "聊天请求过于频繁，请稍后再试")
    result = await request.app.state.chat_service.handle(
        identity.user_id, str(body.conversation_id), body.message.strip(), request.state.request_id
    )
    return {
        "request_id": request.state.request_id,
        "conversation_id": str(body.conversation_id),
        "answer": result.answer,
        "intent": result.intent, "confidence": result.confidence,
        "domains": result.domains, "agents": result.agents,
        "risk_flags": result.risk_flags,
        "evidence": [item.model_dump(mode="json") for item in result.evidence],
        "tool_calls": result.tool_calls,
        "conflicts": result.conflicts,
        "degraded": result.degraded,
        "degradation_reasons": result.degradation_reasons,
        "generation_mode": result.generation_mode,
        "model": result.model,
        "original_query": result.original_query,
        "rewritten_query": result.rewritten_query,
        "execution_mode": result.execution_mode,
        "plan_steps": result.plan_steps,
        "react_steps": result.react_steps,
        "citation_valid": result.citation_valid,
        "citation_coverage": result.citation_coverage,
        "skills_used": result.skills_used,
        "memory_context": result.memory_context,
    }


@router.get("/traces/{request_id}")
async def trace(request_id: UUID, request: Request, identity: Identity = Depends(current_identity)):
    async with require_store(request).sessions() as session:
        item = await session.scalar(select(ChatTrace).where(
            ChatTrace.request_id == str(request_id), ChatTrace.user_id == identity.user_id
        ))
        if item is None:
            from fastapi import HTTPException
            raise HTTPException(404, "问答追踪不存在或不可访问")
        return trace_payload(item)
