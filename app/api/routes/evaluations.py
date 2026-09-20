from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import Identity, administrator, require_store
from app.models.entities.storage import BadCase, EvaluationRun, utcnow


router = APIRouter(prefix="/api/v1/admin/evaluations", tags=["evaluations"])


class BadCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=40)
    expected: dict = Field(default_factory=dict)
    actual: dict = Field(default_factory=dict)


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=80)
    metrics: dict
    limitations: list[str] = Field(default_factory=list, max_length=20)
    bad_cases: list[BadCaseInput] = Field(default_factory=list, max_length=1000)


def run_payload(item: EvaluationRun) -> dict:
    return {
        "id": item.id, "name": item.name, "dataset_version": item.dataset_version,
        "status": item.status, "metrics": item.metrics_json,
        "limitations": item.limitations_json, "created_at": item.created_at,
        "completed_at": item.completed_at,
    }


@router.post("", status_code=201)
async def create_run(body: EvaluationInput, request: Request,
                     identity: Identity = Depends(administrator)):
    async with require_store(request).sessions() as session:
        item = EvaluationRun(
            name=body.name, dataset_version=body.dataset_version, status="completed",
            metrics_json=body.metrics, limitations_json=body.limitations,
            completed_at=utcnow(),
        )
        session.add(item)
        await session.flush()
        for case in body.bad_cases:
            session.add(BadCase(
                evaluation_run_id=item.id, case_id=case.case_id, category=case.category,
                expected_json=case.expected, actual_json=case.actual,
            ))
        await session.commit()
        return run_payload(item)


@router.get("")
async def list_runs(request: Request, limit: int = Query(default=20, ge=1, le=100),
                    identity: Identity = Depends(administrator)):
    async with require_store(request).sessions() as session:
        items = list((await session.scalars(select(EvaluationRun).order_by(
            EvaluationRun.created_at.desc()).limit(limit))).all())
    return {"items": [run_payload(item) for item in items]}


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request, identity: Identity = Depends(administrator)):
    async with require_store(request).sessions() as session:
        item = await session.get(EvaluationRun, run_id)
        if not item:
            raise HTTPException(404, "评测记录不存在")
        cases = list((await session.scalars(select(BadCase).where(
            BadCase.evaluation_run_id == run_id
        ).order_by(BadCase.created_at))).all())
    value = run_payload(item)
    value["bad_cases"] = [{
        "id": case.id, "case_id": case.case_id, "category": case.category,
        "expected": case.expected_json, "actual": case.actual_json,
        "status": case.status,
    } for case in cases]
    return value


@router.patch("/bad-cases/{case_id}")
async def update_bad_case(case_id: str, status: str, request: Request,
                          identity: Identity = Depends(administrator)):
    if status not in {"open", "fixed", "wont_fix"}:
        raise HTTPException(422, "Bad Case 状态不合法")
    async with require_store(request).sessions() as session:
        item = await session.get(BadCase, case_id)
        if not item:
            raise HTTPException(404, "Bad Case 不存在")
        item.status = status
        await session.commit()
        return {"id": item.id, "status": item.status}
