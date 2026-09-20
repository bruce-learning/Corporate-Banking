import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.core.agents.react import ReActDomainAgent
from app.core.rag.hybrid import Evidence
from app.infrastructure.llm.aliyun import PaidCallsDisabled
from app.infrastructure.llm.contracts import ChatOutput
from app.infrastructure.mcp.gateway import ToolResult


class ScriptedModel:
    def __init__(self, actions: list[str]):
        self.actions = list(actions)
        self.controller_calls = 0
        self.answer_calls = 0

    async def chat(self, request):
        if "ReAct 控制器" in request.messages[0].content:
            self.controller_calls += 1
            return ChatOutput(
                text=self.actions.pop(0), model="fake-react", backend="fake",
                input_tokens=10, output_tokens=4,
            )
        self.answer_calls += 1
        return ChatOutput(
            text="收费和限额以当前资料为准。[资料:react-policy-1] [资料:react-policy-2]",
            model="fake-answer", backend="fake", input_tokens=20, output_tokens=8,
        )


class ReActGateway:
    def __init__(self):
        self.queries: list[tuple[str, str]] = []

    async def search(self, domain: str, query: str, top_k: int = 4):
        self.queries.append((domain, query))
        number = len(self.queries)
        now = datetime.now(timezone.utc)
        item = Evidence(
            id=f"react-{domain}-{number}", document_id=f"react-doc-{number}",
            title=f"ReAct 合成资料 {number}", domain=domain, product="企业网银",
            content=f"第 {number} 条合成资料，仅用于 ReAct 单元测试。",
            source="合成测试", effective_at=now - timedelta(days=1), expires_at=None,
            score=1.0, lexical_rank=1, semantic_rank=1, synthetic=True,
            metadata={"topic": f"react-{number}"},
        )
        return ToolResult(
            domain=domain,
            tool={"product": "product_search", "process": "process_lookup", "policy": "policy_lookup"}[domain],
            success=True, duration_ms=1, mode="hybrid", vector_backend="chromadb",
            degraded=False, items=[item],
        )


async def test_react_performs_bounded_multi_step_search_and_finish():
    model = ScriptedModel([
        '{"action":"search_knowledge","query":"企业网银收费","reason_code":"fee_missing"}',
        '{"action":"search_knowledge","query":"企业网银限额","reason_code":"limit_missing"}',
        '{"action":"finish","query":null,"reason_code":"evidence_sufficient"}',
    ])
    gateway = ReActGateway()
    outcome = await ReActDomainAgent(
        "policy", gateway, model, max_steps=3, max_tool_calls=3, timeout_seconds=5,
    ).run("企业网银收费和限额是什么？", [])

    assert gateway.queries == [
        ("policy", "企业网银收费"), ("policy", "企业网银限额")
    ]
    assert [step.action for step in outcome.steps] == [
        "search_knowledge", "search_knowledge", "finish"
    ]
    assert outcome.draft.generation_mode == "react_llm_grounded"
    assert outcome.draft.degraded is False
    assert model.controller_calls == 3 and model.answer_calls == 1
    assert [row["operation"] for row in outcome.draft.model_usage[:3]] == [
        "react_controller_policy", "react_controller_policy", "react_controller_policy"
    ]


async def test_react_stops_duplicate_search_and_marks_degradation():
    model = ScriptedModel([
        '{"action":"search_knowledge","query":"重复问题","reason_code":"first_search"}',
        '{"action":"search_knowledge","query":"重复问题","reason_code":"retry_same"}',
    ])
    gateway = ReActGateway()
    outcome = await ReActDomainAgent(
        "policy", gateway, model, max_steps=3, max_tool_calls=3, timeout_seconds=5,
    ).run("重复问题", [])

    assert len(gateway.queries) == 1
    assert outcome.steps[-1].reason_code == "duplicate_search_stopped"
    assert outcome.draft.degraded is True
    assert outcome.draft.degradation_reason == "react_duplicate_search"


class PaidCallsOffModel:
    async def chat(self, request):
        raise PaidCallsDisabled()


async def test_react_falls_back_to_one_read_only_search_when_model_is_disabled():
    gateway = ReActGateway()
    outcome = await ReActDomainAgent(
        "product", gateway, PaidCallsOffModel(), max_steps=3, timeout_seconds=5,
    ).run("企业网银有什么功能？", [])

    assert gateway.queries == [("product", "企业网银有什么功能？")]
    assert len(outcome.steps) == 1
    assert outcome.draft.generation_mode == "react_evidence_extract"
    assert outcome.draft.degraded is True
    assert "[资料:react-product-1]" in outcome.draft.answer


class SlowController:
    async def chat(self, request):
        await asyncio.sleep(0.05)
        return ChatOutput(text="{}", model="fake-slow", backend="fake")


async def test_react_timeout_is_bounded_and_explicit():
    gateway = ReActGateway()
    agent = ReActDomainAgent("process", gateway, SlowController(), timeout_seconds=1)
    agent.timeout_seconds = 0.01
    outcome = await agent.run("怎么办理？", [])

    assert gateway.queries == []
    assert outcome.draft.degraded is True
    assert outcome.draft.degradation_reason == "react_timeout"
    assert outcome.draft.generation_mode == "react_evidence_extract"


class FailingGateway:
    async def search(self, domain: str, query: str, top_k: int = 4):
        raise OSError("synthetic tool failure")


async def test_react_tool_failure_is_explicit_and_does_not_escape():
    model = ScriptedModel([
        '{"action":"search_knowledge","query":"办理材料","reason_code":"materials_missing"}',
    ])
    outcome = await ReActDomainAgent(
        "process", FailingGateway(), model, max_steps=3, timeout_seconds=5,
    ).run("需要哪些材料？", [])

    assert outcome.steps[0].success is False
    assert outcome.steps[0].degradation_reason == "react_tool_unavailable"
    assert outcome.draft.degraded is True
    assert outcome.draft.degradation_reason == "react_tool_unavailable"


def test_react_config_rejects_tool_budget_larger_than_step_budget():
    with pytest.raises(ValueError, match="工具调用上限"):
        Settings(_env_file=None, react_max_steps=2, react_max_tool_calls=3)
