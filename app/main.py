"""银行对公智能客服统一 FastAPI 入口。"""

from contextlib import asynccontextmanager
import logging
import time
from uuid import uuid4
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api.routes.health import router as health_router
from app.config import Settings, get_settings
from app.api.routes.auth import router as auth_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.documents import router as documents_router
from app.api.routes.search import router as search_router
from app.api.routes.chat import router as chat_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.evaluations import router as evaluations_router
from app.api.routes.observability import router as observability_router
from app.api.routes.skills import router as skills_router
from app.core.application.chat import ChatService
from app.core.intent.recognizer import IntentRecognizer
from app.core.orchestration.bank import BankOrchestrator
from app.infrastructure.cache.store import Cache
from app.infrastructure.database.store import Store
from app.infrastructure.observability.body_limit import BodyLimitMiddleware
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.vector.chroma import ChromaVectorStore
from app.infrastructure.mcp.gateway import StandardMCPGateway
from app.infrastructure.observability.metrics import MetricsRegistry
from app.core.rag.hybrid import HybridSearchService
from app.core.memory.service import MemoryService
from app.core.skills.manager import SkillManager
from app.infrastructure.queue.redis_stream import JobQueue
from app.api.routes.memories import router as memories_router
from app.api.routes.jobs import router as jobs_router

logger = logging.getLogger("bank.requests")


def create_app(settings: Settings | None = None, model_client=None, mcp_gateway=None) -> FastAPI:
    settings = settings or get_settings()
    @asynccontextmanager
    async def lifespan(application):
        application.state.store = Store(settings.database_url.get_secret_value()) if settings.database_url.get_secret_value() else None
        application.state.cache = Cache(settings) if settings.redis_url.get_secret_value() else None
        # Always construct the client so keyword-only degradation and the
        # explicit paid-call guard remain available even when no key is set.
        # The client never performs network I/O until chat/embed is called.
        application.state.model_client = model_client or AliyunModelClient(settings)
        application.state.vector_store = ChromaVectorStore(settings)
        application.state.skill_manager = SkillManager(
            settings.skills_dir, settings.skills_max_prompt_chars
        )
        application.state.skill_manager.load()
        fallback = HybridSearchService(
            application.state.store, application.state.model_client, application.state.vector_store
        ) if application.state.store else None
        application.state.mcp_gateway = mcp_gateway or StandardMCPGateway(
            settings.mcp_url, settings.mcp_timeout_seconds,
            cache=application.state.cache, fallback=fallback,
            max_retries=settings.mcp_max_retries,
            cache_ttl_seconds=settings.mcp_cache_ttl_seconds,
            failure_threshold=settings.mcp_circuit_failure_threshold,
            cooldown_seconds=settings.mcp_circuit_cooldown_seconds,
            metrics=application.state.metrics,
        )
        if application.state.store and application.state.cache:
            recognizer = IntentRecognizer(application.state.model_client, settings.intent_confidence_threshold)
            orchestrator = BankOrchestrator(
                recognizer, application.state.mcp_gateway, application.state.model_client,
                settings.chat_retrieval_top_k, settings.context_rewrite_max_messages,
                application.state.metrics, application.state.skill_manager,
            )
            application.state.memory_service = MemoryService(
                application.state.store, application.state.cache, application.state.vector_store,
                application.state.model_client, settings,
            )
            application.state.job_queue = JobQueue(
                application.state.store, application.state.cache, settings, application.state.metrics
            )
            application.state.chat_service = ChatService(
                application.state.store, application.state.cache, orchestrator,
                application.state.memory_service, application.state.job_queue,
            )
        try:
            yield
        finally:
            if application.state.model_client:
                await application.state.model_client.aclose()
            if application.state.vector_store:
                await application.state.vector_store.close()
            if application.state.cache:
                await application.state.cache.close()
            if application.state.store:
                await application.state.store.close()
    application = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="合成银行对公咨询系统；支持可审计的多 Agent RAG 问答，不执行真实银行交易。",
        lifespan=lifespan,
        docs_url=None if settings.app_env == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "production" else "/openapi.json",
    )
    application.state.settings = settings
    application.state.metrics = MetricsRegistry()
    application.state.store = None
    application.state.cache = None
    application.state.model_client = None
    application.state.vector_store = None
    application.state.mcp_gateway = None
    application.state.chat_service = None
    application.state.skill_manager = None
    application.state.memory_service = None
    application.state.job_queue = None
    application.add_middleware(BodyLimitMiddleware, upload_maximum=settings.max_upload_bytes + 65536)
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(conversations_router)
    application.include_router(documents_router)
    application.include_router(search_router)
    application.include_router(chat_router)
    application.include_router(metrics_router)
    application.include_router(ingestion_router)
    application.include_router(evaluations_router)
    application.include_router(observability_router)
    application.include_router(skills_router)
    application.include_router(memories_router)
    application.include_router(jobs_router)
    frontend = settings.data_dir.parent.parent / "frontend"
    if frontend.exists():
        application.mount("/static", StaticFiles(directory=frontend), name="static")

        @application.get("/", include_in_schema=False)
        async def frontend_index():
            return FileResponse(frontend / "index.html")

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse({"detail": "请求字段或格式不合法"}, status_code=422)

    async def unavailable(request, error):
        return JSONResponse({"detail": "存储依赖暂不可用，请稍后重试"}, status_code=503)
    for error_type in (SQLAlchemyError, RedisError, TimeoutError, OSError):
        application.add_exception_handler(error_type, unavailable)

    @application.middleware("http")
    async def request_tracking(request: Request, call_next):
        request.state.request_id = str(uuid4())
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        route_name = getattr(route, "path", "unmatched")
        duration = time.monotonic() - started
        application.state.metrics.increment(
            "bank_http_requests_total", method=request.method,
            route=route_name, status=str(response.status_code),
        )
        application.state.metrics.observe(
            "bank_http_request_duration_seconds", duration,
            method=request.method, route=route_name,
        )
        logger.info("request id=%s method=%s route=%s status=%d duration_ms=%d",
            request.state.request_id, request.method, route_name,
            response.status_code, int(duration * 1000))
        return response
    return application


app = create_app()
