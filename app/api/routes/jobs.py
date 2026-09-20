from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from app.api.dependencies import Identity, administrator, require_store
from app.models.entities.storage import BackgroundJob, utcnow


router = APIRouter(prefix="/api/v1/admin/jobs", tags=["background-jobs"])


def serialize(job: BackgroundJob):
    return {
        "id": job.id, "job_type": job.job_type, "status": job.status,
        "attempts": job.attempts, "max_attempts": job.max_attempts,
        "result": job.result_json, "error_code": job.error_code,
        "error_message": job.error_message, "available_at": job.available_at,
        "started_at": job.started_at, "completed_at": job.completed_at,
        "created_at": job.created_at,
    }


@router.get("")
async def listing(request: Request, status: str | None = Query(default=None),
                  limit: int = Query(default=50, ge=1, le=100),
                  identity: Identity = Depends(administrator)):
    statement = select(BackgroundJob).order_by(BackgroundJob.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(BackgroundJob.status == status)
    async with require_store(request).sessions() as session:
        rows = list((await session.scalars(statement)).all())
        pending = int(await session.scalar(select(func.count()).select_from(BackgroundJob).where(
            BackgroundJob.status.in_(["queued", "retrying", "running"])
        )) or 0)
    return {"items": [serialize(item) for item in rows], "pending": pending}


@router.post("/{job_id}/retry", status_code=202)
async def retry(job_id: str, request: Request, identity: Identity = Depends(administrator)):
    async with require_store(request).sessions() as session:
        job = await session.get(BackgroundJob, job_id, with_for_update=True)
        if not job:
            raise HTTPException(404, "后台任务不存在")
        if job.status != "failed":
            raise HTTPException(409, "仅失败任务可以重试")
        job.status = "retrying"; job.available_at = utcnow(); job.completed_at = None
        await session.commit()
    await request.app.state.job_queue.notify(job_id)
    return {"id": job_id, "status": "retrying"}
