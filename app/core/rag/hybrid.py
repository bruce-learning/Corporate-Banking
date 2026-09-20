from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import re
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import or_, select

from app.infrastructure.llm.aliyun import ModelServiceError, PaidCallsDisabled
from app.models.entities.storage import DocumentVersion, KnowledgeChunk


TOPIC_HINTS = {
    "overview": "用途 介绍 是什么 主要 做什么",
    "audience": "适合 哪些企业 哪些公司 适用对象 客户",
    "features": "功能 能力 可以做什么 提供",
    "prerequisites": "条件 前提 要求",
    "limitations": "限制 不能 边界",
    "application": "申请 办理 开始",
    "materials": "材料 资料 准备",
    "steps": "步骤 流程 完整流程",
    "duration": "多久 时间 时效",
    "change_cancel": "变更 取消 修改 撤销",
    "fees": "收费 费用 多少钱 价格",
    "effective": "生效 有效期 版本",
    "approval": "审批 审核 批准",
    "security": "安全 敏感 验证码 密码",
    "exceptions": "异常 失败 特殊情况",
}


def tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    latin = re.findall(r"[a-z0-9]+", lowered)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    chars = list(chinese)
    bigrams = [chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))]
    return latin + chars + bigrams


def bm25(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    query_terms = tokens(query)
    rows = [tokens(text) for text in documents]
    if not query_terms or not rows:
        return [0.0 for _ in rows]
    average = sum(len(row) for row in rows) / len(rows) or 1.0
    frequencies = [Counter(row) for row in rows]
    document_frequency = Counter()
    for row in frequencies:
        document_frequency.update(row.keys())
    scores = []
    for row, counts in zip(rows, frequencies):
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            inverse = math.log(1.0 + (len(rows) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + k1 * (1.0 - b + b * len(row) / average)
            score += inverse * frequency * (k1 + 1.0) / denominator
        scores.append(score)
    return scores


class Evidence(BaseModel):
    id: str
    document_id: str
    title: str
    domain: Literal["product", "process", "policy"]
    product: str | None
    content: str
    source: str
    effective_at: datetime
    expires_at: datetime | None
    score: float
    lexical_rank: int
    semantic_rank: int | None
    synthetic: bool
    metadata: dict


class SearchResponse(BaseModel):
    query: str
    mode: Literal["hybrid", "keyword"]
    degraded: bool
    degradation_reason: str | None = None
    vector_backend: Literal["chromadb", "none"] = "none"
    items: list[Evidence]


class HybridSearchService:
    def __init__(self, store, model_client=None, vector_store=None):
        self.store = store
        self.model_client = model_client
        self.vector_store = vector_store

    async def candidates(self, domain: str | None = None, as_of: datetime | None = None) -> list[tuple[KnowledgeChunk, DocumentVersion]]:
        moment = as_of or datetime.now(timezone.utc)
        statement = (
            select(KnowledgeChunk, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == KnowledgeChunk.document_id)
            .where(
                KnowledgeChunk.active.is_(True),
                DocumentVersion.status == "published",
                DocumentVersion.audience == "demo",
                DocumentVersion.effective_at <= moment,
                or_(DocumentVersion.expires_at.is_(None), DocumentVersion.expires_at > moment),
            )
            .order_by(KnowledgeChunk.id)
        )
        if domain:
            statement = statement.where(KnowledgeChunk.domain == domain)
        async with self.store.sessions() as session:
            return list((await session.execute(statement)).all())

    async def search(
        self,
        query: str,
        domain: str | None = None,
        top_k: int = 5,
        as_of: datetime | None = None,
        query_vector: list[float] | None = None,
    ) -> SearchResponse:
        query = query.strip()
        if not query or len(query) > 500:
            raise ValueError("检索问题为空或过长")
        if domain not in {None, "product", "process", "policy"}:
            raise ValueError("检索领域不合法")
        top_k = max(1, min(int(top_k), 10))
        moment = as_of or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        rows = await self.candidates(domain=domain, as_of=moment)
        if not rows:
            return SearchResponse(query=query, mode="keyword", degraded=True, degradation_reason="no_published_knowledge", items=[])

        chunks = [row[0] for row in rows]
        documents = []
        for item in chunks:
            metadata = item.metadata_json or {}
            topic = str(metadata.get("topic") or "")
            keywords = metadata.get("keywords") or []
            documents.append(
                f"{item.title} {item.product or ''} {item.content} "
                f"{' '.join(map(str, keywords))} {TOPIC_HINTS.get(topic, '')}"
            )
        lexical_scores = bm25(query, documents)
        lexical_order = sorted(range(len(chunks)), key=lambda index: (lexical_scores[index], chunks[index].id), reverse=True)
        lexical_rank = {index: rank for rank, index in enumerate(lexical_order, 1)}

        reason = None
        if query_vector is None and self.model_client is not None:
            try:
                query_vector = (await self.model_client.embed([query])).vectors[0]
            except PaidCallsDisabled:
                reason = "paid_calls_disabled"
            except ModelServiceError as error:
                reason = error.category
            except Exception:
                reason = "embedding_unavailable"
        elif query_vector is None:
            reason = "embedding_client_unavailable"

        semantic_order: list[int] = []
        if query_vector is not None and self.vector_store is not None:
            try:
                hits = await self.vector_store.query(
                    query_vector, domain=domain, as_of=moment,
                    n_results=max(50, top_k * 10),
                )
                index_by_id = {item.id: index for index, item in enumerate(chunks)}
                semantic_order = [index_by_id[hit.id] for hit in hits if hit.id in index_by_id]
            except Exception:
                reason = "chromadb_unavailable"
        elif query_vector is not None:
            reason = "chromadb_unavailable"
        semantic_rank = {index: rank for rank, index in enumerate(semantic_order, 1)}
        hybrid = bool(semantic_order)

        ranked = []
        for index, item in enumerate(chunks):
            # The structured topic/keyword signal is intentionally weighted
            # above semantic similarity so close passages from the same
            # product are not shuffled solely by embedding proximity.
            score = 1.5 / (60 + lexical_rank[index])
            if hybrid and index in semantic_rank:
                score += 1.0 / (60 + semantic_rank[index])
            ranked.append((score, lexical_scores[index], item.id, index))
        ranked.sort(reverse=True)

        items = []
        for score, _, _, index in ranked[:top_k]:
            chunk, document = rows[index]
            maximum = 2.5 / 61 if hybrid else 1.5 / 61
            items.append(Evidence(
                id=chunk.id,
                document_id=document.id,
                title=chunk.title,
                domain=chunk.domain,
                product=chunk.product,
                content=chunk.content,
                source=chunk.source,
                effective_at=document.effective_at,
                expires_at=document.expires_at,
                score=round(score / maximum, 6),
                lexical_rank=lexical_rank[index],
                semantic_rank=semantic_rank.get(index),
                synthetic=document.synthetic,
                metadata=chunk.metadata_json,
            ))
        return SearchResponse(
            query=query,
            mode="hybrid" if hybrid else "keyword",
            degraded=not hybrid,
            degradation_reason=None if hybrid else (reason or "document_embeddings_missing"),
            vector_backend="chromadb" if hybrid else "none",
            items=items,
        )
