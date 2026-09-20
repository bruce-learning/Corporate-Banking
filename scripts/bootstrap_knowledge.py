"""将版本化合成知识与过滤负例导入 PostgreSQL，并使用真实阿里云向量。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.infrastructure.database.store import Store
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.vector.chroma import ChromaVectorStore
from app.models.entities.storage import DocumentVersion, KnowledgeChunk, KnowledgeIndex, utcnow


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_hash(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def run(confirm: bool) -> dict:
    if not confirm:
        raise RuntimeError("必须传入 --confirm-paid-call")
    settings = get_settings()
    if not settings.enable_paid_model_calls:
        raise RuntimeError("ENABLE_PAID_MODEL_CALLS 必须仅对本次导入命令显式开启")
    base = ROOT / "datasets" / "generated"
    manifest_path = base / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    knowledge = load_jsonl(base / "knowledge.jsonl")
    controls = load_jsonl(base / "knowledge_controls.jsonl")
    store = Store(settings.database_url.get_secret_value())
    client = AliyunModelClient(settings)
    vector_store = ChromaVectorStore(settings)
    try:
        async with store.sessions() as session:
            existing = await session.scalar(select(KnowledgeIndex).where(
                KnowledgeIndex.data_version == manifest["data_version"]))
            if existing:
                rows = list((await session.execute(
                    select(KnowledgeChunk, DocumentVersion)
                    .join(DocumentVersion, DocumentVersion.id == KnowledgeChunk.document_id)
                    .where(KnowledgeChunk.active.is_(True), KnowledgeChunk.embedding_json.is_not(None))
                )).all())
                for start in range(0, len(rows), 50):
                    batch = rows[start:start + 50]
                    await vector_store.upsert(
                        batch,
                        [[float(value) for value in chunk.embedding_json] for chunk, _ in batch],
                        settings.dashscope_embedding_model,
                    )
                return {
                    "status": "already_loaded", "data_version": existing.data_version,
                    "active_chunks": len(rows), "chromadb_records": await vector_store.count(),
                }

        vectors = []
        for start in range(0, len(knowledge), 10):
            batch = [f"{row['title']} {row['product']} {row['content']}" for row in knowledge[start:start + 10]]
            vectors.extend((await client.embed(batch)).vectors)
        if len(vectors) != len(knowledge):
            raise RuntimeError("Embedding 数量与知识数量不一致")

        async with store.sessions() as session:
            for row, vector in zip(knowledge, vectors):
                document_id = "doc-" + row["id"]
                session.add(DocumentVersion(
                    id=document_id,
                    title=row["title"],
                    source_filename="knowledge.jsonl",
                    source_type="synthetic_jsonl",
                    domain=row["domain"],
                    product=row["product"],
                    content_hash=row_hash(row),
                    status="published",
                    audience=row["audience"],
                    synthetic=True,
                    parser_version="generator-2.0",
                    metadata_json={"product_code": row["product_code"], "topic": row["topic"], "version": row["version"]},
                    effective_at=datetime.fromisoformat(row["effective_at"]),
                    expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
                    published_at=utcnow(),
                ))
                session.add(KnowledgeChunk(
                    id=row["id"],
                    document_id=document_id,
                    chunk_index=0,
                    title=row["title"],
                    domain=row["domain"],
                    product=row["product"],
                    content=row["content"],
                    source=row["source"],
                    metadata_json={
                        "product_code": row["product_code"], "topic": row["topic"],
                        "keywords": row["keywords"], "version": row["version"], "notice": row["notice"],
                    },
                    embedding_json=vector,
                    embedding_model=settings.dashscope_embedding_model,
                    embedding_dimension=len(vector),
                    active=True,
                ))
            for row in controls:
                document_id = "doc-" + row["id"]
                session.add(DocumentVersion(
                    id=document_id,
                    title=row["title"],
                    source_filename="knowledge_controls.jsonl",
                    source_type="synthetic_control_jsonl",
                    domain=row["domain"],
                    product=row["product"],
                    content_hash=row_hash(row),
                    status=row["status"],
                    audience=row["audience"],
                    synthetic=True,
                    parser_version="generator-2.0",
                    metadata_json={"control_type": row["control_type"], "must_not_retrieve": True},
                    effective_at=datetime.fromisoformat(row["effective_at"]),
                    expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
                    published_at=utcnow() if row["status"] == "published" else None,
                    inactive_at=utcnow() if row["status"] == "inactive" else None,
                ))
                session.add(KnowledgeChunk(
                    id=row["id"], document_id=document_id, chunk_index=0, title=row["title"],
                    domain=row["domain"], product=row["product"], content=row["content"], source=row["source"],
                    metadata_json={"control_type": row["control_type"], "must_not_retrieve": True},
                    active=row["status"] == "published",
                ))
            await session.execute(update(KnowledgeIndex).values(active=False))
            manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            session.add(KnowledgeIndex(
                id="index-" + manifest["data_version"],
                data_version=manifest["data_version"],
                manifest_hash=manifest_hash,
                embedding_model=settings.dashscope_embedding_model,
                embedding_dimension=settings.dashscope_embedding_dimension,
                document_count=len(knowledge) + len(controls),
                chunk_count=len(knowledge) + len(controls),
                active=True,
            ))
            await session.commit()
        async with store.sessions() as session:
            indexed_rows = list((await session.execute(
                select(KnowledgeChunk, DocumentVersion)
                .join(DocumentVersion, DocumentVersion.id == KnowledgeChunk.document_id)
                .where(KnowledgeChunk.id.in_([row["id"] for row in knowledge]))
                .order_by(KnowledgeChunk.id)
            )).all())
        vector_by_id = {row["id"]: vector for row, vector in zip(knowledge, vectors)}
        for start in range(0, len(indexed_rows), 50):
            batch = indexed_rows[start:start + 50]
            await vector_store.upsert(
                batch, [vector_by_id[chunk.id] for chunk, _ in batch],
                settings.dashscope_embedding_model,
            )
        return {
            "status": "loaded",
            "data_version": manifest["data_version"],
            "knowledge": len(knowledge),
            "controls": len(controls),
            "embedding_requests": (len(knowledge) + 9) // 10,
            "embedding_dimension": settings.dashscope_embedding_dimension,
            "chromadb_records": await vector_store.count(),
        }
    finally:
        await vector_store.close()
        await client.aclose()
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid-call", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.confirm_paid_call)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
