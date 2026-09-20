from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


ORDER = ("product", "policy", "process")
COMPLEX_MARKERS = ("先", "再", "然后", "之后", "根据", "分别", "综合", "完整方案", "前提")


class PlanStep(BaseModel):
    index: int
    domain: Literal["product", "process", "policy"]
    query: str
    depends_on: list[int] = Field(default_factory=list)
    status: Literal["pending", "success", "degraded", "skipped"] = "pending"


class TaskPlan(BaseModel):
    execution_mode: Literal["fixed", "parallel", "plan_execute"]
    steps: list[PlanStep] = Field(default_factory=list)


def build_plan(query: str, domains: list[str]) -> TaskPlan:
    selected = [domain for domain in ORDER if domain in domains]
    if len(selected) <= 1:
        return TaskPlan(
            execution_mode="fixed",
            steps=[PlanStep(index=1, domain=domain, query=query) for domain in selected],
        )
    complex_request = any(marker in query for marker in COMPLEX_MARKERS)
    if not complex_request:
        return TaskPlan(
            execution_mode="parallel",
            steps=[PlanStep(index=index, domain=domain, query=query)
                   for index, domain in enumerate(selected, 1)],
        )
    steps = []
    for index, domain in enumerate(selected, 1):
        steps.append(PlanStep(
            index=index, domain=domain, query=query,
            depends_on=[index - 1] if index > 1 else [],
        ))
    return TaskPlan(execution_mode="plan_execute", steps=steps)
