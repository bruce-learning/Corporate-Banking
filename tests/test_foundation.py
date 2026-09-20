import httpx
import pytest
from pydantic import ValidationError
from app.config import Settings
from app.main import create_app
from app.models.enums import Domain, ExecutionMode


def make_settings(**changes):
    return Settings(_env_file=None, database_url="", redis_url="", **changes)


async def request(path, method="GET", settings=None, **kwargs):
    application = create_app(settings or make_settings())
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)


async def test_health():
    response = await request("/health")
    assert response.status_code == 200 and response.json()["stage"] == "fullstack_demo"


async def test_readiness_requires_configured_dependencies():
    response = await request("/ready")
    assert response.status_code == 503 and response.json()["status"] == "not_ready"


async def test_chat_requires_authentication():
    assert (await request("/api/v1/chat", "POST", json={
        "conversation_id": "00000000-0000-0000-0000-000000000000", "message": "怎么收费"
    })).status_code == 401


def test_paid_calls_disabled(monkeypatch):
    monkeypatch.delenv("ENABLE_PAID_MODEL_CALLS", raising=False)
    assert make_settings().enable_paid_model_calls is False


def test_secret_redaction():
    config = make_settings(dashscope_api_key="test-secret-not-a-real-key")
    assert "test-secret-not-a-real-key" not in repr(config)
    assert "test-secret-not-a-real-key" not in config.model_dump_json()


@pytest.mark.parametrize("host", ["0.0.0.0", "example.com"])
def test_public_binding_rejected(host):
    with pytest.raises(ValidationError):
        make_settings(app_host=host)


def test_container_private_service_names_allowed():
    settings = make_settings(
        deployment_mode="container",
        app_host="0.0.0.0",
        mcp_host="0.0.0.0",
        mcp_url="http://mcp:8002/mcp",
        chroma_host="chroma",
    )
    assert settings.mcp_url == "http://mcp:8002/mcp"


@pytest.mark.parametrize("field,value", [
    ("mcp_url", "http://public.example:8002/mcp"),
    ("chroma_host", "public.example"),
])
def test_container_public_dependencies_rejected(field, value):
    changes = {
        "deployment_mode": "container",
        "app_host": "0.0.0.0",
        "mcp_host": "0.0.0.0",
        "mcp_url": "http://mcp:8002/mcp",
        "chroma_host": "chroma",
        field: value,
    }
    with pytest.raises(ValidationError):
        make_settings(**changes)


def test_insecure_model_endpoint_rejected():
    with pytest.raises(ValidationError):
        make_settings(dashscope_base_url="http://example.com/v1")


async def test_production_docs_disabled():
    settings = make_settings(app_env="production")
    assert (await request("/docs", settings=settings)).status_code == 404
    assert (await request("/openapi.json", settings=settings)).status_code == 404


def test_roles_and_execution_modes_are_separate():
    assert {item.value for item in Domain} == {"product", "process", "policy"}
    assert {item.value for item in ExecutionMode} == {"fixed", "parallel", "plan_execute"}
