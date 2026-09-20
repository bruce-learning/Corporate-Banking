from __future__ import annotations

import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.core.agents.bank import AgentDraft, BankDomainAgent, DOMAIN_NAMES
from app.core.agents.react import ReActDomainAgent
from app.core.intent.recognizer import IntentDecision
from app.core.orchestration.bank import (
    BankOrchestrator,
    ChatResult,
    _citation_quality,
    _conflicts,
)
from app.core.orchestration.langgraph.domain import build_domain_subgraph
from app.core.orchestration.langgraph.state import BankGraphState
from app.core.planning.query import rewrite_query
from app.core.planning.task import TaskPlan, build_plan
from app.core.rag.hybrid import Evidence
from app.core.safety.risk import RiskDecision, inspect_risk
from app.infrastructure.mcp.gateway import ToolResult


class LangGraphBankOrchestrator(BankOrchestrator):
    """StateGraph orchestration with parallel domain fan-out and explicit joins."""

    backend_name = "langgraph"

    def __init__(self, *args, recursion_limit: int = 30, **kwargs):
        super().__init__(*args, **kwargs)
        self.recursion_limit = max(10, min(int(recursion_limit), 100))
        self.domain_graph = build_domain_subgraph(self._run_domain)
        self.graph = self._build_graph()

    async def run(self, message: str, context: list[dict], long_term_context: str = "",
                  memory_context: dict | None = None) -> ChatResult:
        started = time.monotonic()
        try:
            state = await self.graph.ainvoke({
                "message": message,
                "context": list(context),
                "long_term_context": long_term_context,
                "memory_context": memory_context or {},
                "started": started,
                "branch_results": [],
                "graph_trace": [],
            }, config={"recursion_limit": self.recursion_limit})
            result: ChatResult = state["result"]
            result.orchestration_backend = self.backend_name
            result.graph_trace = self._ordered_trace(state.get("graph_trace", []))
            result.latency_ms = int((time.monotonic() - started) * 1000)
            self._chat_metric(result)
            if self.metrics:
                self.metrics.increment(
                    "bank_langgraph_runs_total",
                    outcome="degraded" if result.degraded else "success",
                    execution_mode=result.execution_mode,
                )
            return result
        except Exception:
            # All currently authorised tools are read-only, so falling back to
            # the legacy orchestrator cannot duplicate a banking transaction.
            result = await super().run(message, context, long_term_context, memory_context)
            result.orchestration_backend = "legacy_fallback"
            result.degraded = True
            result.degradation_reasons = list(dict.fromkeys(
                result.degradation_reasons + ["langgraph_runtime_fallback"]
            ))
            result.graph_trace = [{
                "index": 1, "node": "langgraph_runtime", "status": "degraded",
                "reason": "langgraph_runtime_fallback",
            }]
            return result

    def _build_graph(self):
        graph = StateGraph(BankGraphState)
        graph.add_node("risk_guard", self._risk_guard_node)
        graph.add_node("blocked_response", self._blocked_response_node)
        graph.add_node("rewrite_query", self._rewrite_node)
        graph.add_node("recognize_intent", self._intent_node)
        graph.add_node("fixed_response", self._fixed_response_node)
        graph.add_node("clarify_response", self._clarify_response_node)
        graph.add_node("build_plan", self._plan_node)
        graph.add_node("domain_agent", self._domain_agent_node)
        graph.add_node("sequential_agent", self._sequential_agent_node)
        graph.add_node("aggregate", self._aggregate_node)

        graph.add_edge(START, "risk_guard")
        graph.add_conditional_edges(
            "risk_guard", self._after_risk,
            {"blocked": "blocked_response", "continue": "rewrite_query"},
        )
        graph.add_edge("blocked_response", END)
        graph.add_edge("rewrite_query", "recognize_intent")
        graph.add_conditional_edges(
            "recognize_intent", self._after_intent,
            {"fixed": "fixed_response", "clarify": "clarify_response", "plan": "build_plan"},
        )
        graph.add_edge("fixed_response", END)
        graph.add_edge("clarify_response", END)
        graph.add_conditional_edges(
            "build_plan", self._dispatch_plan,
            ["domain_agent", "sequential_agent"],
        )
        graph.add_edge("domain_agent", "aggregate")
        graph.add_conditional_edges(
            "sequential_agent", self._after_sequential,
            {"continue": "sequential_agent", "aggregate": "aggregate"},
        )
        graph.add_edge("aggregate", END)
        return graph.compile()

    async def _risk_guard_node(self, state: BankGraphState) -> dict:
        risk = inspect_risk(state["message"])
        if risk.blocked and self.metrics:
            for flag in risk.flags:
                self.metrics.increment("bank_safety_blocks_total", flag=flag)
        return {
            "risk": risk.model_dump(),
            "graph_trace": [self._event("risk_guard", "blocked" if risk.blocked else "success")],
        }

    @staticmethod
    def _after_risk(state: BankGraphState) -> str:
        return "blocked" if state["risk"]["blocked"] else "continue"

    async def _blocked_response_node(self, state: BankGraphState) -> dict:
        risk = RiskDecision.model_validate(state["risk"])
        result = ChatResult(
            answer=risk.response or "该请求已被安全规则拦截。",
            stored_user_message=risk.safe_message,
            original_query=risk.safe_message, rewritten_query=risk.safe_message,
            intent="policy", confidence=1.0, risk_flags=risk.flags,
            generation_mode="hard_rule", execution_mode="fixed",
            orchestration_backend=self.backend_name,
        )
        return {"result": result, "graph_trace": [self._event("blocked_response")]}

    async def _rewrite_node(self, state: BankGraphState) -> dict:
        rewrite = rewrite_query(
            state["risk"]["safe_message"], state.get("context", []),
            self.context_rewrite_max_messages,
        )
        return {"rewrite": rewrite.model_dump(), "graph_trace": [self._event("rewrite_query")]}

    async def _intent_node(self, state: BankGraphState) -> dict:
        intent = await self.recognizer.recognize(state["rewrite"]["rewritten"])
        risk = RiskDecision.model_validate(state["risk"])
        domains = list(dict.fromkeys(intent.domains + risk.forced_domains))
        return {
            "intent": intent.model_dump(), "domains": domains,
            "graph_trace": [self._event("recognize_intent", reason=intent.label)],
        }

    @staticmethod
    def _after_intent(state: BankGraphState) -> str:
        label = state["intent"]["label"]
        if label in {"greeting", "escalation"}:
            return "fixed"
        return "plan" if state.get("domains") else "clarify"

    async def _fixed_response_node(self, state: BankGraphState) -> dict:
        intent = IntentDecision.model_validate(state["intent"])
        risk = RiskDecision.model_validate(state["risk"])
        result = self._fixed_response(
            intent, risk, state["rewrite"]["original"], state["rewrite"]["rewritten"]
        )
        if result is None:
            raise RuntimeError("固定回答路由没有生成结果")
        result.orchestration_backend = self.backend_name
        return {"result": result, "graph_trace": [self._event("fixed_response", reason=intent.label)]}

    async def _clarify_response_node(self, state: BankGraphState) -> dict:
        intent = IntentDecision.model_validate(state["intent"])
        risk = RiskDecision.model_validate(state["risk"])
        result = ChatResult(
            answer="我目前专注于银行对公产品、办理流程和政策收费咨询。请补充具体产品或业务场景。",
            stored_user_message=risk.safe_message,
            original_query=state["rewrite"]["original"],
            rewritten_query=state["rewrite"]["rewritten"],
            intent="other", confidence=intent.confidence, risk_flags=risk.flags,
            strategy_scores=intent.strategy_scores, strategy_weights=intent.strategy_weights,
            degraded=intent.degraded, degradation_reasons=intent.degradation_reasons,
            generation_mode="fixed", execution_mode="fixed", model_usage=intent.model_usage,
            orchestration_backend=self.backend_name,
        )
        return {"result": result, "graph_trace": [self._event("clarify_response")]}

    async def _plan_node(self, state: BankGraphState) -> dict:
        plan = build_plan(state["rewrite"]["rewritten"], state["domains"])
        agent_context = list(state.get("context", []))
        if state.get("long_term_context"):
            agent_context.append({"role": "memory", "content": state["long_term_context"]})
        return {
            "plan": plan.model_dump(), "agent_context": agent_context, "sequential_index": 0,
            "graph_trace": [self._event("build_plan", reason=plan.execution_mode)],
        }

    def _dispatch_plan(self, state: BankGraphState):
        plan = TaskPlan.model_validate(state["plan"])
        if plan.execution_mode == "plan_execute":
            return "sequential_agent"
        return [Send("domain_agent", {
            "step": step.model_dump(), "agent_context": state.get("agent_context", []),
        }) for step in plan.steps]

    async def _domain_agent_node(self, state: BankGraphState) -> dict:
        step = state["step"]
        output = await self.domain_graph.ainvoke({
            "domain": step["domain"], "query": step["query"],
            "context": state.get("agent_context", []), "step_index": step["index"],
            "trace": [],
        })
        return {
            "branch_results": [output["result"]],
            "graph_trace": output.get("trace", []),
        }

    async def _sequential_agent_node(self, state: BankGraphState) -> dict:
        plan = TaskPlan.model_validate(state["plan"])
        index = int(state.get("sequential_index", 0))
        step = plan.steps[index]
        context = list(state.get("agent_context", []))
        for branch in sorted(state.get("branch_results", []), key=lambda item: item["step_index"]):
            draft = AgentDraft.model_validate(branch["draft"])
            context.append({
                "role": "assistant",
                "content": f"{DOMAIN_NAMES[draft.domain]}步骤结果：{draft.answer}",
            })
        output = await self.domain_graph.ainvoke({
            "domain": step.domain, "query": step.query, "context": context,
            "step_index": step.index, "trace": [],
        })
        return {
            "branch_results": [output["result"]], "sequential_index": index + 1,
            "graph_trace": output.get("trace", []),
        }

    @staticmethod
    def _after_sequential(state: BankGraphState) -> str:
        return "continue" if int(state["sequential_index"]) < len(state["plan"]["steps"]) else "aggregate"

    async def _aggregate_node(self, state: BankGraphState) -> dict:
        intent = IntentDecision.model_validate(state["intent"])
        risk = RiskDecision.model_validate(state["risk"])
        plan = TaskPlan.model_validate(state["plan"])
        branches = sorted(state.get("branch_results", []), key=lambda item: item["step_index"])
        by_step = {item["step_index"]: item for item in branches}
        for step in plan.steps:
            if step.index in by_step:
                step.status = by_step[step.index]["status"]

        tool_results = [
            ToolResult.model_validate(tool)
            for branch in branches for tool in branch.get("tools", [])
        ]
        drafts = [AgentDraft.model_validate(branch["draft"]) for branch in branches]
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
        react_steps = {
            branch["domain"]: branch["react_steps"]
            for branch in branches if branch.get("react_steps")
        }
        if self.metrics:
            for agent, names in skills_used.items():
                for name in names:
                    self.metrics.increment("bank_skill_matches_total", agent=agent, skill=name)

        result = ChatResult(
            answer=answer, stored_user_message=risk.safe_message,
            original_query=state["rewrite"]["original"],
            rewritten_query=state["rewrite"]["rewritten"],
            intent=intent.label, confidence=intent.confidence,
            domains=state["domains"], agents=state["domains"], risk_flags=risk.flags,
            strategy_scores=intent.strategy_scores, strategy_weights=intent.strategy_weights,
            tool_calls=[item.model_dump(exclude={"items"}, mode="json") for item in tool_results],
            evidence=evidence, conflicts=conflicts, degraded=bool(reasons),
            degradation_reasons=reasons,
            generation_mode=modes.pop() if len(modes) == 1 else "multi_mode",
            model=models[0] if models and len(set(models)) == 1 else None,
            execution_mode=plan.execution_mode,
            plan_steps=[step.model_dump() for step in plan.steps],
            citation_valid=citation_valid, citation_coverage=citation_coverage,
            model_usage=usage, skills_used=skills_used, react_steps=react_steps,
            memory_context=state.get("memory_context", {}),
            orchestration_backend=self.backend_name,
        )
        return {"result": result, "graph_trace": [self._event("aggregate", reason=plan.execution_mode)]}

    async def _run_domain(self, domain: str, query: str, context: list[dict], step_index: int) -> dict:
        try:
            if self.react_enabled:
                outcome = await ReActDomainAgent(
                    domain, self.gateway, self.model_client, self.skill_manager,
                    top_k=self.top_k, max_steps=self.react_max_steps,
                    max_tool_calls=self.react_max_tool_calls,
                    timeout_seconds=self.react_timeout_seconds,
                    max_evidence=self.react_max_evidence, metrics=self.metrics,
                ).run(query, context)
                tools = outcome.tool_results
                draft = outcome.draft
                react_steps = [item.model_dump() for item in outcome.steps]
            else:
                tool = await self.gateway.search(domain, query, self.top_k)
                tools = [tool]
                draft = await BankDomainAgent(domain, self.model_client, self.skill_manager).answer(
                    query, context, tool.items if tool.success else []
                )
                react_steps = []
            degraded = draft.degraded or any(item.degraded or not item.success for item in tools)
            return {
                "step_index": step_index, "domain": domain,
                "status": "degraded" if degraded else "success",
                "degradation_reason": draft.degradation_reason if degraded else None,
                "tools": [item.model_dump(mode="json") for item in tools],
                "draft": draft.model_dump(mode="json"), "react_steps": react_steps,
            }
        except Exception:
            draft = AgentDraft(
                domain=domain,
                answer=f"{DOMAIN_NAMES[domain]}服务暂不可用，请稍后重试或联系人工渠道。",
                generation_mode="graph_fallback", degraded=True,
                degradation_reason="langgraph_domain_unavailable",
            )
            return {
                "step_index": step_index, "domain": domain, "status": "degraded",
                "degradation_reason": "langgraph_domain_unavailable", "tools": [],
                "draft": draft.model_dump(mode="json"), "react_steps": [],
            }

    @staticmethod
    def _event(node: str, status: str = "success", *, domain: str | None = None,
               step: int | None = None, reason: str | None = None) -> dict:
        return {
            "node": node, "status": status, "domain": domain,
            "step": step, "reason": reason,
        }

    @staticmethod
    def _ordered_trace(events: list[dict]) -> list[dict]:
        phase = {
            "risk_guard": 10, "blocked_response": 20, "rewrite_query": 20,
            "recognize_intent": 30, "fixed_response": 40, "clarify_response": 40,
            "build_plan": 40, "prepare_domain": 50, "execute_domain": 60,
            "aggregate": 70,
        }
        ordered = sorted(events, key=lambda item: (
            phase.get(item.get("node"), 999), int(item.get("step") or 0),
            str(item.get("domain") or ""),
        ))
        return [{"index": index, **item} for index, item in enumerate(ordered, 1)]
