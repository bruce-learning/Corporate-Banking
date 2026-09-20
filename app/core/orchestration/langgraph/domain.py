from __future__ import annotations

from collections.abc import Awaitable, Callable

from langgraph.graph import END, START, StateGraph

from app.core.agents.bank import DOMAIN_NAMES
from app.core.orchestration.langgraph.state import DomainGraphState


DomainRunner = Callable[[str, str, list[dict], int], Awaitable[dict]]


def build_domain_subgraph(runner: DomainRunner):
    """Build a reusable bounded domain-agent subgraph.

    The subgraph deliberately keeps business logic in the existing domain and
    ReAct agents. LangGraph owns state hand-off and execution boundaries.
    """

    async def prepare(state: DomainGraphState) -> dict:
        domain = state.get("domain", "")
        if domain not in DOMAIN_NAMES:
            raise ValueError("未知银行领域")
        return {"trace": [{
            "node": "prepare_domain", "domain": domain,
            "step": int(state.get("step_index", 0)), "status": "success",
        }]}

    async def execute(state: DomainGraphState) -> dict:
        domain = state["domain"]
        step_index = int(state.get("step_index", 0))
        result = await runner(domain, state["query"], state.get("context", []), step_index)
        return {
            "result": result,
            "trace": [{
                "node": "execute_domain", "domain": domain, "step": step_index,
                "status": result.get("status", "success"),
                "reason": result.get("degradation_reason"),
            }],
        }

    graph = StateGraph(DomainGraphState)
    graph.add_node("prepare_domain", prepare)
    graph.add_node("execute_domain", execute)
    graph.add_edge(START, "prepare_domain")
    graph.add_edge("prepare_domain", "execute_domain")
    graph.add_edge("execute_domain", END)
    return graph.compile()
