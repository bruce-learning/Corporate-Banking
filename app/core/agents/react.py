from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.core.agents.bank import AgentDraft, BankDomainAgent, DOMAIN_NAMES, extractive_answer
from app.core.rag.hybrid import Evidence
from app.infrastructure.llm.aliyun import ModelServiceError, PaidCallsDisabled
from app.infrastructure.llm.contracts import ChatInput, ModelMessage


class ReActAction(BaseModel):
    """模型只能选择只读检索或结束，不能直接声明其他工具。"""

    action: Literal["search_knowledge", "finish"]
    query: str | None = Field(default=None, max_length=500)
    reason_code: str = Field(default="unspecified", min_length=1, max_length=80,
                             pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def require_search_query(self):
        if self.action == "search_knowledge" and not (self.query or "").strip():
            raise ValueError("检索动作必须包含非空 query")
        if self.query is not None:
            self.query = " ".join(self.query.split())
        return self


class ReActStep(BaseModel):
    index: int = Field(ge=1)
    action: Literal["search_knowledge", "finish"]
    query: str | None = None
    reason_code: str
    tool: str | None = None
    success: bool | None = None
    degraded: bool = False
    degradation_reason: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    new_evidence_ids: list[str] = Field(default_factory=list)


class ReActOutcome(BaseModel):
    draft: AgentDraft
    tool_results: list = Field(default_factory=list)
    steps: list[ReActStep] = Field(default_factory=list)


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("ReAct 控制器未返回 JSON 对象")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("ReAct 控制器返回值不是对象")
    return value


class ReActDomainAgent:
    """领域内有界 ReAct：只读补查、去重、超时和显式降级。"""

    def __init__(self, domain: str, gateway, model_client=None, skill_manager=None, *,
                 top_k: int = 4, max_steps: int = 3, max_tool_calls: int = 3,
                 timeout_seconds: float = 15.0, max_evidence: int = 8, metrics=None):
        if domain not in DOMAIN_NAMES:
            raise ValueError("未知银行领域")
        self.domain = domain
        self.gateway = gateway
        self.model_client = model_client
        self.skill_manager = skill_manager
        self.top_k = max(1, min(int(top_k), 5))
        self.max_steps = max(1, min(int(max_steps), 5))
        self.max_tool_calls = max(1, min(int(max_tool_calls), self.max_steps))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_evidence = max(1, min(int(max_evidence), 20))
        self.metrics = metrics

    async def run(self, question: str, context: list[dict]) -> ReActOutcome:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        steps: list[ReActStep] = []
        tool_results: list = []
        evidence_by_id: dict[str, Evidence] = {}
        seen_queries: set[str] = set()
        planner_usage: list[dict] = []
        controller_degradation: str | None = None
        tool_calls = 0
        stagnant_calls = 0

        for index in range(1, self.max_steps + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                controller_degradation = "react_timeout"
                break
            try:
                action, usage = await asyncio.wait_for(
                    self._decide(question, context, list(evidence_by_id.values()), steps),
                    timeout=remaining,
                )
                planner_usage.append(usage)
            except PaidCallsDisabled:
                action = ReActAction(
                    action="search_knowledge", query=question,
                    reason_code="planner_paid_calls_disabled",
                )
                controller_degradation = "react_planner_paid_calls_disabled"
            except ModelServiceError as error:
                action = ReActAction(
                    action="search_knowledge", query=question,
                    reason_code="planner_model_unavailable",
                )
                controller_degradation = "react_planner_" + error.category
            except asyncio.TimeoutError:
                controller_degradation = "react_timeout"
                break
            except Exception:
                action = ReActAction(
                    action="search_knowledge", query=question,
                    reason_code="planner_invalid_output",
                )
                controller_degradation = "react_planner_invalid_output"

            if action.action == "finish":
                if evidence_by_id:
                    steps.append(ReActStep(
                        index=index, action="finish", reason_code=action.reason_code,
                        evidence_ids=list(evidence_by_id),
                    ))
                    break
                action = ReActAction(
                    action="search_knowledge", query=question,
                    reason_code="finish_without_evidence",
                )

            query = " ".join((action.query or question).split())
            if query in seen_queries:
                steps.append(ReActStep(
                    index=index, action="finish", query=query,
                    reason_code="duplicate_search_stopped",
                    evidence_ids=list(evidence_by_id),
                ))
                if not controller_degradation:
                    controller_degradation = "react_duplicate_search"
                break
            if tool_calls >= self.max_tool_calls:
                steps.append(ReActStep(
                    index=index, action="finish", query=query,
                    reason_code="tool_budget_exhausted",
                    evidence_ids=list(evidence_by_id),
                ))
                controller_degradation = controller_degradation or "react_tool_budget_exhausted"
                break

            seen_queries.add(query)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                controller_degradation = "react_timeout"
                break
            call_started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    self.gateway.search(self.domain, query, self.top_k), timeout=remaining
                )
            except asyncio.TimeoutError:
                controller_degradation = "react_timeout"
                steps.append(ReActStep(
                    index=index, action="search_knowledge", query=query,
                    reason_code=action.reason_code, success=False, degraded=True,
                    degradation_reason="react_timeout",
                    duration_ms=int((time.monotonic() - call_started) * 1000),
                    evidence_ids=list(evidence_by_id),
                ))
                break
            except Exception:
                controller_degradation = "react_tool_unavailable"
                steps.append(ReActStep(
                    index=index, action="search_knowledge", query=query,
                    reason_code=action.reason_code, success=False, degraded=True,
                    degradation_reason="react_tool_unavailable",
                    duration_ms=int((time.monotonic() - call_started) * 1000),
                    evidence_ids=list(evidence_by_id),
                ))
                break
            tool_calls += 1
            tool_results.append(result)
            new_ids = []
            for item in result.items if result.success else []:
                if item.id not in evidence_by_id and len(evidence_by_id) < self.max_evidence:
                    evidence_by_id[item.id] = item
                    new_ids.append(item.id)
            steps.append(ReActStep(
                index=index, action="search_knowledge", query=query,
                reason_code=action.reason_code, tool=result.tool,
                success=result.success, degraded=result.degraded,
                degradation_reason=result.degradation_reason,
                duration_ms=result.duration_ms,
                evidence_ids=list(evidence_by_id), new_evidence_ids=new_ids,
            ))
            if self.metrics:
                self.metrics.increment(
                    "bank_react_actions_total", domain=self.domain,
                    action="search_knowledge",
                    outcome="success" if result.success else "failure",
                )
            stagnant_calls = 0 if new_ids else stagnant_calls + 1
            if controller_degradation or stagnant_calls >= 2:
                controller_degradation = controller_degradation or "react_no_new_evidence"
                break

        evidence = list(evidence_by_id.values())
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                draft = await asyncio.wait_for(
                    BankDomainAgent(self.domain, self.model_client, self.skill_manager).answer(
                        question, context, evidence
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                controller_degradation = "react_timeout"
                draft = self._extractive_draft(question, evidence, "react_timeout")
            except Exception:
                controller_degradation = controller_degradation or "react_generation_unavailable"
                draft = self._extractive_draft(question, evidence, controller_degradation)
        else:
            controller_degradation = "react_timeout"
            draft = self._extractive_draft(question, evidence, "react_timeout")

        draft.generation_mode = "react_" + draft.generation_mode
        draft.model_usage = planner_usage + draft.model_usage
        if controller_degradation:
            draft.degraded = True
            # Preserve the more specific ReAct failure instead of replacing it
            # with a downstream generic fallback such as ``no_evidence``.
            draft.degradation_reason = controller_degradation
        if self.metrics:
            self.metrics.observe(
                "bank_react_duration_seconds", time.monotonic() - started, domain=self.domain
            )
            self.metrics.increment(
                "bank_react_runs_total", domain=self.domain,
                outcome="degraded" if draft.degraded else "success",
            )
        return ReActOutcome(draft=draft, tool_results=tool_results, steps=steps)

    async def _decide(self, question: str, context: list[dict], evidence: list[Evidence],
                      steps: list[ReActStep]) -> tuple[ReActAction, dict]:
        if self.model_client is None:
            raise RuntimeError("ReAct 控制器没有模型客户端")
        recent = "\n".join(
            f"{row.get('role', 'unknown')}：{str(row.get('content') or '')[:500]}"
            for row in context[-4:]
        ) or "无"
        observations = "\n".join(
            f"- {item.id} | {item.title} | {' '.join(item.content.split())[:180]}"
            for item in evidence
        ) or "暂无证据"
        previous = "\n".join(
            f"- step={item.index} action={item.action} query={item.query or '-'} "
            f"new_evidence={','.join(item.new_evidence_ids) or '-'}"
            for item in steps
        ) or "暂无动作"
        output = await self.model_client.chat(ChatInput(messages=[
            ModelMessage(role="system", content=(
                f"你是银行对公智能客服{DOMAIN_NAMES[self.domain]}领域的 ReAct 控制器。"
                "你不回答业务问题，也不输出思维链，只选择下一步动作。"
                "只允许 search_knowledge 和 finish。search_knowledge 只能检索当前领域的只读知识。"
                "如果证据能直接支持回答则 finish；证据缺少关键条件时生成更具体且不改变产品、金额、日期、否定条件的检索词。"
                "只输出 JSON：{\"action\":\"search_knowledge|finish\","
                "\"query\":\"检索词或 null\",\"reason_code\":\"小写英文下划线代码\"}。"
            )),
            ModelMessage(role="user", content=(
                f"问题：{question}\n最近对话：\n{recent}\n已有证据：\n{observations}\n"
                f"已执行动作：\n{previous}"
            )),
        ], max_tokens=180))
        action = ReActAction.model_validate(_json_object(output.text))
        usage = {
            "operation": f"react_controller_{self.domain}", "model": output.model,
            "input_tokens": output.input_tokens, "output_tokens": output.output_tokens,
        }
        return action, usage

    def _extractive_draft(self, question: str, evidence: list[Evidence], reason: str) -> AgentDraft:
        skill = self.skill_manager.prompt_for(question, self.domain) if self.skill_manager else None
        return AgentDraft(
            domain=self.domain, answer=extractive_answer(self.domain, evidence),
            evidence_ids=[item.id for item in evidence],
            generation_mode="evidence_extract", degraded=True,
            degradation_reason=reason, skills_used=skill.names if skill else [],
        )
