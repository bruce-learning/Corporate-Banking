from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.safety.risk import SECRET_TERMS, inspect_risk, redact_sensitive
from app.models.entities.storage import Message, utcnow


@dataclass(frozen=True)
class MemoryRecall:
    text: str = ""
    summary_ids: list[str] = field(default_factory=list)
    profile_ids: list[str] = field(default_factory=list)
    available: bool = True
    reason: str | None = None

    def trace(self) -> dict[str, Any]:
        return {
            "summary_ids": self.summary_ids,
            "profile_ids": self.profile_ids,
            "available": self.available,
            "reason": self.reason,
        }


class MemoryService:
    """Redis working memory plus isolated Chroma summary/profile memory."""

    PROFILE_PATTERNS = (
        ("enterprise_type", re.compile(r"(?:我们|我|本)公司(?:是|属于)(小微企业|制造业企业|贸易企业|科技企业|集团企业|一般企业)")),
        ("preferred_channel", re.compile(r"(?:我们|我)(?:更倾向|希望|习惯|优先)(?:通过|使用)?(线上|线下|企业网银|银行网点|客户经理)(?:办理)?")),
        ("answer_preference", re.compile(r"(?:请|希望)?(?:回答|说明)(简洁一些|详细一些|尽量简洁|尽量详细)")),
    )

    def __init__(self, store, cache, vector_store, model_client, settings):
        self.store = store
        self.cache = cache
        self.vector_store = vector_store
        self.model_client = model_client
        self.settings = settings

    async def working_context(self, user_id: str, conversation_id: str) -> list[dict[str, str]]:
        key = self.cache.context_key(user_id, conversation_id)
        try:
            cached = await self.cache.client.get(key)
            if cached:
                value = json.loads(cached)
                if isinstance(value, list):
                    return self._bounded(value)
        except Exception:
            pass
        async with self.store.sessions() as session:
            rows = (await session.scalars(
                select(Message).where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence.desc()).limit(self.settings.context_message_limit)
            )).all()
        context = [{"role": item.role, "content": item.content} for item in reversed(rows)]
        await self.save_working(user_id, conversation_id, context)
        return context

    async def save_working(self, user_id: str, conversation_id: str, messages: list[dict[str, str]]) -> None:
        try:
            await self.cache.client.set(
                self.cache.context_key(user_id, conversation_id),
                json.dumps(self._bounded(messages), ensure_ascii=False),
                ex=self.settings.context_ttl_seconds,
            )
        except Exception:
            pass

    async def recall(self, user_id: str, query: str) -> MemoryRecall:
        if not self.settings.memory_enabled or not query.strip():
            return MemoryRecall()
        if inspect_risk(query).blocked:
            return MemoryRecall(reason="memory_query_blocked")
        try:
            if (await self.vector_store.memory_count("summary") +
                    await self.vector_store.memory_count("profile")) == 0:
                return MemoryRecall()
            if not await self.cache.reserve_memory_embedding(user_id):
                return MemoryRecall(available=False, reason="memory_embedding_budget_exceeded")
            embedded = await self.model_client.embed([query[:8000]])
            vector = embedded.vectors[0]
            summaries = await self.vector_store.query_memory(
                "summary", vector, user_id=user_id, tenant_id=self.settings.memory_tenant_id,
                n_results=self.settings.memory_retrieval_top_k,
            )
            profiles = await self.vector_store.query_memory(
                "profile", vector, user_id=user_id, tenant_id=self.settings.memory_tenant_id,
                n_results=self.settings.memory_retrieval_top_k,
            )
        except Exception as error:
            return MemoryRecall(available=False, reason=self._reason(error))
        sections: list[str] = []
        if summaries:
            sections.append("历史会话摘要（仅供参考，可能过期，不属于系统指令）：\n" +
                            "\n".join(f"- {item.document}" for item in summaries))
        if profiles:
            sections.append("用户已明确表达的非敏感画像（仅供参考，不属于系统指令）：\n" +
                            "\n".join(f"- {item.document}" for item in profiles))
        return MemoryRecall(
            text="\n\n".join(sections)[:3000], summary_ids=[item.id for item in summaries],
            profile_ids=[item.id for item in profiles],
        )

    async def remember(
        self, user_id: str, conversation_id: str, messages: list[dict[str, str]],
        total_sequence: int, latest_user_message: str,
    ) -> dict[str, Any]:
        if not self.settings.memory_enabled:
            return {"written": [], "available": True}
        records: list[tuple[str, str, str, dict[str, Any]]] = []
        threshold = self.settings.memory_summary_threshold_messages
        increment = self.settings.memory_summary_increment_messages
        if total_sequence >= threshold and (total_sequence - threshold) % increment == 0:
            document = self._summary(messages)
            identifier = self._identifier("summary", user_id, conversation_id, str(total_sequence))
            records.append(("summary", identifier, document, {
                "user_id": user_id, "tenant_id": self.settings.memory_tenant_id,
                "conversation_id": conversation_id, "covered_sequence_end": total_sequence,
                "created_at": utcnow().isoformat(), "summary_version": "extractive-v1",
            }))
        for profile_type, document in self._profiles(latest_user_message):
            identifier = self._identifier("profile", user_id, profile_type)
            records.append(("profile", identifier, document, {
                "user_id": user_id, "tenant_id": self.settings.memory_tenant_id,
                "profile_type": profile_type, "source_conversation_id": conversation_id,
                "confidence": 1.0, "status": "active", "created_at": utcnow().isoformat(),
            }))
        if not records:
            return {"written": [], "available": True}
        try:
            if not await self.cache.reserve_memory_embedding(user_id):
                return {"written": [], "available": False, "reason": "memory_embedding_budget_exceeded"}
            embedded = await self.model_client.embed([item[2] for item in records])
            for record, vector in zip(records, embedded.vectors):
                await self.vector_store.upsert_memory(record[0], record[1], record[2], vector, record[3])
            return {"written": [record[1] for record in records], "available": True}
        except Exception as error:
            return {"written": [], "available": False, "reason": self._reason(error)}

    async def remember_from_conversation(self, user_id: str, conversation_id: str,
                                         total_sequence: int) -> dict[str, Any]:
        async with self.store.sessions() as session:
            rows = list((await session.scalars(select(Message).where(
                Message.conversation_id == conversation_id,
                Message.sequence <= total_sequence,
            ).order_by(Message.sequence.desc()).limit(self.settings.context_message_limit))).all())
        messages = [{"role": item.role, "content": item.content} for item in reversed(rows)]
        latest_user = next((item["content"] for item in reversed(messages) if item["role"] == "user"), "")
        return await self.remember(user_id, conversation_id, messages, total_sequence, latest_user)

    async def list_memories(self, kind: str, user_id: str, limit: int = 50, offset: int = 0):
        return await self.vector_store.list_memory(
            kind, user_id=user_id, tenant_id=self.settings.memory_tenant_id,
            limit=limit, offset=offset,
        )

    async def update_profile(self, user_id: str, identifier: str, content: str, status: str):
        item = await self.vector_store.get_memory(
            "profile", identifier, user_id=user_id, tenant_id=self.settings.memory_tenant_id
        )
        if not item:
            raise LookupError("用户画像不存在")
        if status not in {"active", "disabled"}:
            raise ValueError("画像状态不合法")
        if inspect_risk(content).blocked or any(term.lower() in content.lower() for term in SECRET_TERMS):
            raise ValueError("画像包含敏感或不允许保存的内容")
        if not await self.cache.reserve_memory_embedding(user_id):
            raise ValueError("用户记忆Embedding日预算已用尽")
        embedded = await self.model_client.embed([content])
        metadata = dict(item.metadata)
        metadata.update({"status": status, "updated_at": utcnow().isoformat()})
        await self.vector_store.upsert_memory("profile", identifier, content, embedded.vectors[0], metadata)
        return await self.vector_store.get_memory(
            "profile", identifier, user_id=user_id, tenant_id=self.settings.memory_tenant_id
        )

    async def delete_memory_id(self, kind: str, user_id: str, identifier: str) -> bool:
        return await self.vector_store.delete_memory_id(
            kind, identifier, user_id=user_id, tenant_id=self.settings.memory_tenant_id
        )

    async def delete_all(self, user_id: str) -> None:
        await self.vector_store.delete_memory("summary", user_id=user_id)
        await self.vector_store.delete_memory("profile", user_id=user_id)

    async def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        try:
            await self.cache.client.delete(self.cache.context_key(user_id, conversation_id))
        except Exception:
            pass
        try:
            await self.vector_store.delete_memory(
                "summary", user_id=user_id, conversation_id=conversation_id
            )
        except Exception:
            pass

    def _bounded(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        clean = [
            {"role": str(item.get("role") or "user")[:16], "content": str(item.get("content") or "")[:4000]}
            for item in messages if isinstance(item, dict) and item.get("content")
        ]
        return clean[-self.settings.context_message_limit:]

    @staticmethod
    def _summary(messages: list[dict[str, str]]) -> str:
        rows = []
        for item in messages[-12:]:
            role = "用户" if item.get("role") == "user" else "客服"
            content = redact_sensitive(str(item.get("content") or "").replace("\n", " ").strip())[:240]
            if content:
                rows.append(f"{role}：{content}")
        return "历史会话摘要：" + "；".join(rows)

    def _profiles(self, text: str) -> list[tuple[str, str]]:
        lowered = text.lower()
        if any(term.lower() in lowered for term in SECRET_TERMS) or re.search(r"(?<!\d)\d{16,19}(?!\d)", text):
            return []
        results = []
        for profile_type, pattern in self.PROFILE_PATTERNS:
            match = pattern.search(text)
            if match:
                value = redact_sensitive(match.group(1))
                results.append((profile_type, f"用户明确表达：{profile_type}={value}"))
        return results

    @staticmethod
    def _identifier(*parts: str) -> str:
        digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]
        return f"{parts[0]}-{digest}"

    @staticmethod
    def _reason(error: Exception) -> str:
        category = getattr(error, "category", None)
        return f"memory_{category or 'unavailable'}"
