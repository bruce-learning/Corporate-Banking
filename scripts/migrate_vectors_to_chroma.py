"""将 PostgreSQL 中已有向量幂等迁移到 ChromaDB；不产生模型调用。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.infrastructure.database.store import Store
from app.infrastructure.vector.chroma import ChromaVectorStore
from app.models.entities.storage import DocumentVersion, KnowledgeChunk


async def run() -> dict:
    settings = get_settings()
    store = Store(settings.database_url.get_secret_value())
    vectors = ChromaVectorStore(settings)
    try:
        async with store.sessions() as session:
            rows = list((await session.execute(
                select(KnowledgeChunk, DocumentVersion)
                .join(DocumentVersion, DocumentVersion.id == KnowledgeChunk.document_id)
                .where(
                    KnowledgeChunk.active.is_(True),
                    KnowledgeChunk.embedding_json.is_not(None),
                    DocumentVersion.status == "published",
                    DocumentVersion.audience == "demo",
                )
                .order_by(KnowledgeChunk.id)
            )).all())
        for start in range(0, len(rows), 50):
            batch = rows[start:start + 50]
            await vectors.upsert(
                batch,
                [[float(value) for value in chunk.embedding_json] for chunk, _ in batch],
                settings.dashscope_embedding_model,
            )
        return {
            "status": "migrated",
            "postgres_vectors_read": len(rows),
            "chromadb_collection": settings.chroma_collection,
            "chromadb_records": await vectors.count(),
            "paid_model_calls": 0,
        }
    finally:
        await vectors.close()
        await store.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
