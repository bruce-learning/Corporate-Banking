from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select

from app.api.dependencies import Identity, administrator, require_model_client, require_store, require_vector_store
from app.core.application.knowledge import deactivate_document, import_document, publish_document
from app.etl.parser import DocumentParseError, chunk_document, parse_document
from app.infrastructure.llm.aliyun import ModelServiceError, PaidCallsDisabled
from app.models.entities.storage import DocumentVersion, KnowledgeChunk


router = APIRouter(prefix="/api/v1/admin/documents", tags=["documents"])


def serialize(document: DocumentVersion, chunk_count: int | None = None) -> dict:
    value = {
        "id": document.id,
        "title": document.title,
        "source_filename": document.source_filename,
        "source_type": document.source_type,
        "domain": document.domain,
        "product": document.product,
        "content_hash": document.content_hash,
        "status": document.status,
        "audience": document.audience,
        "synthetic": document.synthetic,
        "effective_at": document.effective_at,
        "expires_at": document.expires_at,
        "created_at": document.created_at,
        "published_at": document.published_at,
        "inactive_at": document.inactive_at,
        "error_code": document.error_code,
        "metadata": document.metadata_json,
    }
    if chunk_count is not None:
        value["chunk_count"] = chunk_count
    return value


@router.post("/preview")
async def preview_document(
    request: Request,
    file: UploadFile = File(...),
    identity: Identity = Depends(administrator),
):
    maximum = request.app.state.settings.max_upload_bytes
    data = await file.read(maximum + 1)
    await file.close()
    if len(data) > maximum:
        raise HTTPException(413, "文件超过上传上限")
    try:
        document = parse_document(file.filename or "unnamed", data)
        chunks = chunk_document(document)
    except DocumentParseError as error:
        raise HTTPException(422, detail={"code": error.code, "message": str(error)}) from error
    return {
        "filename": document.filename,
        "source_type": document.source_type,
        "title": document.title,
        "sha256": document.sha256,
        "parser_version": document.parser_version,
        "character_count": document.character_count,
        "block_count": len(document.blocks),
        "chunk_count": len(chunks),
        "warnings": document.warnings,
        "preview": [chunk.model_dump() for chunk in chunks[:10]],
        "notice": "仅完成解析预览，尚未发布到知识库",
    }


@router.post("", status_code=201)
async def create_document(
    request: Request,
    domain: str = Form(...),
    product: str | None = Form(default=None),
    effective_at: datetime | None = Form(default=None),
    file: UploadFile = File(...),
    identity: Identity = Depends(administrator),
):
    maximum = request.app.state.settings.max_upload_bytes
    data = await file.read(maximum + 1)
    await file.close()
    if len(data) > maximum:
        raise HTTPException(413, "文件超过上传上限")
    try:
        document, created = await import_document(
            require_store(request), filename=file.filename or "unnamed", data=data,
            domain=domain, product=product, user_id=identity.user_id, effective_at=effective_at,
        )
    except DocumentParseError as error:
        raise HTTPException(422, detail={"code": error.code, "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    count = await _chunk_count(request, document.id)
    response = serialize(document, count)
    response["created"] = created
    response["notice"] = "文档已保存为待发布状态；发布前不会进入检索"
    return response


async def _chunk_count(request: Request, document_id: str) -> int:
    async with require_store(request).sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document_id)) or 0)


@router.get("")
async def list_documents(
    request: Request,
    status: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    identity: Identity = Depends(administrator),
):
    statement = select(DocumentVersion).order_by(DocumentVersion.created_at.desc(), DocumentVersion.id).offset(offset).limit(limit)
    if status:
        statement = statement.where(DocumentVersion.status == status)
    async with require_store(request).sessions() as session:
        items = (await session.scalars(statement)).all()
    return {"items": [serialize(item) for item in items], "offset": offset, "limit": limit}


@router.get("/{document_id}")
async def document_detail(document_id: str, request: Request, identity: Identity = Depends(administrator)):
    async with require_store(request).sessions() as session:
        document = await session.get(DocumentVersion, document_id)
        if not document:
            raise HTTPException(404, "文档不存在")
        chunks = (await session.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.document_id == document_id).order_by(KnowledgeChunk.chunk_index).limit(20))).all()
    response = serialize(document, len(chunks))
    response["chunks"] = [{
        "id": item.id, "index": item.chunk_index, "content": item.content,
        "active": item.active, "metadata": item.metadata_json,
    } for item in chunks]
    return response


@router.post("/{document_id}/publish")
async def publish(document_id: str, request: Request, identity: Identity = Depends(administrator)):
    try:
        document = await publish_document(
            require_store(request), require_model_client(request), require_vector_store(request), document_id
        )
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except PaidCallsDisabled as error:
        raise HTTPException(409, str(error)) from error
    except ModelServiceError as error:
        raise HTTPException(503, f"Embedding 发布失败：{error.category}") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return serialize(document, await _chunk_count(request, document_id))


@router.post("/{document_id}/deactivate")
async def deactivate(document_id: str, request: Request, identity: Identity = Depends(administrator)):
    try:
        document = await deactivate_document(require_store(request), require_vector_store(request), document_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    return serialize(document, await _chunk_count(request, document_id))
