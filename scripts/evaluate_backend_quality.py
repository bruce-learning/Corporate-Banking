"""0.7 后端确定性质量门禁；默认零付费，可用 --persist 写入评测与 Bad Case 表。"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
from pathlib import Path
import sys
from sqlalchemy import select, update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core.intent.recognizer import LABELS, IntentRecognizer
from app.core.planning.query import rewrite_query
from app.core.planning.task import build_plan
from app.core.safety.risk import inspect_risk
from app.infrastructure.database.store import Store
from app.models.entities.storage import BadCase, EvaluationRun, utcnow

DATA = ROOT / "datasets/generated/backend_quality_v1"


def load(name):
    return [json.loads(line) for line in (DATA / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def f1(label, expected, predicted):
    tp = sum(a == label and b == label for a, b in zip(expected, predicted))
    fp = sum(a != label and b == label for a, b in zip(expected, predicted))
    fn = sum(a == label and b != label for a, b in zip(expected, predicted))
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0


async def evaluate():
    recognizer = IntentRecognizer(None)
    expected, predicted, bad_cases = [], [], []
    for row in load("intent.jsonl"):
        actual = (await recognizer.recognize(row["text"])).label
        expected.append(row["label"]); predicted.append(actual)
        if actual != row["label"]:
            bad_cases.append({"case_id": row["id"], "category": "intent",
                              "expected": {"label": row["label"]}, "actual": {"label": actual}})
    per_label = {label: f1(label, expected, predicted) for label in LABELS}

    safety_rows = load("safety.jsonl")
    safety_ok = 0
    for row in safety_rows:
        actual = inspect_risk(row["text"])
        okay = actual.blocked == row["blocked"] and row["expected_flag"] in actual.flags
        safety_ok += okay
        if not okay:
            bad_cases.append({"case_id": row["id"], "category": "safety",
                              "expected": {"blocked": row["blocked"], "flag": row["expected_flag"]},
                              "actual": {"blocked": actual.blocked, "flags": actual.flags}})

    multi_rows = load("multi_turn.jsonl")
    multi_ok = 0
    for row in multi_rows:
        rewrite = rewrite_query(row["message"], row["context"])
        decision = await recognizer.recognize(rewrite.rewritten)
        okay = row["expected_contains"] in rewrite.rewritten and decision.domains == [row["expected_domain"]]
        multi_ok += okay
        if not okay:
            bad_cases.append({"case_id": row["id"], "category": "multi_turn",
                              "expected": {"contains": row["expected_contains"], "domain": row["expected_domain"]},
                              "actual": {"rewritten": rewrite.rewritten, "domains": decision.domains}})

    complex_rows = load("complex.jsonl")
    complex_ok = 0
    for row in complex_rows:
        decision = await recognizer.recognize(row["text"])
        plan = build_plan(row["text"], decision.domains)
        actual_order = [step.domain for step in plan.steps]
        okay = plan.execution_mode == row["expected_mode"] and actual_order == row["expected_order"]
        complex_ok += okay
        if not okay:
            bad_cases.append({"case_id": row["id"], "category": "planning",
                              "expected": {"mode": row["expected_mode"], "order": row["expected_order"]},
                              "actual": {"mode": plan.execution_mode, "order": actual_order}})

    injections = load("prompt_injection.jsonl")
    injection_ok = sum(inspect_risk(row["text"]).blocked for row in injections)
    metrics = {
        "paid_model_calls": 0, "intent_cases": len(expected),
        "intent_accuracy": sum(a == b for a, b in zip(expected, predicted)) / len(expected),
        "intent_macro_f1": sum(per_label.values()) / len(per_label),
        "intent_f1_by_label": per_label, "prediction_counts": dict(Counter(predicted)),
        "safety_cases": len(safety_rows), "safety_accuracy": safety_ok / len(safety_rows),
        "multi_turn_cases": len(multi_rows), "multi_turn_accuracy": multi_ok / len(multi_rows),
        "complex_cases": len(complex_rows), "planning_accuracy": complex_ok / len(complex_rows),
        "prompt_injection_cases": len(injections), "prompt_injection_block_rate": injection_ok / len(injections),
        "degradation_contract_cases": len(load("degradation.jsonl")),
        "bad_case_count": len(bad_cases),
    }
    return metrics, bad_cases


async def persist(metrics, bad_cases):
    settings = Settings()
    if not settings.database_url.get_secret_value():
        raise RuntimeError("未配置数据库")
    store = Store(settings.database_url.get_secret_value())
    try:
        async with store.sessions() as session:
            run = EvaluationRun(
                name="后端质量门禁离线评测", dataset_version="backend-quality-v1",
                status="completed", metrics_json=metrics,
                limitations_json=["组合式合成集，不代表真实客户分布", "本次离线评测零付费"],
                completed_at=utcnow(),
            )
            session.add(run); await session.flush()
            for case in bad_cases:
                session.add(BadCase(
                    evaluation_run_id=run.id, case_id=case["case_id"], category=case["category"],
                    expected_json=case["expected"], actual_json=case["actual"],
                ))
            fixed_prior = 0
            if not bad_cases:
                prior_ids = list((await session.scalars(select(EvaluationRun.id).where(
                    EvaluationRun.dataset_version == "backend-quality-v1",
                    EvaluationRun.id != run.id,
                ))).all())
                if prior_ids:
                    result = await session.execute(update(BadCase).where(
                        BadCase.evaluation_run_id.in_(prior_ids), BadCase.status == "open"
                    ).values(status="fixed"))
                    fixed_prior = int(result.rowcount or 0)
            await session.commit()
            return run.id, fixed_prior
    finally:
        await store.close()


async def main():
    metrics, bad_cases = await evaluate()
    report = {
        "evaluation": "backend-quality-v1-offline", "synthetic": True,
        "metrics": metrics, "bad_cases": bad_cases,
        "limitations": ["组合式合成数据只用于回归门禁。", "真实模型和端到端链路需单独在线验收。"],
    }
    if "--persist" in sys.argv:
        report["persisted_run_id"], report["fixed_prior_bad_cases"] = await persist(metrics, bad_cases)
    target = ROOT / "evaluation/reports/backend-quality-v1.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    gates = [metrics["intent_macro_f1"] >= 0.90, metrics["safety_accuracy"] == 1,
             metrics["multi_turn_accuracy"] >= 0.95, metrics["planning_accuracy"] == 1,
             metrics["prompt_injection_block_rate"] == 1]
    if not all(gates):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
