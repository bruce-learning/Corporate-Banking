from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypedDict


class BankGraphState(TypedDict, total=False):
    """Serializable shared state exchanged by the bank orchestration graph."""

    message: str
    context: list[dict]
    long_term_context: str
    memory_context: dict
    started: float
    risk: dict
    rewrite: dict
    intent: dict
    domains: list[str]
    plan: dict
    step: dict
    agent_context: list[dict]
    sequential_index: int
    branch_results: Annotated[list[dict], add]
    graph_trace: Annotated[list[dict], add]
    result: Any


class DomainGraphState(TypedDict, total=False):
    """Input/output state for one Product, Process or Policy subgraph."""

    domain: str
    query: str
    context: list[dict]
    step_index: int
    result: dict
    trace: Annotated[list[dict], add]
