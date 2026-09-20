from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Literal

from mcp import Client
from pydantic import BaseModel, Field

from app.core.rag.hybrid import Evidence


TOOLS = {
    "product": "product_search",
    "process": "process_lookup",
    "policy": "policy_lookup",
}


class ToolResult(BaseModel):
    domain: Literal["product", "process", "policy"]
    tool: str
    success: bool
    duration_ms: int
    mode: str = "unavailable"
    vector_backend: str = "none"
    degraded: bool = True
    degradation_reason: str | None = None
    attempts: int = 1
    cache_hit: bool = False
    circuit_state: Literal["closed", "open", "half_open"] = "closed"
    source: Literal["mcp", "cache", "local_fallback"] = "mcp"
    items: list[Evidence] = Field(default_factory=list)


def _structured(result) -> dict:
    if result.structured_content and isinstance(result.structured_content, dict):
        return result.structured_content
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise ValueError("MCP 工具未返回结构化对象")


class StandardMCPGateway:
    """仅允许调用三项银行知识工具，参数和超时均有上界。"""

    def __init__(self, url: str, timeout_seconds: float = 15.0, *, cache=None, fallback=None,
                 max_retries: int = 1, cache_ttl_seconds: int = 300,
                 failure_threshold: int = 3, cooldown_seconds: int = 30, metrics=None):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.fallback = fallback
        self.max_retries = max_retries
        self.cache_ttl_seconds = cache_ttl_seconds
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.metrics = metrics
        self._circuits = {tool: {"failures": 0, "opened_at": 0.0} for tool in TOOLS.values()}

    def _cache_key(self, domain: str, query: str, top_k: int) -> str:
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
        return self.cache.key("mcp", domain, digest, str(top_k))

    def _circuit_state(self, tool: str) -> str:
        state = self._circuits[tool]
        if not state["opened_at"]:
            return "closed"
        if time.monotonic() - state["opened_at"] >= self.cooldown_seconds:
            return "half_open"
        return "open"

    async def _call_remote(self, tool: str, query: str, top_k: int):
        async with Client(self.url) as client:
            return await client.call_tool(tool, {"query": query, "top_k": top_k})

    async def _fallback(self, domain: str, query: str, top_k: int, reason: str,
                        started: float, attempts: int, circuit_state: str) -> ToolResult:
        if self.fallback is not None:
            try:
                data = await self.fallback.search(query, domain=domain, top_k=top_k)
                result = ToolResult(
                    domain=domain, tool=TOOLS[domain], success=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    mode=data.mode, vector_backend=data.vector_backend,
                    degraded=True, degradation_reason=reason, attempts=attempts,
                    circuit_state=circuit_state, source="local_fallback", items=data.items,
                )
                self._metric(result)
                return result
            except Exception:
                reason += "+fallback_unavailable"
        result = ToolResult(
            domain=domain, tool=TOOLS[domain], success=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            degradation_reason=reason, attempts=attempts,
            circuit_state=circuit_state,
        )
        self._metric(result)
        return result

    def _metric(self, result: ToolResult):
        if not self.metrics:
            return
        self.metrics.increment(
            "bank_mcp_calls_total", tool=result.tool,
            outcome="success" if result.success else "failure", source=result.source,
        )
        self.metrics.observe("bank_mcp_duration_seconds", result.duration_ms / 1000, tool=result.tool)

    async def ready(self) -> bool:
        async def check():
            async with Client(self.url) as client:
                result = await client.list_tools()
                return set(TOOLS.values()).issubset({item.name for item in result.tools})
        return bool(await asyncio.wait_for(check(), self.timeout_seconds))

    async def search(self, domain: str, query: str, top_k: int = 4) -> ToolResult:
        if domain not in TOOLS:
            raise ValueError("MCP 领域不在白名单")
        query = query.strip()
        if not query or len(query) > 500:
            raise ValueError("MCP 检索问题为空或过长")
        top_k = max(1, min(int(top_k), 5))
        tool = TOOLS[domain]
        started = time.monotonic()
        cache_key = self._cache_key(domain, query, top_k) if self.cache and self.cache_ttl_seconds else None
        if cache_key:
            cached = await self.cache.client.get(cache_key)
            if cached:
                result = ToolResult.model_validate_json(cached)
                result.cache_hit = True
                result.source = "cache"
                result.duration_ms = int((time.monotonic() - started) * 1000)
                self._metric(result)
                return result
        state = self._circuit_state(tool)
        if state == "open":
            return await self._fallback(domain, query, top_k, "mcp_circuit_open", started, 0, state)
        last_reason = "mcp_unavailable"
        attempts = 0
        for attempts in range(1, self.max_retries + 2):
            try:
                raw = await asyncio.wait_for(self._call_remote(tool, query, top_k), self.timeout_seconds)
                if raw.is_error:
                    raise RuntimeError("MCP tool error")
                data = _structured(raw)
                result = ToolResult(
                    domain=domain, tool=tool, success=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    mode=str(data.get("mode") or "unknown"),
                    vector_backend=str(data.get("vector_backend") or "none"),
                    degraded=bool(data.get("degraded")),
                    degradation_reason=data.get("degradation_reason"), attempts=attempts,
                    circuit_state=state, source="mcp",
                    items=[Evidence.model_validate(item) for item in data.get("items") or []],
                )
                self._circuits[tool] = {"failures": 0, "opened_at": 0.0}
                if cache_key and result.success:
                    await self.cache.client.set(cache_key, result.model_dump_json(), ex=self.cache_ttl_seconds)
                self._metric(result)
                return result
            except asyncio.TimeoutError:
                last_reason = "mcp_timeout"
            except ValueError:
                last_reason = "mcp_invalid_response"
                break
            except Exception:
                last_reason = "mcp_unavailable"
            if attempts <= self.max_retries:
                await asyncio.sleep(min(0.1 * attempts, 0.25))
        circuit = self._circuits[tool]
        circuit["failures"] += 1
        if circuit["failures"] >= self.failure_threshold:
            circuit["opened_at"] = time.monotonic()
        return await self._fallback(
            domain, query, top_k, last_reason, started, attempts, self._circuit_state(tool)
        )
