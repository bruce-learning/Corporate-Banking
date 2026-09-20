import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from app.api.dependencies import Identity, administrator

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "stage": "fullstack_demo"}


@router.get("/ready")
async def ready(request: Request):
    checks = await dependency_status(request)
    available = all(checks.values()) and request.app.state.chat_service is not None
    body = {
        "status": "ready" if available else "not_ready",
        "stage": "fullstack_demo",
        "checks": checks,
        "paid_calls_enabled": request.app.state.settings.enable_paid_model_calls,
        "supported_degraded_mode": "keyword_retrieval_and_react_evidence_extract",
        "transaction_execution": False,
        "execution_modes": ["fixed", "parallel", "plan_execute"],
        "orchestration_backend": request.app.state.settings.orchestration_backend,
        "agent_execution_mode": "bounded_react" if request.app.state.settings.react_enabled else "single_pass",
    }
    return JSONResponse(body, status_code=200 if available else 503)


async def dependency_status(request: Request):
    async def database():
        return bool(request.app.state.store and await request.app.state.store.ready())
    async def redis():
        return bool(request.app.state.cache and await request.app.state.cache.client.ping())
    async def chroma():
        return bool(request.app.state.vector_store and await request.app.state.vector_store.ready())
    async def mcp():
        return bool(request.app.state.mcp_gateway and await request.app.state.mcp_gateway.ready())
    async def probe(function):
        try:
            return bool(await asyncio.wait_for(function(), request.app.state.settings.dependency_timeout_seconds))
        except Exception:
            return False
    db, cache, vectors, tools = await asyncio.gather(probe(database), probe(redis), probe(chroma), probe(mcp))
    return {"database_schema": db, "redis": cache, "chromadb": vectors, "mcp": tools}


@router.get("/api/v1/health/live")
async def live():
    return await health()


@router.get("/api/v1/health/dependencies")
async def dependencies(request: Request):
    checks = await dependency_status(request)
    available = all(checks.values())
    return JSONResponse({"status": "ready" if available else "not_ready", "checks": checks},
                        status_code=200 if available else 503)


@router.get("/api/v1/health/ready")
async def business_ready(request: Request):
    return await ready(request)


@router.get("/api/v1/admin/status")
async def admin_status(request: Request, identity: Identity = Depends(administrator)):
    return {
        "stage": "fullstack_demo",
        "checks": await dependency_status(request),
        "bank_chat_ready": request.app.state.chat_service is not None,
        "document_preview_formats": ["json", "jsonl", "txt", "md", "docx", "pdf", "xlsx", "xls"],
        "paid_calls_enabled": request.app.state.settings.enable_paid_model_calls,
        "orchestration_backend": request.app.state.settings.orchestration_backend,
        "skills_loaded": request.app.state.skill_manager.summary()["count"] if request.app.state.skill_manager else 0,
        "memory_layers": ["redis_working", "chroma_conversation_summaries", "chroma_user_profiles"],
    }
