import asyncio
from datetime import datetime, timedelta, timezone

from app.core.intent.recognizer import IntentDecision
from app.core.orchestration.langgraph import LangGraphBankOrchestrator
from app.core.rag.hybrid import Evidence
from app.infrastructure.mcp.gateway import ToolResult


class StaticRecognizer:
    def __init__(self, label: str, domains: list[str]):
        self.label = label
        self.domains = domains

    async def recognize(self, message: str):
        return IntentDecision(label=self.label, confidence=0.99, domains=self.domains)


class ConcurrentGateway:
    def __init__(self):
        self.calls: list[str] = []
        self.active = 0
        self.maximum_active = 0

    async def search(self, domain: str, query: str, top_k: int = 4):
        self.calls.append(domain)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        now = datetime.now(timezone.utc)
        evidence = Evidence(
            id=f"graph-{domain}", document_id=f"graph-doc-{domain}",
            title=f"{domain} 合成资料", domain=domain, product="企业网银",
            content=f"{domain} 领域合成资料，仅用于 LangGraph 单元测试。",
            source="合成测试", effective_at=now - timedelta(days=1), expires_at=None,
            score=1.0, lexical_rank=1, semantic_rank=1, synthetic=True,
            metadata={"topic": domain},
        )
        return ToolResult(
            domain=domain,
            tool={"product": "product_search", "process": "process_lookup", "policy": "policy_lookup"}[domain],
            success=True, duration_ms=1, mode="hybrid", vector_backend="chromadb",
            degraded=False, items=[evidence],
        )


async def test_langgraph_routes_single_domain_and_exposes_node_trace():
    gateway = ConcurrentGateway()
    orchestrator = LangGraphBankOrchestrator(
        StaticRecognizer("policy", ["policy"]), gateway, react_enabled=False,
    )
    result = await orchestrator.run("企业网银怎么收费？", [])

    assert result.orchestration_backend == "langgraph"
    assert result.execution_mode == "fixed"
    assert gateway.calls == ["policy"]
    assert [item["node"] for item in result.graph_trace] == [
        "risk_guard", "rewrite_query", "recognize_intent", "build_plan",
        "prepare_domain", "execute_domain", "aggregate",
    ]
    assert result.graph_trace[4]["domain"] == "policy"


async def test_langgraph_parallel_send_runs_three_domain_subgraphs_concurrently():
    gateway = ConcurrentGateway()
    orchestrator = LangGraphBankOrchestrator(
        StaticRecognizer("composite", ["product", "process", "policy"]),
        gateway, react_enabled=False,
    )
    result = await orchestrator.run("企业网银的功能、材料和收费是什么？", [])

    assert result.execution_mode == "parallel"
    assert set(result.agents) == {"product", "process", "policy"}
    assert set(gateway.calls) == {"product", "process", "policy"}
    assert gateway.maximum_active == 3
    assert len(result.evidence) == 3
    assert [item["domain"] for item in result.graph_trace if item["node"] == "execute_domain"] == [
        "product", "policy", "process",
    ]


async def test_langgraph_plan_execute_preserves_dependency_order():
    gateway = ConcurrentGateway()
    orchestrator = LangGraphBankOrchestrator(
        StaticRecognizer("composite", ["product", "policy", "process"]),
        gateway, react_enabled=False,
    )
    result = await orchestrator.run("请先介绍功能，再说明收费，然后给出办理步骤", [])

    assert result.execution_mode == "plan_execute"
    assert gateway.calls == ["product", "policy", "process"]
    assert [item["status"] for item in result.plan_steps] == ["degraded", "degraded", "degraded"]


async def test_langgraph_safety_guard_stops_before_agents():
    gateway = ConcurrentGateway()
    orchestrator = LangGraphBankOrchestrator(
        StaticRecognizer("policy", ["policy"]), gateway, react_enabled=False,
    )
    result = await orchestrator.run("我的验证码是123456，请帮我办理", [])

    assert result.generation_mode == "hard_rule"
    assert gateway.calls == []
    assert [item["node"] for item in result.graph_trace] == ["risk_guard", "blocked_response"]
    assert result.orchestration_backend == "langgraph"
