from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
from uuid import uuid4

from mcp import Client
from sqlalchemy import delete, select

from app.core.application.knowledge import deactivate_document, import_document, publish_document
from app.core.rag.hybrid import HybridSearchService, bm25
from app.infrastructure.llm.contracts import FakeModelClient
from app.models.entities.storage import DocumentVersion, KnowledgeChunk
from mcp_server.server import build_server
from tests.conftest import login
from tests.test_knowledge_foundation import docx_bytes


async def add_document(runtime, *, status="published", audience="demo", effective=None,
                       expires=None, active=True, domain="product", content="企业网银支持查询和转账"):
    suffix = uuid4().hex
    document_id = "test-doc-" + suffix
    chunk_id = "test-chunk-" + suffix
    async with runtime.store.sessions() as session:
        session_document = DocumentVersion(
            id=document_id,
            title="企业网银测试",
            source_filename="test.jsonl",
            source_type="test",
            domain=domain,
            product="企业网银",
            content_hash=hashlib.sha256(suffix.encode()).hexdigest(),
            status=status,
            audience=audience,
            synthetic=True,
            parser_version="test",
            metadata_json={},
            effective_at=effective or datetime.now(timezone.utc) - timedelta(days=1),
            expires_at=expires,
            published_at=datetime.now(timezone.utc) if status == "published" else None,
        )
        session.add(session_document)
        session_chunk = KnowledgeChunk(
            id=chunk_id,
            document_id=document_id,
            chunk_index=0,
            title="企业网银测试",
            domain=domain,
            product="企业网银",
            content=content,
            source="合成测试",
            metadata_json={},
            embedding_json=[1.0, 0.0],
            embedding_model="fake",
            embedding_dimension=2,
            active=active,
        )
        session.add(session_chunk)
        await session.commit()
    await runtime.app.state.vector_store.upsert(
        [(session_chunk, session_document)], [[1.0, 0.0]], "fake"
    )
    return document_id, chunk_id


async def cleanup(runtime, identifiers):
    for identifier in identifiers:
        await runtime.app.state.vector_store.delete_document(identifier)
    async with runtime.store.sessions() as session:
        await session.execute(delete(DocumentVersion).where(DocumentVersion.id.in_(identifiers)))
        await session.commit()


def test_bm25_prefers_relevant_text():
    scores = bm25("企业网银怎么收费", ["企业网银服务收费规则", "流动资金贷款申请材料"])
    assert scores[0] > scores[1]


async def test_published_effective_audience_filter(runtime):
    now = datetime.now(timezone.utc)
    created = []
    valid_doc, valid_chunk = await add_document(runtime)
    created.append(valid_doc)
    for options in [
        {"status": "draft"},
        {"status": "inactive", "active": False},
        {"expires": now - timedelta(seconds=1)},
        {"effective": now + timedelta(days=1)},
        {"audience": "restricted"},
    ]:
        item, _ = await add_document(runtime, **options)
        created.append(item)
    try:
        result = await HybridSearchService(
            runtime.store, vector_store=runtime.app.state.vector_store
        ).search("企业网银", top_k=10, query_vector=[1.0, 0.0])
        ids = [item.id for item in result.items]
        assert valid_chunk in ids
        assert len([item for item in ids if item.startswith("test-chunk-")]) == 1
        assert result.mode == "hybrid" and result.degraded is False
        assert result.vector_backend == "chromadb"
    finally:
        await cleanup(runtime, created)


async def test_search_api_keyword_degradation_is_explicit(runtime):
    document_id, chunk_id = await add_document(runtime)
    try:
        response = await runtime.client.post(
            "/api/v1/search",
            headers=await login(runtime),
            json={"query": "企业网银查询转账", "domain": "product", "top_k": 3},
        )
        assert response.status_code == 200
        body = response.json()
        assert any(item["id"] == chunk_id for item in body["items"])
        assert body["mode"] == "keyword" and body["degraded"] is True
        assert body["degradation_reason"] == "paid_calls_disabled"
    finally:
        await cleanup(runtime, [document_id])


async def test_deactivate_removes_chromadb_vector(runtime):
    document_id, _ = await add_document(runtime)
    try:
        assert await runtime.app.state.vector_store.count() == 1
        document = await deactivate_document(
            runtime.store, runtime.app.state.vector_store, document_id
        )
        assert document.status == "inactive"
        assert await runtime.app.state.vector_store.count() == 0
    finally:
        await cleanup(runtime, [document_id])


async def test_publish_writes_content_and_vector_to_chromadb(runtime):
    document, _ = await import_document(
        runtime.store,
        filename="发布到向量库.docx",
        data=docx_bytes(),
        domain="process",
        product="企业网银",
        user_id=runtime.users["manager"].id,
    )
    try:
        published = await publish_document(
            runtime.store, FakeModelClient(), runtime.app.state.vector_store, document.id
        )
        assert published.status == "published"
        async with runtime.store.sessions() as session:
            chunks = list((await session.scalars(select(KnowledgeChunk).where(
                KnowledgeChunk.document_id == document.id
            ))).all())
        assert chunks and all(item.embedding_json is None for item in chunks)
        record = await asyncio.to_thread(
            runtime.app.state.vector_store.collection.get,
            ids=[chunks[0].id], include=["documents", "metadatas"],
        )
        assert record["documents"][0]
        assert record["metadatas"][0]["domain"] == "process"
    finally:
        await cleanup(runtime, [document.id])


async def test_document_import_idempotence_publish_guard_and_deactivate(runtime):
    headers = await login(runtime, "manager")
    payload = {"domain": "product", "product": "企业网银"}
    first = await runtime.client.post(
        "/api/v1/admin/documents",
        headers=headers,
        data=payload,
        files={"file": ("导入规则.docx", docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert first.status_code == 201 and first.json()["created"] is True
    identifier = first.json()["id"]
    try:
        second = await runtime.client.post(
            "/api/v1/admin/documents",
            headers=headers,
            data=payload,
            files={"file": ("导入规则.docx", docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert second.status_code == 201 and second.json()["created"] is False
        detail = await runtime.client.get(f"/api/v1/admin/documents/{identifier}", headers=headers)
        assert detail.status_code == 200 and detail.json()["chunk_count"] >= 1
        publish = await runtime.client.post(f"/api/v1/admin/documents/{identifier}/publish", headers=headers)
        assert publish.status_code == 409
        inactive = await runtime.client.post(f"/api/v1/admin/documents/{identifier}/deactivate", headers=headers)
        assert inactive.status_code == 200 and inactive.json()["status"] == "inactive"
    finally:
        await cleanup(runtime, [identifier])


async def test_official_mcp_in_process_discovery_and_call(runtime):
    document_id, chunk_id = await add_document(runtime)
    try:
        server = build_server(runtime.settings)
        async with Client(server) as client:
            listed = await client.list_tools()
            assert sorted(item.name for item in listed.tools) == ["policy_lookup", "process_lookup", "product_search"]
            result = await client.call_tool("product_search", {"query": "企业网银有什么功能", "top_k": 3})
            assert result.is_error is False
            data = result.structured_content or {}
            assert any(item["id"] == chunk_id for item in data["items"])
            assert data["mode"] == "keyword" and data["degraded"] is True
    finally:
        await cleanup(runtime, [document_id])
