"""配置只读取服务端环境或项目目录的 .env，不输出秘密。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "银行对公智能客服结合版"
    app_env: Literal["development", "test", "production"] = "development"
    deployment_mode: Literal["native", "container"] = "native"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1024, le=65535)
    enable_paid_model_calls: bool = False
    dashscope_api_key: SecretStr = SecretStr("")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_chat_model: str = "qwen-plus"
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_embedding_dimension: int = Field(default=1024, ge=64, le=2560)
    dashscope_max_retries: int = Field(default=1, ge=0, le=2)
    dashscope_timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
    dashscope_trust_env: bool = False
    data_dir: Path = PROJECT_ROOT / "var" / "data"
    log_dir: Path = PROJECT_ROOT / "var" / "logs"
    database_url: SecretStr = SecretStr("")
    redis_url: SecretStr = SecretStr("")
    cache_prefix: str = "bank:v1"
    token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    login_limit: int = Field(default=5, ge=1, le=20)
    login_window_seconds: int = Field(default=900, ge=60)
    context_ttl_seconds: int = Field(default=1800, ge=60)
    context_message_limit: int = Field(default=12, ge=1, le=40)
    skills_dir: Path = PROJECT_ROOT / "skills"
    skills_max_prompt_chars: int = Field(default=5000, ge=500, le=12000)
    memory_enabled: bool = True
    memory_tenant_id: str = Field(default="demo", min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    memory_summary_collection: str = Field(default="conversation_summaries_v1", min_length=3, max_length=63,
                                           pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$")
    memory_profile_collection: str = Field(default="user_profiles_v1", min_length=3, max_length=63,
                                           pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$")
    memory_summary_threshold_messages: int = Field(default=12, ge=4, le=40)
    memory_summary_increment_messages: int = Field(default=8, ge=2, le=20)
    memory_retrieval_top_k: int = Field(default=3, ge=1, le=5)
    worker_stream_maxlen: int = Field(default=10000, ge=100, le=100000)
    worker_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=10.0)
    chat_rate_limit_per_minute: int = Field(default=30, ge=1, le=300)
    memory_embedding_daily_limit: int = Field(default=500, ge=1, le=100000)
    lock_ttl_seconds: int = Field(default=30, ge=10, le=60)
    dependency_timeout_seconds: float = Field(default=2.0, ge=0.1, le=10)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8002, ge=1024, le=65535)
    mcp_url: str = "http://127.0.0.1:8002/mcp"
    mcp_timeout_seconds: float = Field(default=15.0, ge=1.0, le=30.0)
    mcp_max_retries: int = Field(default=1, ge=0, le=2)
    mcp_cache_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    mcp_circuit_failure_threshold: int = Field(default=3, ge=1, le=10)
    mcp_circuit_cooldown_seconds: int = Field(default=30, ge=1, le=300)
    chat_retrieval_top_k: int = Field(default=4, ge=1, le=5)
    intent_confidence_threshold: float = Field(default=0.58, ge=0.0, le=1.0)
    context_rewrite_max_messages: int = Field(default=6, ge=1, le=12)
    orchestration_backend: Literal["legacy", "langgraph"] = "langgraph"
    langgraph_recursion_limit: int = Field(default=30, ge=10, le=100)
    react_enabled: bool = True
    react_max_steps: int = Field(default=3, ge=1, le=5)
    react_max_tool_calls: int = Field(default=3, ge=1, le=5)
    react_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    react_max_evidence: int = Field(default=8, ge=1, le=20)
    ingestion_job_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    chroma_mode: Literal["http", "persistent"] = "http"
    chroma_host: str = "127.0.0.1"
    chroma_port: int = Field(default=8001, ge=1024, le=65535)
    chroma_collection: str = Field(default="bank_knowledge_v1", min_length=3, max_length=63,
                                   pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$")
    chroma_path: Path = PROJECT_ROOT / "var" / "chroma"

    @field_validator("chroma_path")
    @classmethod
    def require_project_chroma_path(cls, value: Path) -> Path:
        path = value if value.is_absolute() else PROJECT_ROOT / value
        resolved = path.resolve()
        managed_root = (PROJECT_ROOT / "var").resolve()
        if resolved != managed_root and managed_root not in resolved.parents:
            raise ValueError("Chroma 持久化目录必须位于项目 var 目录内")
        return resolved

    @field_validator("skills_dir")
    @classmethod
    def require_project_skills_path(cls, value: Path) -> Path:
        path = value if value.is_absolute() else PROJECT_ROOT / value
        resolved = path.resolve()
        root = PROJECT_ROOT.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("Skills 目录必须位于项目目录内")
        return resolved

    @model_validator(mode="after")
    def require_private_runtime_addresses(self):
        if self.react_max_tool_calls > self.react_max_steps:
            raise ValueError("ReAct 工具调用上限不能大于步骤上限")
        loopback = {"127.0.0.1", "::1"}
        if self.deployment_mode == "native":
            if self.app_host not in loopback or self.mcp_host not in loopback or self.chroma_host not in loopback:
                raise ValueError("原生部署只允许回环地址")
            allowed_mcp_hosts = loopback
        else:
            if self.app_host not in loopback | {"0.0.0.0", "::"}:
                raise ValueError("容器 API 监听地址不合法")
            if self.mcp_host not in loopback | {"0.0.0.0", "::"}:
                raise ValueError("容器 MCP 监听地址不合法")
            if self.chroma_host not in loopback | {"chroma"}:
                raise ValueError("容器 ChromaDB 只能使用内部服务名 chroma 或回环地址")
            allowed_mcp_hosts = loopback | {"mcp"}

        parsed = urlsplit(self.mcp_url)
        if parsed.scheme != "http" or parsed.hostname not in allowed_mcp_hosts or parsed.port != self.mcp_port:
            raise ValueError("MCP 端点必须使用已批准的私有地址和配置端口")
        self.mcp_url = self.mcp_url.rstrip("/")
        return self

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() and not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("持久化数据库必须使用 PostgreSQL asyncpg")
        return value

    @field_validator("dashscope_base_url")
    @classmethod
    def require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("模型端点必须使用 HTTPS")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
