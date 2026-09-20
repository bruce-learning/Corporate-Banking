"""Verify the Docker Compose API and standard MCP path from the private network."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp import Client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402


async def main() -> None:
    settings = Settings()
    api_url = os.environ.get("RUNTIME_API_URL", f"http://127.0.0.1:{settings.app_port}").rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        health = await client.get(api_url + "/health")
        ready = await client.get(api_url + "/ready")
        health.raise_for_status()
        ready.raise_for_status()
    async with Client(settings.mcp_url) as mcp:
        listed = await mcp.list_tools()
        tools = sorted(item.name for item in listed.tools)
    expected = ["policy_lookup", "process_lookup", "product_search"]
    if tools != expected:
        raise RuntimeError(f"MCP 工具不完整：{tools}")
    print(json.dumps({
        "compose_runtime": "passed",
        "ready": ready.json().get("status"),
        "mcp_tools": tools,
    }, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
