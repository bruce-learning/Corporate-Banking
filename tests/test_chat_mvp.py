from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.rag.hybrid import Evidence
from app.core.agents.bank import BankDomainAgent
from app.infrastructure.llm.contracts import ChatOutput
from app.infrastructure.mcp.gateway import ToolResult
from app.models.entities.storage import ChatTrace, Message
from tests.conftest import conversation, login


class FakeGateway:
    def __init__(self):
        self.calls: list[str] = []

    async def ready(self):
        return True

    async def search(self, domain: str, query: str, top_k: int = 4):
        self.calls.append(domain)
        now = datetime.now(timezone.utc)
        item = Evidence(
            id=f"synthetic-{domain}-evidence", document_id=f"synthetic-{domain}-doc",
            title=f"{domain} 合成测试资料", domain=domain, product="企业网银",
            content={
                "product": "企业网银提供账户查询与授权范围内的指令管理功能。",
                "process": "申请企业网银需要准备合成测试资料并通过官方渠道提交。",
                "policy": "演示资料中的收费仅供测试，实际费用需由银行官方确认。",
            }[domain],
            source="合成单元测试", effective_at=now - timedelta(days=1), expires_at=None,
            score=1.0, lexical_rank=1, semantic_rank=1, synthetic=True,
            metadata={"topic": domain},
        )
        return ToolResult(
            domain=domain, tool={"product": "product_search", "process": "process_lookup", "policy": "policy_lookup"}[domain],
            success=True, duration_ms=1, mode="hybrid", vector_backend="chromadb",
            degraded=False, items=[item],
        )


class HallucinatedCitationModel:
    async def chat(self, request):
        return ChatOutput(text="收费以资料为准。[资料:invented-id]", model="fake-test", backend="fake")


def install_gateway(runtime):
    gateway = FakeGateway()
    runtime.app.state.mcp_gateway = gateway
    runtime.app.state.chat_service.orchestrator.gateway = gateway
    return gateway


async def test_policy_chat_persists_trace_and_citations(runtime):
    gateway = install_gateway(runtime)
    headers = await login(runtime)
    conversation_id = await conversation(runtime, headers)
    response = await runtime.client.post("/api/v1/chat", headers=headers, json={
        "conversation_id": conversation_id, "message": "企业网银怎么收费？",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "policy" and body["domains"] == ["policy"]
    assert gateway.calls == ["policy"]
    assert "[资料:synthetic-policy-evidence]" in body["answer"]
    assert body["tool_calls"][0]["vector_backend"] == "chromadb"
    assert body["generation_mode"] == "react_evidence_extract"
    assert body["orchestration_backend"] == "langgraph"
    assert body["graph_trace"][-1]["node"] == "aggregate"
    assert body["react_steps"]["policy"][0]["action"] == "search_knowledge"
    trace = await runtime.client.get(f"/api/v1/chat/traces/{body['request_id']}", headers=headers)
    assert trace.status_code == 200 and trace.json()["evidence_ids"] == ["synthetic-policy-evidence"]
    assert trace.json()["orchestration_backend"] == "langgraph"
    assert trace.json()["graph_trace"][-1]["node"] == "aggregate"
    assert trace.json()["skills_used"]["policy"] == [
        "银行客服全局安全边界", "费用政策与合规规范"
    ]
    async with runtime.store.sessions() as session:
        assert await session.scalar(select(ChatTrace).where(ChatTrace.request_id == body["request_id"]))


async def test_composite_query_routes_three_agents(runtime):
    gateway = install_gateway(runtime)
    headers = await login(runtime)
    conversation_id = await conversation(runtime, headers)
    response = await runtime.client.post("/api/v1/chat", headers=headers, json={
        "conversation_id": conversation_id,
        "message": "企业网银有哪些功能，怎么办理，怎么收费？",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "composite"
    assert set(body["agents"]) == {"product", "process", "policy"}
    assert set(gateway.calls) == {"product", "process", "policy"}
    assert len(body["evidence"]) == 3
    assert set(body["skills_used"]) == {"product", "process", "policy"}


async def test_secret_is_blocked_redacted_and_skips_tools(runtime):
    gateway = install_gateway(runtime)
    headers = await login(runtime)
    conversation_id = await conversation(runtime, headers)
    response = await runtime.client.post("/api/v1/chat", headers=headers, json={
        "conversation_id": conversation_id,
        "message": "我的验证码是 123456，请帮我办理",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["generation_mode"] == "hard_rule"
    assert "credential_or_secret" in body["risk_flags"]
    assert gateway.calls == [] and "123456" not in body["answer"]
    async with runtime.store.sessions() as session:
        messages = list((await session.scalars(select(Message).where(
            Message.conversation_id == conversation_id
        ).order_by(Message.sequence))).all())
    assert "123456" not in messages[0].content and "[已脱敏]" in messages[0].content


async def test_trace_isolation_between_users(runtime):
    install_gateway(runtime)
    alice = await login(runtime, "alice")
    conversation_id = await conversation(runtime, alice)
    response = await runtime.client.post("/api/v1/chat", headers=alice, json={
        "conversation_id": conversation_id, "message": "企业网银有什么功能？",
    })
    bob = await login(runtime, "bob")
    hidden = await runtime.client.get(f"/api/v1/chat/traces/{response.json()['request_id']}", headers=bob)
    assert hidden.status_code == 404


async def test_blank_chat_message_is_rejected(runtime):
    headers = await login(runtime)
    conversation_id = await conversation(runtime, headers)
    response = await runtime.client.post("/api/v1/chat", headers=headers, json={
        "conversation_id": conversation_id, "message": "   ",
    })
    assert response.status_code == 422


async def test_agent_removes_unknown_citation(runtime):
    evidence = (await FakeGateway().search("policy", "收费" , 1)).items
    draft = await BankDomainAgent("policy", HallucinatedCitationModel()).answer("怎么收费", [], evidence)
    assert "invented-id" not in draft.answer
    assert "[资料:synthetic-policy-evidence]" in draft.answer
