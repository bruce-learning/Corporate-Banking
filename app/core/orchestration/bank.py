from __future__ import annotations

import asyncio
import re
import time
from pydantic import BaseModel, Field

from app.core.agents.bank import AgentDraft, BankDomainAgent, DOMAIN_NAMES
from app.core.agents.react import ReActDomainAgent
from app.core.intent.recognizer import IntentDecision, IntentRecognizer
from app.core.planning.query import rewrite_query
from app.core.planning.task import PlanStep, TaskPlan, build_plan
from app.core.rag.hybrid import Evidence
from app.core.safety.risk import RiskDecision, inspect_risk


class ChatResult(BaseModel):
    answer: str
    stored_user_message: str
    original_query: str
    rewritten_query: str
    intent: str
    confidence: float
    domains: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    strategy_scores: dict = Field(default_factory=dict)
    strategy_weights: dict = Field(default_factory=dict)
    tool_calls: list[dict] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    generation_mode: str
    model: str | None = None
    execution_mode: str = "fixed"
    plan_steps: list[dict] = Field(default_factory=list)
    citation_valid: bool = True
    citation_coverage: float = 0.0
    model_usage: list[dict] = Field(default_factory=list)
    skills_used: dict[str, list[str]] = Field(default_factory=dict)
    react_steps: dict[str, list[dict]] = Field(default_factory=dict)
    memory_context: dict = Field(default_factory=dict)
    latency_ms: int = 0


def _conflicts(items: list[Evidence]) -> list[str]:
    seen: dict[tuple[str, str], Evidence] = {}
    conflicts: list[str] = []
    for item in sorted(items, key=lambda row: (row.effective_at, row.id)):
        topic = str(item.metadata.get("topic") or item.title)
        key = (item.product or "", topic)
        previous = seen.get(key)
        if previous and previous.content != item.content:
            conflicts.append(f"{previous.id}:{item.id}")
        seen[key] = item
    return sorted(set(conflicts))


def _citation_quality(answer: str, evidence: list[Evidence], drafts: list[AgentDraft]) -> tuple[bool, float]:
    valid_ids = {item.id for item in evidence}
    references = re.findall(r"\[资料:([^\]]+)\]", answer)
    valid = all(reference in valid_ids for reference in references)
    evidence_drafts = [draft for draft in drafts if draft.evidence_ids]
    covered = sum(any(f"[资料:{item}]" in answer for item in draft.evidence_ids) for draft in evidence_drafts)
    coverage = covered / len(evidence_drafts) if evidence_drafts else 0.0
    return valid, round(coverage, 6)


