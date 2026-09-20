from typing import Literal, Protocol
from pydantic import BaseModel, Field


class ModelMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatInput(BaseModel):
    messages: list[ModelMessage] = Field(min_length=1, max_length=20)
    max_tokens: int = Field(default=512, ge=1, le=2048)


class ToolCall(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    arguments: dict = Field(default_factory=dict)


class ChatOutput(BaseModel):
    text: str
    model: str
    backend: Literal["fake", "aliyun"]
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"
    tool_calls: list[ToolCall] = Field(default_factory=list)
    error_category: Literal["authentication", "rate_limit", "timeout", "transient", "invalid_input"] | None = None


class EmbeddingOutput(BaseModel):
    vectors: list[list[float]]
    model: str
    backend: Literal["fake", "aliyun"]
    input_tokens: int = 0


class ModelClient(Protocol):
    async def chat(self, request: ChatInput) -> ChatOutput: ...
    async def embed(self, texts: list[str]) -> EmbeddingOutput: ...
    async def aclose(self) -> None: ...


class FakeModelClient:
    """仅测试；固定向量不得冒充真实语义检索。"""
    async def chat(self, request: ChatInput) -> ChatOutput:
        return ChatOutput(text="测试客户端响应，不是银行业务答案", model="fake-test", backend="fake")
    async def embed(self, texts: list[str]) -> EmbeddingOutput:
        if not texts or len(texts) > 20 or any(not text or len(text) > 8000 for text in texts):
            raise ValueError("测试向量输入不合法")
        return EmbeddingOutput(vectors=[[1.0, 0.0] for _ in texts], model="fake-test", backend="fake")
    async def aclose(self):
        pass
