"""真实复杂任务在线验收；不输出密码、令牌或 API Key。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]


async def main():
    password = (ROOT / "var/secrets/admin-initial-password").read_text(encoding="utf-8").strip()
    conversation_id = None
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=120) as client:
        login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        created = await client.post("/api/v1/conversations", headers=headers,
                                    json={"title": "复杂编排在线验收-完成后删除"})
        created.raise_for_status(); conversation_id = created.json()["id"]
        try:
            response = await client.post("/api/v1/chat", headers=headers, json={
                "conversation_id": conversation_id,
                "message": "请先说明企业网银的功能，再说明收费限制，之后给出办理步骤",
            })
            response.raise_for_status(); body = response.json()
            print(json.dumps({
                "request_id": body["request_id"], "intent": body["intent"],
                "execution_mode": body["execution_mode"],
                "plan_order": [step["domain"] for step in body["plan_steps"]],
                "plan_status": [step["status"] for step in body["plan_steps"]],
                "tool_sources": [call["source"] for call in body["tool_calls"]],
                "tool_modes": [call["mode"] for call in body["tool_calls"]],
                "vector_backends": [call["vector_backend"] for call in body["tool_calls"]],
                "evidence_count": len(body["evidence"]),
                "citation_valid": body["citation_valid"],
                "citation_coverage": body["citation_coverage"],
                "degraded": body["degraded"], "degradation_reasons": body["degradation_reasons"],
                "generation_mode": body["generation_mode"], "model": body["model"],
            }, ensure_ascii=False, indent=2))
        finally:
            if conversation_id:
                await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)


if __name__ == "__main__":
    asyncio.run(main())
