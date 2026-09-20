from __future__ import annotations

import time
from sqlalchemy import func, select

from app.core.application.conversations import owned
from app.core.memory.service import MemoryRecall
from app.core.safety.risk import inspect_risk
from app.models.entities.storage import ChatTrace, Message, ModelUsageRecord, RequestRecord, utcnow


class ChatService:
    def __init__(self, store, cache, orchestrator, memory_service=None, job_queue=None):
        self.store = store
        self.cache = cache
        self.orchestrator = orchestrator
        self.memory_service = memory_service
        self.job_queue = job_queue

    async def handle(self, user_id: str, conversation_id: str, message: str, request_id: str):
        started = time.monotonic()
        async with self.cache.lock(user_id, conversation_id):
            async with self.store.sessions() as session:
                await owned(session, user_id, conversation_id)
            if self.memory_service:
                context = await self.memory_service.working_context(user_id, conversation_id)
                risk = inspect_risk(message)
                # Blocked credentials and prompt-injection text must not be sent to the embedding API.
                recall = (MemoryRecall() if risk.blocked else
                          await self.memory_service.recall(user_id, risk.safe_message))
                metrics = getattr(self.orchestrator, "metrics", None)
                if metrics:
                    metrics.increment("bank_memory_recall_total", available=str(recall.available).lower())
                    if recall.summary_ids:
                        metrics.increment("bank_memory_recall_hits_total", len(recall.summary_ids), kind="summary")
                    if recall.profile_ids:
                        metrics.increment("bank_memory_recall_hits_total", len(recall.profile_ids), kind="profile")
                result = await self.orchestrator.run(
                    message, context, recall.text, recall.trace()
                )
            else:
                async with self.store.sessions() as session:
                    rows = (await session.scalars(
                        select(Message).where(Message.conversation_id == conversation_id)
                        .order_by(Message.sequence.desc()).limit(self.cache.settings.context_message_limit)
                    )).all()
                    context = [{"role": item.role, "content": item.content} for item in reversed(rows)]
                result = await self.orchestrator.run(message, context)
            async with self.store.sessions() as session:
                conversation = await owned(session, user_id, conversation_id, for_update=True)
                maximum = await session.scalar(select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id))
                sequence = maximum or 0
                session.add_all([
                    Message(conversation_id=conversation_id, sequence=sequence + 1, role="user",
                            content=result.stored_user_message, request_id=request_id),
                    Message(conversation_id=conversation_id, sequence=sequence + 2, role="assistant",
                            content=result.answer, request_id=request_id),
                ])
                trace = ChatTrace(
                    request_id=request_id, user_id=user_id, conversation_id=conversation_id,
                    intent=result.intent, confidence=result.confidence, domains=result.domains,
                    risk_flags=result.risk_flags, strategy_scores=result.strategy_scores,
                    strategy_weights=result.strategy_weights, tool_calls=result.tool_calls,
                    evidence_ids=[item.id for item in result.evidence], conflicts=result.conflicts,
                    degraded=result.degraded, degradation_reasons=result.degradation_reasons,
                    generation_mode=result.generation_mode, model=result.model, status="success",
                    latency_ms=result.latency_ms,
                    original_query=result.original_query, rewritten_query=result.rewritten_query,
                    execution_mode=result.execution_mode, plan_steps=result.plan_steps,
                    citation_valid=result.citation_valid, citation_coverage=result.citation_coverage,
                    skills_used=result.skills_used, memory_context=result.memory_context,
                )
                session.add(trace)
                for usage in result.model_usage:
                    session.add(ModelUsageRecord(
                        request_id=request_id, user_id=user_id,
                        operation=str(usage.get("operation") or "unknown")[:40],
                        model=str(usage.get("model") or "unknown")[:80],
                        input_tokens=max(0, int(usage.get("input_tokens") or 0)),
                        output_tokens=max(0, int(usage.get("output_tokens") or 0)),
                        success=True,
                    ))
                    metrics = getattr(self.orchestrator, "metrics", None)
                    if metrics:
                        metrics.increment("bank_model_tokens_total", int(usage.get("input_tokens") or 0),
                                          model=str(usage.get("model") or "unknown"), token_type="input")
                        metrics.increment("bank_model_tokens_total", int(usage.get("output_tokens") or 0),
                                          model=str(usage.get("model") or "unknown"), token_type="output")
                conversation.updated_at = utcnow()
                session.add(RequestRecord(
                    request_id=request_id, user_id=user_id, conversation_id=conversation_id,
                    action="bank_chat", status="success",
                    duration_ms=int((time.monotonic() - started) * 1000),
                ))
                await session.commit()
            updated_context = context + [
                {"role": "user", "content": result.stored_user_message},
                {"role": "assistant", "content": result.answer},
            ]
            if self.memory_service:
                await self.memory_service.save_working(user_id, conversation_id, updated_context)
                if self.job_queue:
                    try:
                        job = await self.job_queue.enqueue("memory_sync", {
                            "user_id": user_id, "conversation_id": conversation_id,
                            "total_sequence": sequence + 2,
                        }, user_id=user_id)
                        result.memory_context["write"] = {"status": "queued", "job_id": job.id}
                    except Exception:
                        result.memory_context["write"] = {"status": "unavailable", "reason": "queue_unavailable"}
                else:
                    result.memory_context["write"] = {"status": "disabled"}
                async with self.store.sessions() as session:
                    stored_trace = await session.scalar(select(ChatTrace).where(ChatTrace.request_id == request_id))
                    if stored_trace:
                        stored_trace.memory_context = result.memory_context
                        await session.commit()
            else:
                await self.cache.client.delete(self.cache.context_key(user_id, conversation_id))
            return result
