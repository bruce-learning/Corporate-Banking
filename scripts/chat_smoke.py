"""经真实 HTTP 主链路执行一次银行问答；不输出令牌、密码或 API Key。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def main():
    password_path = ROOT / "var/secrets/admin-initial-password"
    if not password_path.exists():
        raise RuntimeError("初始管理员凭据文件不存在")
    password = password_path.read_text(encoding="utf-8").strip()
    conversation_id = None
    headers = None
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=90.0) as client:
        login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        created = await client.post("/api/v1/conversations", headers=headers, json={"title": "自动在线验收-完成后删除"})
        created.raise_for_status()
        conversation_id = created.json()["id"]
        try:
            response = await client.post("/api/v1/chat", headers=headers, json={
                "conversation_id": conversation_id,
                "message": "不同产品是怎么收费的？",
            })
            response.raise_for_status()
            body = response.json()
            print(json.dumps({
                "request_id": body["request_id"], "intent": body["intent"],
                "domains": body["domains"], "agents": body["agents"],
                "evidence_count": len(body["evidence"]),
                "evidence_ids": [item["id"] for item in body["evidence"]],
                "tool_modes": [item["mode"] for item in body["tool_calls"]],
                "tool_sources": [item["source"] for item in body["tool_calls"]],
                "tool_attempts": [item["attempts"] for item in body["tool_calls"]],
                "circuit_states": [item["circuit_state"] for item in body["tool_calls"]],
                "vector_backends": [item["vector_backend"] for item in body["tool_calls"]],
                "degraded": body["degraded"],
                "degradation_reasons": body["degradation_reasons"],
                "generation_mode": body["generation_mode"], "model": body["model"],
                "has_citations": "[资料:" in body["answer"],
            }, ensure_ascii=False, indent=2))
        finally:
            if conversation_id and headers:
                await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)


if __name__ == "__main__":
    asyncio.run(main())
