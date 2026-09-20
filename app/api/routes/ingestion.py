from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select

from app.api.dependencies import Identity, administrator, require_store, require_vector_store
from app.core.application.ingestion import payload_path
from app.models.entities.storage import DocumentVersion, IngestionJob, KnowledgeChunk


router = APIRouter(prefix="/api/v1/admin/ingestion-jobs", tags=["ingestion"])


def serialize(job: IngestionJob) -> dict:
    return {
        "id": job.id, "source_filename": job.source_filename, "domain": job.domain,
        "product": job.product, "status": job.status, "auto_publish": job.auto_publish,
        "document_id": job.document_id, "error_code": job.error_code,
        "error_message": job.error_message, "created_at": job.created_at,
        "started_at": job.started_at, "completed_at": job.completed_at,
    }


@router.post("", status_code=202)
async def create_job(
    request: Request,
    domain: str = Form(...), product: str | None = Form(default=None),
    auto_publish: bool = Form(default=False), file: UploadFile = File(...),
    identity: Identity = Depends(administrator),
):
    if domain not in {"product", "process", "policy"}:
        raise HTTPException(422, "领域不合法")
    maximum = request.app.state.settings.ingestion_job_max_bytes
    data = await file.read(maximum + 1)
    await file.close()
    if not data:
        raise HTTPException(422, "文件为空")
    if len(data) > maximum:
        raise HTTPException(413, "文件超过后台导入上限")
    filename = file.filename or "unnamed"
    async with require_store(request).sessions() as session:
        job = IngestionJob(
            user_id=identity.user_id, source_filename=filename, domain=domain,
            product=product, status="queued", auto_publish=auto_publish,
        )
        session.add(job)
        await session.flush()
        identifier = job.id
        await session.commit()
    target = payload_path(request.app.state.settings.data_dir, identifier)
    target.write_bytes(data)
    queue = getattr(request.app.state, "job_queue", None)
    if queue is None:
        raise HTTPException(503, "后台任务队列尚未初始化")
    worker_job = await queue.enqueue("ingestion", {"ingestion_job_id": identifier}, user_id=identity.user_id)
    return {"id": identifier, "status": "queued", "worker_job_id": worker_job.id,
            "notice": "任务已持久化并进入外部Worker处理"}


@router.get("")
async def list_jobs(request: Request, status: str | None = Query(default=None),
                    limit: int = Query(default=20, ge=1, le=100),
                    identity: Identity = Depends(administrator)):
    statement = select(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(IngestionJob.status == status)
    async with require_store(request).sessions() as session:
        items = list((await session.scalars(statement)).all())
    return {"items": [serialize(item) for item in items]}


@router.get("/{job_id}")
async def get_job(job_id: str, request: Request, identity: Identity = Depends(administrator)):
    async with require_store(request).sessions() as session:
        job = await session.get(IngestionJob, job_id)
    if not job:
        raise HTTPException(404, "导入任务不存在")
    return serialize(job)


@router.post("/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request,
                    identity: Identity = Depends(administrator)):
    target = payload_path(request.app.state.settings.data_dir, job_id)
    async with require_store(request).sessions() as session:
        job = await session.get(IngestionJob, job_id)
        if not job:
            raise HTTPException(404, "导入任务不存在")
        if job.status != "failed" or not target.exists():
            raise HTTPException(409, "仅保留了原始载荷的失败任务可以重试")
        job.status = "retrying"
        job.completed_at = None
        await session.commit()
    queue = getattr(request.app.state, "job_queue", None)
    if queue is None:
        raise HTTPException(503, "后台任务队列尚未初始化")
    worker_job = await queue.enqueue("ingestion", {"ingestion_job_id": job_id}, user_id=identity.user_id)
    return {"id": job_id, "status": "retrying", "worker_job_id": worker_job.id}


@router.get("/consistency/summary")
async def consistency(request: Request, identity: Identity = Depends(administrator)):
    store, vectors = require_store(request), require_vector_store(request)
    async with store.sessions() as session:
        documents = list((await session.scalars(select(DocumentVersion).where(
            DocumentVersion.status == "published"
        ).order_by(DocumentVersion.id))).all())
        rows = []
        for document in documents:
            active = int(await session.scalar(select(func.count()).select_from(KnowledgeChunk).where(
                KnowledgeChunk.document_id == document.id, KnowledgeChunk.active.is_(True)
            )) or 0)
            vector_count = await vectors.count_document(document.id)
            rows.append({"document_id": document.id, "postgres_active_chunks": active,
                         "chromadb_chunks": vector_count, "consistent": active == vector_count})
    return {"documents": rows, "consistent": all(item["consistent"] for item in rows)}