class BankOrchestrator:
    def __init__(self, recognizer: IntentRecognizer, gateway, model_client=None, top_k: int = 4,
                 context_rewrite_max_messages: int = 6, metrics=None, skill_manager=None, *,
                 react_enabled: bool = False, react_max_steps: int = 3,
                 react_max_tool_calls: int = 3, react_timeout_seconds: float = 15.0,
                 react_max_evidence: int = 8):
        self.recognizer = recognizer
        self.gateway = gateway
        self.model_client = model_client
        self.top_k = top_k
        self.context_rewrite_max_messages = context_rewrite_max_messages
        self.metrics = metrics
        self.skill_manager = skill_manager
        self.react_enabled = react_enabled
        self.react_max_steps = react_max_steps
        self.react_max_tool_calls = react_max_tool_calls
        self.react_timeout_seconds = react_timeout_seconds
        self.react_max_evidence = react_max_evidence

    async def run(self, message: str, context: list[dict], long_term_context: str = "",
                  memory_context: dict | None = None) -> ChatResult:
        started = time.monotonic()
        risk = inspect_risk(message)
        if risk.blocked:
            for flag in risk.flags:
                if self.metrics:
                    self.metrics.increment("bank_safety_blocks_total", flag=flag)
            return ChatResult(
                answer=risk.response or "该请求已被安全规则拦截。", stored_user_message=risk.safe_message,
                original_query=risk.safe_message, rewritten_query=risk.safe_message,
                intent="policy", confidence=1.0, domains=[], agents=[], risk_flags=risk.flags,
                generation_mode="hard_rule", execution_mode="fixed",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        rewrite = rewrite_query(risk.safe_message, context, self.context_rewrite_max_messages)
        intent = await self.recognizer.recognize(rewrite.rewritten)
        fixed = self._fixed_response(intent, risk, rewrite.original, rewrite.rewritten)
        if fixed:
            fixed.latency_ms = int((time.monotonic() - started) * 1000)
            self._chat_metric(fixed)
            return fixed
        domains = list(dict.fromkeys(intent.domains + risk.forced_domains))
        if not domains:
            result = ChatResult(
                answer="我目前专注于银行对公产品、办理流程和政策收费咨询。请补充具体产品或业务场景。",
                stored_user_message=risk.safe_message, original_query=rewrite.original,
                rewritten_query=rewrite.rewritten, intent="other", confidence=intent.confidence,
                risk_flags=risk.flags, strategy_scores=intent.strategy_scores,
                strategy_weights=intent.strategy_weights, degraded=intent.degraded,
                degradation_reasons=intent.degradation_reasons, generation_mode="fixed",
                execution_mode="fixed", model_usage=intent.model_usage,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            self._chat_metric(result)
            return result
        plan = build_plan(rewrite.rewritten, domains)
        agent_context = list(context)
        if long_term_context:
            agent_context.append({"role": "memory", "content": long_term_context})
        tool_results, drafts, react_steps = await self._execute(plan, agent_context)
        evidence_by_id: dict[str, Evidence] = {}
        for result in tool_results:
            for item in result.items:
                evidence_by_id.setdefault(item.id, item)
        evidence = list(evidence_by_id.values())
        conflicts = _conflicts(evidence)
        answer = self._merge(drafts, bool(risk.forced_domains), conflicts)
        citation_valid, citation_coverage = _citation_quality(answer, evidence, drafts)
        reasons = list(intent.degradation_reasons)
        for result in tool_results:
            if result.degraded or not result.success:
                reasons.append(result.degradation_reason or "mcp_degraded")
        for draft in drafts:
            if draft.degraded:
                reasons.append(draft.degradation_reason or "agent_degraded")
        if not citation_valid:
            reasons.append("citation_invalid")
        reasons = list(dict.fromkeys(reasons))
        models = [draft.model for draft in drafts if draft.model]
        modes = {draft.generation_mode for draft in drafts}
        usage = list(intent.model_usage)
        for draft in drafts:
            usage.extend(draft.model_usage)
        skills_used = {draft.domain: draft.skills_used for draft in drafts if draft.skills_used}
        if self.metrics:
            for agent, names in skills_used.items():
                for name in names:
                    self.metrics.increment("bank_skill_matches_total", agent=agent, skill=name)
        result = ChatResult(
            answer=answer, stored_user_message=risk.safe_message,
            original_query=rewrite.original, rewritten_query=rewrite.rewritten,
            intent=intent.label, confidence=intent.confidence, domains=domains, agents=domains,
            risk_flags=risk.flags, strategy_scores=intent.strategy_scores,
            strategy_weights=intent.strategy_weights,
            tool_calls=[item.model_dump(exclude={"items"}, mode="json") for item in tool_results],
            evidence=evidence, conflicts=conflicts, degraded=bool(reasons),
            degradation_reasons=reasons,
            generation_mode=modes.pop() if len(modes) == 1 else "multi_mode",
            model=models[0] if models and len(set(models)) == 1 else None,
            execution_mode=plan.execution_mode,
            plan_steps=[step.model_dump() for step in plan.steps],
            citation_valid=citation_valid, citation_coverage=citation_coverage,
            model_usage=usage,
            skills_used=skills_used, react_steps=react_steps,
            memory_context=memory_context or {},
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        self._chat_metric(result)
        return result

    async def _execute(self, plan: TaskPlan, context: list[dict]):
        async def step_call(step: PlanStep, augmented_context: list[dict]):
            if self.react_enabled:
                outcome = await ReActDomainAgent(
                    step.domain, self.gateway, self.model_client, self.skill_manager,
                    top_k=self.top_k, max_steps=self.react_max_steps,
                    max_tool_calls=self.react_max_tool_calls,
                    timeout_seconds=self.react_timeout_seconds,
                    max_evidence=self.react_max_evidence, metrics=self.metrics,
                ).run(step.query, augmented_context)
                draft = outcome.draft
                step.status = "degraded" if draft.degraded or any(
                    result.degraded or not result.success for result in outcome.tool_results
                ) else "success"
                return outcome.tool_results, draft, [item.model_dump() for item in outcome.steps]
            tool = await self.gateway.search(step.domain, step.query, self.top_k)
            draft = await BankDomainAgent(step.domain, self.model_client, self.skill_manager).answer(
                step.query, augmented_context, tool.items if tool.success else []
            )
            step.status = "degraded" if tool.degraded or draft.degraded else "success"
            return [tool], draft, []

        if plan.execution_mode != "plan_execute":
            outcomes = await asyncio.gather(*[step_call(step, context) for step in plan.steps])
            tools = [tool for outcome in outcomes for tool in outcome[0]]
            drafts = [outcome[1] for outcome in outcomes]
            traces = {
                step.domain: outcome[2] for step, outcome in zip(plan.steps, outcomes) if outcome[2]
            }
            return tools, drafts, traces
        tools, drafts, traces = [], [], {}
        augmented = list(context)
        for step in plan.steps:
            step_tools, draft, step_trace = await step_call(step, augmented)
            tools.extend(step_tools)
            drafts.append(draft)
            if step_trace:
                traces[step.domain] = step_trace
            augmented.append({"role": "assistant", "content": f"{DOMAIN_NAMES[step.domain]}步骤结果：{draft.answer}"})
        return tools, drafts, traces

    def _fixed_response(self, intent: IntentDecision, risk: RiskDecision,
                        original: str, rewritten: str) -> ChatResult | None:
        response = None
        if intent.label == "greeting":
            response = "你好，我可以协助查询银行对公产品功能、办理流程、收费政策与安全边界。"
        elif intent.label == "escalation":
            response = "我已识别到人工服务诉求。本演示系统不能真实转接，请通过银行官方客服或联系客户经理。"
        if response is None:
            return None
        return ChatResult(
            answer=response, stored_user_message=risk.safe_message,
            original_query=original, rewritten_query=rewritten, intent=intent.label,
            confidence=intent.confidence, risk_flags=risk.flags,
            strategy_scores=intent.strategy_scores, strategy_weights=intent.strategy_weights,
            degraded=intent.degraded, degradation_reasons=intent.degradation_reasons,
            generation_mode="fixed", execution_mode="fixed", model_usage=intent.model_usage,
        )

    def _chat_metric(self, result: ChatResult):
        if self.metrics:
            self.metrics.increment(
                "bank_chat_requests_total", execution_mode=result.execution_mode,
                degraded="true" if result.degraded else "false",
            )

    @staticmethod
    def _merge(drafts: list[AgentDraft], forced_policy: bool, conflicts: list[str]) -> str:
        sections = []
        if forced_policy:
            sections.append("说明：系统不能保证审批结果、固定费率或放款结果，以下仅依据当前有效演示资料。")
        for draft in drafts:
            if len(drafts) > 1:
                sections.append(f"### {DOMAIN_NAMES[draft.domain]}\n{draft.answer}")
            else:
                sections.append(draft.answer)
        if conflicts:
            sections.append("检测到资料间可能存在差异，已保留引用，请以最新有效资料或人工复核为准。")
        return "\n\n".join(sections)
