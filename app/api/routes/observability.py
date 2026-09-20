from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.api.dependencies import Identity, administrator, require_store
from app.models.entities.storage import BackgroundJob, ChatTrace, IngestionJob, ModelUsageRecord, utcnow


router = APIRouter(prefix="/api/v1/admin/observability", tags=["observability"])


@router.get("/summary")
async def summary(request: Request, days: int = Query(default=7, ge=1, le=90),
                  identity: Identity = Depends(administrator)):
    since = utcnow() - timedelta(days=days)
    async with require_store(request).sessions() as session:
        usage_rows = (await session.execute(
            select(
                ModelUsageRecord.model, ModelUsageRecord.operation,
                func.count(ModelUsageRecord.id), func.sum(ModelUsageRecord.input_tokens),
                func.sum(ModelUsageRecord.output_tokens),
            ).where(ModelUsageRecord.created_at >= since)
            .group_by(ModelUsageRecord.model, ModelUsageRecord.operation)
            .order_by(ModelUsageRecord.model, ModelUsageRecord.operation)
        )).all()
        total_chats = int(await session.scalar(select(func.count()).select_from(ChatTrace).where(
            ChatTrace.created_at >= since
        )) or 0)
        degraded = int(await session.scalar(select(func.count()).select_from(ChatTrace).where(
            ChatTrace.created_at >= since, ChatTrace.degraded.is_(True)
        )) or 0)
        invalid_citations = int(await session.scalar(select(func.count()).select_from(ChatTrace).where(
            ChatTrace.created_at >= since, ChatTrace.citation_valid.is_(False)
        )) or 0)
        failed_jobs = int(await session.scalar(select(func.count()).select_from(IngestionJob).where(
            IngestionJob.created_at >= since, IngestionJob.status == "failed"
        )) or 0)
        background_rows = (await session.execute(select(
            BackgroundJob.status, func.count(BackgroundJob.id)
        ).where(BackgroundJob.created_at >= since).group_by(BackgroundJob.status))).all()
    return {
        "window_days": days,
        "chat_requests": total_chats,
        "degraded_requests": degraded,
        "degraded_rate": degraded / total_chats if total_chats else 0.0,
        "invalid_citation_requests": invalid_citations,
        "failed_ingestion_jobs": failed_jobs,
        "background_jobs": {row[0]: int(row[1]) for row in background_rows},
        "model_usage": [{
            "model": row[0], "operation": row[1], "calls": int(row[2] or 0),
            "input_tokens": int(row[3] or 0), "output_tokens": int(row[4] or 0),
        } for row in usage_rows],
        "privacy": "不返回用户正文、令牌、密码或 API Key",
    }
