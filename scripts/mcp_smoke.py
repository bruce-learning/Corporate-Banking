"""通过真实 Streamable HTTP 发现并调用银行 MCP 工具。"""

import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import Client

from app.config import Settings


async def run() -> dict:
    settings = Settings()
    async with Client(settings.mcp_url) as client:
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        result = await client.call_tool("product_search", {"query": "企业网银有什么功能", "top_k": 3})
        structured = result.structured_content or {}
        return {
            "protocol_version": client.protocol_version,
            "tools": names,
            "call_is_error": bool(result.is_error),
            "result_mode": structured.get("mode"),
            "vector_backend": structured.get("vector_backend"),
            "result_count": len(structured.get("items") or []),
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2, default=str))
