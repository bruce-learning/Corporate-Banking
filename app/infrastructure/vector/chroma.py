from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.config import Settings
from app.models.entities.storage import DocumentVersion, KnowledgeChunk


FAR_FUTURE_TIMESTAMP = 253402300799.0


@dataclass(frozen=True)
class ChromaHit:
    id: str
    distance: float


@dataclass(frozen=True)
class MemoryHit:
    id: str
    document: str
    metadata: dict[str, Any]
    distance: float


class ChromaVectorStore:
    """ChromaDB adapter using caller-supplied Alibaba Cloud embeddings."""

    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.chroma_mode == "http":
            self.client = chromadb.HttpClient(
                host=settings.chroma_host,
                port=settings.chroma_port,
                ssl=False,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        else:
            settings.chroma_path.mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(
                path=str(settings.chroma_path),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        self.collection = self.client.get_or_create_collection(
            name=settings.chroma_collection,
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
            metadata={
                "description": "EchoMind synthetic corporate banking knowledge",
                "embedding_model": settings.dashscope_embedding_model,
                "embedding_dimension": settings.dashscope_embedding_dimension,
            },
        )
        self.summary_collection = self.client.get_or_create_collection(
            name=settings.memory_summary_collection,
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
            metadata={"description": "Per-user historical conversation summaries"},
        )
        self.profile_collection = self.client.get_or_create_collection(
            name=settings.memory_profile_collection,
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
            metadata={"description": "Explicit, non-sensitive user profile memories"},
        )

    @staticmethod
    def metadata(chunk: KnowledgeChunk, document: DocumentVersion, model: str) -> dict[str, Any]:
        return {
            "document_id": document.id,
            "domain": chunk.domain,
            "product": chunk.product or "",
            "status": document.status,
            "audience": document.audience,
            "active": bool(chunk.active),
            "synthetic": bool(document.synthetic),
            "effective_ts": document.effective_at.timestamp(),
            "expires_ts": document.expires_at.timestamp() if document.expires_at else FAR_FUTURE_TIMESTAMP,
            "embedding_model": model,
        }

    async def upsert(
        self,
        rows: list[tuple[KnowledgeChunk, DocumentVersion]],
        vectors: list[list[float]],
        model: str,
    ) -> None:
        if not rows or len(rows) != len(vectors):
            raise ValueError("Chroma 写入记录与向量数量不一致")
        ids = [chunk.id for chunk, _ in rows]
        documents = [chunk.content for chunk, _ in rows]
        metadatas = [self.metadata(chunk, document, model) for chunk, document in rows]
        await asyncio.to_thread(
            self.collection.upsert,
            ids=ids,
            embeddings=vectors,
            documents=documents,
            metadatas=metadatas,
        )

    async def query(
        self,
        query_vector: list[float],
        *,
        domain: str | None,
        as_of: datetime,
        n_results: int,
    ) -> list[ChromaHit]:
        count = await self.count()
        if count == 0:
            return []
        moment = as_of.astimezone(timezone.utc).timestamp()
        conditions: list[dict[str, Any]] = [
            {"status": {"$eq": "published"}},
            {"audience": {"$eq": "demo"}},
            {"active": {"$eq": True}},
            {"effective_ts": {"$lte": moment}},
            {"expires_ts": {"$gt": moment}},
        ]
        if domain:
            conditions.append({"domain": {"$eq": domain}})
        result = await asyncio.to_thread(
            self.collection.query,
            query_embeddings=[query_vector],
            n_results=min(max(1, n_results), count),
            where={"$and": conditions},
            include=["distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [ChromaHit(str(identifier), float(distance)) for identifier, distance in zip(ids, distances)]

    async def delete(self, ids: list[str]) -> None:
        if ids:
            await asyncio.to_thread(self.collection.delete, ids=ids)

    async def delete_document(self, document_id: str) -> None:
        await asyncio.to_thread(self.collection.delete, where={"document_id": {"$eq": document_id}})

    def _memory_collection(self, kind: str):
        if kind == "summary":
            return self.summary_collection
        if kind == "profile":
            return self.profile_collection
        raise ValueError("未知记忆类型")

    async def upsert_memory(
        self, kind: str, identifier: str, document: str, vector: list[float], metadata: dict[str, Any]
    ) -> None:
        await asyncio.to_thread(
            self._memory_collection(kind).upsert,
            ids=[identifier], documents=[document], embeddings=[vector], metadatas=[metadata],
        )

    async def query_memory(
        self, kind: str, vector: list[float], *, user_id: str, tenant_id: str, n_results: int
    ) -> list[MemoryHit]:
        collection = self._memory_collection(kind)
        count = int(await asyncio.to_thread(collection.count))
        if count == 0:
            return []
        conditions: list[dict[str, Any]] = [
            {"user_id": {"$eq": user_id}}, {"tenant_id": {"$eq": tenant_id}},
        ]
        if kind == "profile":
            conditions.append({"status": {"$eq": "active"}})
        result = await asyncio.to_thread(
            collection.query,
            query_embeddings=[vector], n_results=min(max(1, n_results), count),
            where={"$and": conditions},
            include=["documents", "metadatas", "distances"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            MemoryHit(str(identifier), str(document), dict(metadata or {}), float(distance))
            for identifier, document, metadata, distance in zip(ids, documents, metadatas, distances)
        ]

    async def memory_count(self, kind: str) -> int:
        return int(await asyncio.to_thread(self._memory_collection(kind).count))

    async def list_memory(self, kind: str, *, user_id: str, tenant_id: str,
                          limit: int = 50, offset: int = 0) -> list[MemoryHit]:
        result = await asyncio.to_thread(
            self._memory_collection(kind).get,
            where={"$and": [{"user_id": {"$eq": user_id}}, {"tenant_id": {"$eq": tenant_id}}]},
            limit=limit, offset=offset, include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        return [MemoryHit(str(identifier), str(document), dict(metadata or {}), 0.0)
                for identifier, document, metadata in zip(ids, documents, metadatas)]

    async def get_memory(self, kind: str, identifier: str, *, user_id: str, tenant_id: str) -> MemoryHit | None:
        result = await asyncio.to_thread(
            self._memory_collection(kind).get, ids=[identifier], include=["documents", "metadatas"]
        )
        ids = result.get("ids") or []
        if not ids:
            return None
        metadata = dict((result.get("metadatas") or [{}])[0] or {})
        if metadata.get("user_id") != user_id or metadata.get("tenant_id") != tenant_id:
            return None
        return MemoryHit(str(ids[0]), str((result.get("documents") or [""])[0]), metadata, 0.0)

    async def delete_memory_id(self, kind: str, identifier: str, *, user_id: str, tenant_id: str) -> bool:
        item = await self.get_memory(kind, identifier, user_id=user_id, tenant_id=tenant_id)
        if not item:
            return False
        await asyncio.to_thread(self._memory_collection(kind).delete, ids=[identifier])
        return True

    async def delete_memory(self, kind: str, *, user_id: str, conversation_id: str | None = None) -> None:
        conditions: list[dict[str, Any]] = [
            {"user_id": {"$eq": user_id}},
            {"tenant_id": {"$eq": self.settings.memory_tenant_id}},
        ]
        if conversation_id:
            conditions.append({"conversation_id": {"$eq": conversation_id}})
        await asyncio.to_thread(self._memory_collection(kind).delete, where={"$and": conditions})

    async def count(self) -> int:
        return int(await asyncio.to_thread(self.collection.count))

    async def count_document(self, document_id: str) -> int:
        result = await asyncio.to_thread(
            self.collection.get,
            where={"document_id": {"$eq": document_id}},
            include=[],
        )
        return len(result.get("ids") or [])

    async def ready(self) -> bool:
        try:
            await asyncio.to_thread(self.client.heartbeat)
            await self.count()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close:
            await asyncio.to_thread(close)
