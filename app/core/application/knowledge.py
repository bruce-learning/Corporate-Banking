from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from sqlalchemy import select

from app.etl.parser import chunk_document, parse_document
from app.infrastructure.llm.aliyun import ModelServiceError
from app.models.entities.storage import DocumentVersion, KnowledgeChunk, utcnow


def stable_chunk_id(content_hash: str, index: int) -> str:
    return f"upload-{content_hash[:48]}-{index:04d}"


async def import_document(store, *, filename: str, data: bytes, domain: str, product: str | None,
                          user_id: str, effective_at: datetime | None = None,
                          allow_ocr: bool = False) -> tuple[DocumentVersion, bool]:
    if domain not in {"product", "process", "policy"}:
        raise ValueError("领域不合法")
    parsed = parse_document(filename, data, allow_ocr=allow_ocr)
    chunks = chunk_document(parsed)
    moment = effective_at or datetime.now(timezone.utc)
    async with store.sessions() as session:
        existing = await session.scalar(select(DocumentVersion).where(
            DocumentVersion.content_hash == parsed.sha256,
            DocumentVersion.domain == domain,
            DocumentVersion.audience == "demo",
        ))
        if existing:
            return existing, False
        document = DocumentVersion(
            title=parsed.title,
            source_filename=parsed.filename,
            source_type=parsed.source_type,
            domain=domain,
            product=product,
            content_hash=parsed.sha256,
            status="uploaded",
            audience="demo",
            synthetic=True,
            parser_version=parsed.parser_version,
            metadata_json={"warnings": parsed.warnings, "character_count": parsed.character_count},
            created_by=user_id,
            effective_at=moment,
        )
        session.add(document)
        await session.flush()
        for item in chunks:
            session.add(KnowledgeChunk(
                id=stable_chunk_id(parsed.sha256, item.index),
                document_id=document.id,
                chunk_index=item.index,
                title=parsed.title,
                domain=domain,
                product=product,
                content=item.text,
                source=parsed.filename,
                metadata_json=item.source,
                active=False,
            ))
        await session.commit()
        return document, True


async def publish_document(store, model_client, vector_store, document_id: str) -> DocumentVersion:
    async with store.sessions() as session:
        document = await session.scalar(select(DocumentVersion).where(DocumentVersion.id == document_id).with_for_update())
        if not document:
            raise LookupError("文档不存在")
        if document.status == "published":
            return document
        if document.status == "inactive":
            raise ValueError("停用文档不能直接重新发布")
        chunks = list((await session.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document_id).order_by(KnowledgeChunk.chunk_index))).all())
        if not chunks:
            raise ValueError("文档没有可发布分块")
        document.status = "processing"
        document.error_code = None
        await session.commit()

    try:
        vectors = []
        embedding_model = None
        for start in range(0, len(chunks), 10):
            result = await model_client.embed([item.content for item in chunks[start:start + 10]])
            vectors.extend(result.vectors)
            embedding_model = result.model
        if len(vectors) != len(chunks):
            raise ValueError("向量数量与分块数量不一致")
        document.status = "published"
        for item in chunks:
            item.active = True
        await vector_store.upsert(
            [(item, document) for item in chunks], vectors,
            embedding_model or "unknown",
        )
    except Exception as error:
        async with store.sessions() as session:
            failed = await session.get(DocumentVersion, document_id)
            if failed:
                failed.status = "failed"
                failed.error_code = error.category if isinstance(error, ModelServiceError) else "embedding_failed"
                await session.commit()
        raise

    async with store.sessions() as session:
        document = await session.scalar(select(DocumentVersion).where(DocumentVersion.id == document_id).with_for_update())
        current = list((await session.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document_id).order_by(KnowledgeChunk.chunk_index))).all())
        for item, vector in zip(current, vectors):
            # ChromaDB is the primary vector store. PostgreSQL keeps document
            # governance and embedding provenance, not new vector payloads.
            item.embedding_json = None
            item.embedding_model = embedding_model or "unknown"
            item.embedding_dimension = len(vector)
            item.active = True
        document.status = "published"
        document.published_at = utcnow()
        document.error_code = None
        await session.commit()
        return document


async def deactivate_document(store, vector_store, document_id: str) -> DocumentVersion:
    async with store.sessions() as session:
        document = await session.scalar(select(DocumentVersion).where(DocumentVersion.id == document_id).with_for_update())
        if not document:
            raise LookupError("文档不存在")
        chunks = (await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id))).all()
        for item in chunks:
            item.active = False
        document.status = "inactive"
        document.inactive_at = utcnow()
        await session.commit()
    await vector_store.delete([item.id for item in chunks])
    return document


def hash_rows(rows: list[dict]) -> str:
    rendered = "\n".join(sorted(str(row) for row in rows))
    return hashlib.sha256(rendered.encode()).hexdigest()
