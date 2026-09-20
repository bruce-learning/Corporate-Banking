from __future__ import annotations

from pathlib import Path
from sqlalchemy import select

from app.core.application.knowledge import import_document, publish_document
from app.etl.parser import DocumentParseError
from app.infrastructure.llm.aliyun import ModelServiceError, PaidCallsDisabled
from app.models.entities.storage import IngestionJob, utcnow


def payload_path(data_dir: Path, job_id: str) -> Path:
    root = (data_dir / "ingestion-jobs").resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / f"{job_id}.payload").resolve()
    if root not in target.parents:
        raise ValueError("导入任务路径越界")
    return target


async def recover_interrupted_jobs(store) -> int:
    """Worker重启时把执行中的导入恢复为可重试，原始载荷仍保留。"""
    async with store.sessions() as session:
        jobs = list((await session.scalars(select(IngestionJob).where(
            IngestionJob.status.in_(["parsing", "embedding"])
        ))).all())
        for job in jobs:
            job.status = "retrying"
            job.error_code = "worker_interrupted"
            job.error_message = "Worker重启中断任务，已自动进入重试"
            job.completed_at = None
        if jobs:
            await session.commit()
        return len(jobs)


async def process_ingestion_job(store, model_client, vector_store, data_dir: Path, job_id: str,
                                allow_ocr: bool = False):
    path = payload_path(data_dir, job_id)
    async with store.sessions() as session:
        job = await session.get(IngestionJob, job_id)
        if not job or job.status not in {"queued", "retrying"}:
            return {"status": "ignored"}
        job.status = "parsing"
        job.started_at = utcnow()
        job.error_code = None
        job.error_message = None
        await session.commit()
        filename, domain, product, user_id, auto_publish = (
            job.source_filename, job.domain, job.product, job.user_id, job.auto_publish
        )
    try:
        data = path.read_bytes()
        document, _ = await import_document(
            store, filename=filename, data=data, domain=domain, product=product, user_id=user_id,
            allow_ocr=allow_ocr,
        )
        async with store.sessions() as session:
            job = await session.get(IngestionJob, job_id)
            job.document_id = document.id
            job.status = "embedding" if auto_publish else "completed"
            if not auto_publish:
                job.completed_at = utcnow()
            await session.commit()
        if auto_publish:
            await publish_document(store, model_client, vector_store, document.id)
            async with store.sessions() as session:
                job = await session.get(IngestionJob, job_id)
                job.status = "completed"
                job.completed_at = utcnow()
                await session.commit()
        path.unlink(missing_ok=True)
        return {"status": "completed", "document_id": document.id}
    except Exception as error:
        if isinstance(error, DocumentParseError):
            code, message = error.code, str(error)
        elif isinstance(error, PaidCallsDisabled):
            code, message = "paid_calls_disabled", str(error)
        elif isinstance(error, ModelServiceError):
            code, message = error.category, "模型服务暂不可用"
        else:
            code, message = "ingestion_failed", "导入任务执行失败"
        async with store.sessions() as session:
            job = await session.get(IngestionJob, job_id)
            if job:
                job.status = "failed"
                job.error_code = code
                job.error_message = message[:300]
                job.completed_at = utcnow()
                await session.commit()
        return {"status": "failed", "error_code": code, "error_message": message[:300]}
