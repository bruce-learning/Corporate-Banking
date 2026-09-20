"""阿里云百炼 OpenAI 兼容客户端。

仅服务端读取密钥；异常信息不包含请求头、密钥或完整业务正文。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.infrastructure.llm.contracts import ChatInput, ChatOutput, EmbeddingOutput


ERROR_CATEGORIES = {
    400: "invalid_input",
    401: "authentication",
    403: "authentication",
    408: "timeout",
    422: "invalid_input",
    429: "rate_limit",
}


@dataclass(frozen=True)
class ModelServiceError(RuntimeError):
    category: str
    status_code: int | None
    retryable: bool
    message: str

    def __str__(self) -> str:
        return self.message


class PaidCallsDisabled(ModelServiceError):
    def __init__(self) -> None:
        super().__init__("invalid_input", None, False, "真实模型调用开关未启用")


class AliyunModelClient:
    """有界重试、显式模型和维度的异步客户端。"""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.dashscope_base_url + "/",
            headers={
                "Authorization": "Bearer " + settings.dashscope_api_key.get_secret_value(),
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(settings.dashscope_timeout_seconds),
            transport=transport,
            trust_env=settings.dashscope_trust_env,
        )

    def _guard(self) -> None:
        if not self.settings.enable_paid_model_calls:
            raise PaidCallsDisabled()
        if not self.settings.dashscope_api_key.get_secret_value():
            raise ModelServiceError("authentication", None, False, "DASHSCOPE_API_KEY 未配置")

    async def chat(self, request: ChatInput) -> ChatOutput:
        self._guard()
        payload = {
            "model": self.settings.dashscope_chat_model,
            "messages": [item.model_dump() for item in request.messages],
            "temperature": 0.0,
            "max_tokens": request.max_tokens,
        }
        data = await self._post("chat/completions", payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content", "")
            if isinstance(text, list):
                text = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in text
                )
            usage = data.get("usage") or {}
            return ChatOutput(
                text=str(text).strip(),
                model=str(data.get("model") or self.settings.dashscope_chat_model),
                backend="aliyun",
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                finish_reason=str(choice.get("finish_reason") or "stop"),
            )
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ModelServiceError("transient", None, False, "聊天模型响应格式无效") from error

    async def embed(self, texts: list[str]) -> EmbeddingOutput:
        self._guard()
        if not texts or len(texts) > 10:
            raise ValueError("text-embedding-v4 每批必须为 1 至 10 条")
        if any(not isinstance(text, str) or not text.strip() or len(text) > 32000 for text in texts):
            raise ValueError("Embedding 输入为空或过长")
        payload = {
            "model": self.settings.dashscope_embedding_model,
            "input": texts,
            "dimensions": self.settings.dashscope_embedding_dimension,
            "encoding_format": "float",
        }
        data = await self._post("embeddings", payload)
        try:
            rows = sorted(data["data"], key=lambda item: int(item["index"]))
            vectors = [list(map(float, item["embedding"])) for item in rows]
            if len(vectors) != len(texts):
                raise ValueError("向量数量不一致")
            if any(len(vector) != self.settings.dashscope_embedding_dimension for vector in vectors):
                raise ValueError("向量维度不一致")
            usage = data.get("usage") or {}
            return EmbeddingOutput(
                vectors=vectors,
                model=str(data.get("model") or self.settings.dashscope_embedding_model),
                backend="aliyun",
                input_tokens=int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ModelServiceError("transient", None, False, "Embedding 响应格式无效") from error

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: ModelServiceError | None = None
        for attempt in range(self.settings.dashscope_max_retries + 1):
            try:
                response = await self._client.post(path, json=payload)
                if response.is_success:
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError("响应不是 JSON 对象")
                    return body
                category = ERROR_CATEGORIES.get(
                    response.status_code,
                    "transient" if response.status_code >= 500 else "invalid_input",
                )
                retryable = response.status_code == 429 or response.status_code >= 500
                error = ModelServiceError(
                    category,
                    response.status_code,
                    retryable,
                    f"阿里云模型请求失败（HTTP {response.status_code}，类别 {category}）",
                )
            except httpx.TimeoutException:
                error = ModelServiceError("timeout", None, True, "阿里云模型请求超时")
            except httpx.HTTPError:
                error = ModelServiceError("transient", None, True, "阿里云模型网络请求失败")
            except ValueError:
                error = ModelServiceError("transient", None, False, "阿里云模型返回无效 JSON")

            last_error = error
            if not error.retryable or attempt >= self.settings.dashscope_max_retries:
                raise error
            await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        raise last_error or ModelServiceError("transient", None, False, "阿里云模型请求失败")

    async def aclose(self) -> None:
        await self._client.aclose()
