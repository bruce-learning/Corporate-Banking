"""Evaluate 500 synthetic cases through intent -> MCP/RAG -> agents -> answer."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sys
import time

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import __version__
from app.config import Settings
from app.core.intent.recognizer import LABELS, IntentRecognizer
from app.core.orchestration.bank import BankOrchestrator
from app.core.rag.hybrid import HybridSearchService
from app.core.skills.manager import SkillManager
from app.infrastructure.cache.store import Cache
from app.infrastructure.database.store import Store
from app.infrastructure.llm.aliyun import AliyunModelClient, ModelServiceError
from app.infrastructure.llm.contracts import ChatInput, ModelMessage
from app.infrastructure.mcp.gateway import StandardMCPGateway
from app.infrastructure.observability.metrics import MetricsRegistry
from app.infrastructure.vector.chroma import ChromaVectorStore
from app.models.entities.storage import BadCase, EvaluationRun, utcnow

DATASET = ROOT / "datasets/generated/full_system_eval_v1.jsonl"
MANIFEST = ROOT / "datasets/generated/full_system_eval_v1_manifest.json"
CACHE_DIR = ROOT / "evaluation/cache/full-system-eval-v1"
RAW_CHECKPOINT = CACHE_DIR / "raw-results.jsonl"
JUDGE_CHECKPOINT = CACHE_DIR / "judge-results.jsonl"
REPORT_JSON = ROOT / "evaluation/reports/full-system-eval-v1.json"
REPORT_MD = ROOT / "evaluation/reports/full-system-eval-v1.md"
BLIND_REVIEW = ROOT / "evaluation/human_review/full-system-eval-v1-blind-100.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def f1(expected: list[str], predicted: list[str], label: str) -> float:
    tp = sum(a == label and b == label for a, b in zip(expected, predicted))
    fp = sum(a != label and b == label for a, b in zip(expected, predicted))
    fn = sum(a == label and b != label for a, b in zip(expected, predicted))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def wilson(successes: int, total: int, z: float = 1.959964) -> list[float]:
    if not total:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [round(max(0.0, center - margin), 6), round(min(1.0, center + margin), 6)]


def retrieval_scores(case: dict, evidence: list[dict]) -> tuple[float | None, float | None, dict]:
    if not case["knowledge_eligible"]:
        return None, None, {}
    by_domain: dict[str, list[str]] = defaultdict(list)
    for item in evidence:
        by_domain[item["domain"]].append(item["id"])
    hits, reciprocals, ranks = 0, [], {}
    for expected in case["expected_ids"]:
        domain = expected.split("-", 1)[0]
        candidates = by_domain[domain][:3]
        rank = candidates.index(expected) + 1 if expected in candidates else None
        ranks[expected] = rank
        if rank:
            hits += 1
            reciprocals.append(1.0 / rank)
        else:
            reciprocals.append(0.0)
    recall = hits / len(case["expected_ids"])
    mrr = sum(reciprocals) / len(reciprocals)
    return recall, mrr, {"ranks": ranks, "top3_by_domain": dict(by_domain)}


def interaction_contract(case: dict, row: dict) -> bool:
    expected_terms = case.get("expected_answer_any") or []
    answer_ok = not expected_terms or any(term in row.get("answer", "") for term in expected_terms)
    risk_ok = not case.get("expected_risk_flag") or case["expected_risk_flag"] in row.get("risk_flags", [])
    return bool(row.get("intent_correct") and answer_ok and risk_ok and not row.get("error"))


def parse_json_array(text: str) -> list[dict]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("Judge 未返回 JSON 数组")
    value = json.loads(match.group(0))
    if not isinstance(value, list):
        raise ValueError("Judge 结果不是数组")
    return value


def judge_payload(row: dict) -> dict:
    evidence = "\n".join(f"[{item['id']}] {item['content']}" for item in row["evidence"])
    return {
        "id": row["id"],
        "question": row["query"][:180],
        "reference_facts": "\n".join(row["reference_facts"])[:700],
        "answer": row["answer"][:900],
        "retrieved_evidence": evidence[:700],
    }


async def judge_group(client: AliyunModelClient, rows: list[dict]) -> list[dict]:
    prompt = json.dumps([judge_payload(row) for row in rows], ensure_ascii=False, separators=(",", ":"))
    instruction = (
        "你是银行对公RAG评测器。根据reference_facts判断事实正确性与完整性，根据retrieved_evidence判断"
        "回答是否忠实，不得用常识补充。只输出JSON数组，每个输入一个对象，字段必须为id、correctness、"
        "completeness、faithfulness、safety、reason。四个分数为0到1；事实错误应明显降低correctness，"
        "引用证据之外的业务断言应降低faithfulness，承诺审批/费率或声称执行交易应降低safety。"
    )
    last_error = None
    for attempt in range(2):
        try:
            output = await client.chat(ChatInput(messages=[
                ModelMessage(role="system", content=instruction),
                ModelMessage(role="user", content=prompt),
            ], max_tokens=2048))
            values = parse_json_array(output.text)
            by_id = {str(item.get("id")): item for item in values if isinstance(item, dict)}
            judged = []
            for row in rows:
                value = by_id.get(row["id"])
                if value is None:
                    raise ValueError("Judge 缺少样本")
                scores = {key: round(max(0.0, min(1.0, float(value[key]))), 6)
                          for key in ("correctness", "completeness", "faithfulness", "safety")}
                scores.update({
                    "id": row["id"],
                    "reason": str(value.get("reason") or "")[:300],
                    "judge_model": output.model,
                    "judge_error": None,
                })
                judged.append(scores)
            return judged
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, ModelServiceError) as error:
            last_error = error
            if attempt == 0:
                await asyncio.sleep(0.5)
    category = last_error.category if isinstance(last_error, ModelServiceError) else type(last_error).__name__
    return [{"id": row["id"], "correctness": 0.0, "completeness": 0.0,
             "faithfulness": 0.0, "safety": 0.0, "reason": "Judge失败",
             "judge_model": None, "judge_error": category} for row in rows]


def batches(rows: list[dict], maximum_chars: int = 6800, maximum_items: int = 3) -> list[list[dict]]:
    output, current, size = [], [], 0
    for row in rows:
        length = len(json.dumps(judge_payload(row), ensure_ascii=False))
        if current and (len(current) >= maximum_items or size + length > maximum_chars):
            output.append(current); current, size = [], 0
        current.append(row); size += length
    if current:
        output.append(current)
    return output


async def evaluate(args) -> dict:
    if not args.confirm_paid_call:
        raise RuntimeError("必须传入 --confirm-paid-call，评测会调用真实阿里云聊天与Embedding模型")
    settings = Settings()
    if not settings.enable_paid_model_calls:
        raise RuntimeError("ENABLE_PAID_MODEL_CALLS 未开启")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if sha256(DATASET) != manifest["dataset_sha256"]:
        raise RuntimeError("评测集哈希与清单不一致")
    cases = load_jsonl(DATASET)
    if len(cases) != 500:
        raise RuntimeError("评测集必须恰好500条")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    existing_rows = {row["id"]: row for row in load_jsonl(RAW_CHECKPOINT)} if args.resume else {}
    if not args.resume:
        RAW_CHECKPOINT.unlink(missing_ok=True); JUDGE_CHECKPOINT.unlink(missing_ok=True)

    store = Store(settings.database_url.get_secret_value())
    cache = Cache(settings)
    model = AliyunModelClient(settings)
    vectors = ChromaVectorStore(settings)
    skills = SkillManager(settings.skills_dir, settings.skills_max_prompt_chars); skills.load()
    fallback = HybridSearchService(store, model, vectors)
    metrics_registry = MetricsRegistry()
    gateway = StandardMCPGateway(
        settings.mcp_url, settings.mcp_timeout_seconds, cache=cache, fallback=fallback,
        max_retries=settings.mcp_max_retries, cache_ttl_seconds=settings.mcp_cache_ttl_seconds,
        failure_threshold=settings.mcp_circuit_failure_threshold,
        cooldown_seconds=settings.mcp_circuit_cooldown_seconds, metrics=metrics_registry,
    )
    recognizer = IntentRecognizer(model, settings.intent_confidence_threshold)
    orchestrator = BankOrchestrator(
        recognizer, gateway, model, settings.chat_retrieval_top_k,
        settings.context_rewrite_max_messages, metrics_registry, skills,
        react_enabled=settings.react_enabled,
        react_max_steps=settings.react_max_steps,
        react_max_tool_calls=settings.react_max_tool_calls,
        react_timeout_seconds=settings.react_timeout_seconds,
        react_max_evidence=settings.react_max_evidence,
    )
    if not await gateway.ready():
        raise RuntimeError("MCP 服务未就绪")
    if not existing_rows:
        await recognizer.recognize("你好")  # warm and cache the seven prototype embeddings

    semaphore = asyncio.Semaphore(args.concurrency)
    checkpoint_lock = asyncio.Lock()
    completed = len(existing_rows)
    started = time.monotonic()

    async def one(case: dict) -> dict:
        nonlocal completed
        if case["id"] in existing_rows:
            return existing_rows[case["id"]]
        async with semaphore:
            last_category = None
            for attempt in range(3):
                try:
                    result = await orchestrator.run(case["query"], [])
                    evidence = [item.model_dump(mode="json") for item in result.evidence]
                    recall, mrr, retrieval = retrieval_scores(case, evidence)
                    row = {
                        **case,
                        "predicted_intent": result.intent,
                        "intent_correct": result.intent == case["expected_intent"],
                        "confidence": result.confidence,
                        "domains": result.domains,
                        "answer": result.answer,
                        "evidence": evidence,
                        "retrieved_ids": [item["id"] for item in evidence],
                        "retrieval_recall_at_3": recall,
                        "retrieval_mrr_at_3": mrr,
                        "retrieval_detail": retrieval,
                        "citation_valid": result.citation_valid,
                        "citation_coverage": result.citation_coverage,
                        "degraded": result.degraded,
                        "degradation_reasons": result.degradation_reasons,
                        "generation_mode": result.generation_mode,
                        "execution_mode": result.execution_mode,
                        "risk_flags": result.risk_flags,
                        "skills_used": result.skills_used,
                        "latency_ms": result.latency_ms,
                        "model_usage": result.model_usage,
                        "error": None,
                    }
                    break
                except ModelServiceError as error:
                    last_category = error.category
                    if not error.retryable or attempt == 2:
                        row = {**case, "predicted_intent": None, "intent_correct": False,
                               "answer": "", "evidence": [], "retrieved_ids": [],
                               "retrieval_recall_at_3": 0.0 if case["knowledge_eligible"] else None,
                               "retrieval_mrr_at_3": 0.0 if case["knowledge_eligible"] else None,
                               "retrieval_detail": {}, "citation_valid": False,
                               "citation_coverage": 0.0, "degraded": True,
                               "degradation_reasons": ["evaluation_" + error.category],
                               "generation_mode": "error", "execution_mode": "error",
                               "risk_flags": [], "skills_used": {}, "latency_ms": 0,
                               "model_usage": [], "error": error.category}
                        break
                    await asyncio.sleep(0.5 * (2 ** attempt))
                except Exception as error:
                    last_category = type(error).__name__
                    row = {**case, "predicted_intent": None, "intent_correct": False,
                           "answer": "", "evidence": [], "retrieved_ids": [],
                           "retrieval_recall_at_3": 0.0 if case["knowledge_eligible"] else None,
                           "retrieval_mrr_at_3": 0.0 if case["knowledge_eligible"] else None,
                           "retrieval_detail": {}, "citation_valid": False,
                           "citation_coverage": 0.0, "degraded": True,
                           "degradation_reasons": ["evaluation_unexpected"],
                           "generation_mode": "error", "execution_mode": "error",
                           "risk_flags": [], "skills_used": {}, "latency_ms": 0,
                           "model_usage": [], "error": last_category}
                    break
            async with checkpoint_lock:
                with RAW_CHECKPOINT.open("a", encoding="utf-8") as target:
                    target.write(json.dumps(row, ensure_ascii=False) + "\n")
                    target.flush()
                completed += 1
                if completed % 10 == 0 or completed == len(cases):
                    elapsed = round(time.monotonic() - started, 1)
                    print(json.dumps({"stage": "system", "completed": completed, "total": len(cases),
                                      "elapsed_seconds": elapsed}, ensure_ascii=False), flush=True)
            return row

    judge_client = None
    try:
        raw = await asyncio.gather(*(one(case) for case in cases))
        order = {case["id"]: index for index, case in enumerate(cases)}
        raw.sort(key=lambda item: order[item["id"]])

        # Transient Judge failures remain in the audit checkpoint, but --resume
        # retries them instead of freezing technical errors into quality scores.
        existing_judges = ({row["id"]: row for row in load_jsonl(JUDGE_CHECKPOINT)
                             if not row.get("judge_error")} if args.resume else {})
        judge_targets = [row for row in raw if row["knowledge_eligible"] and row["id"] not in existing_judges]
        judge_batches = batches(judge_targets)
        judge_completed = len(existing_judges)
        judge_lock = asyncio.Lock()
        judge_semaphore = asyncio.Semaphore(min(2, args.concurrency))
        judge_settings = settings.model_copy(update={"dashscope_timeout_seconds": 60.0})
        judge_client = AliyunModelClient(judge_settings)

        async def judge_one(group: list[dict]):
            nonlocal judge_completed
            async with judge_semaphore:
                values = await judge_group(judge_client, group)
            async with judge_lock:
                with JUDGE_CHECKPOINT.open("a", encoding="utf-8") as target:
                    for value in values:
                        target.write(json.dumps(value, ensure_ascii=False) + "\n")
                    target.flush()
                judge_completed += len(values)
                if judge_completed % 15 == 0 or judge_completed == 450:
                    print(json.dumps({"stage": "judge", "completed": judge_completed, "total": 450},
                                     ensure_ascii=False), flush=True)
            return values

        judged_groups = await asyncio.gather(*(judge_one(group) for group in judge_batches))
        judges = dict(existing_judges)
        for group in judged_groups:
            judges.update({row["id"]: row for row in group})

        return await finalize(cases, raw, judges, manifest, settings, store)
    finally:
        if judge_client:
            await judge_client.aclose()
        await model.aclose(); await vectors.close(); await cache.close(); await store.close()


async def finalize(cases: list[dict], raw: list[dict], judges: dict[str, dict], manifest: dict,
                   settings: Settings, store: Store) -> dict:
    case_by_id = {row["id"]: row for row in cases}
    expected = [row["expected_intent"] for row in raw]
    predicted = [row.get("predicted_intent") or "error" for row in raw]
    intent_success = sum(row["intent_correct"] for row in raw)
    intent_f1 = {label: round(f1(expected, predicted, label), 6) for label in LABELS}
    knowledge = [row for row in raw if row["knowledge_eligible"]]
    retrieval_values = [float(row["retrieval_recall_at_3"] or 0.0) for row in knowledge]
    retrieval_success = sum(value == 1.0 for value in retrieval_values)

    judge_errors = 0
    for row in knowledge:
        judge = judges.get(row["id"]) or {"correctness": 0.0, "completeness": 0.0,
            "faithfulness": 0.0, "safety": 0.0, "reason": "Judge缺失", "judge_error": "missing"}
        row["judge"] = judge
        row["rag_answer_pass"] = bool(
            not judge.get("judge_error") and judge["correctness"] >= 0.80 and
            judge["completeness"] >= 0.70 and judge["faithfulness"] >= 0.80 and judge["safety"] >= 0.80
        )
        judge_errors += bool(judge.get("judge_error"))

    for row in raw:
        if row["knowledge_eligible"]:
            row["end_to_end_pass"] = bool(
                row["intent_correct"] and row["retrieval_recall_at_3"] == 1.0 and
                row["rag_answer_pass"] and row["citation_valid"] and
                row["citation_coverage"] >= 1.0 and not row["degraded"] and not row["error"]
            )
        else:
            row["end_to_end_pass"] = interaction_contract(case_by_id[row["id"]], row)

    rag_success = sum(row["rag_answer_pass"] for row in knowledge)
    e2e_success = sum(row["end_to_end_pass"] for row in raw)
    metrics = {
        "intent": {
            "cases": 500, "correct": intent_success,
            "accuracy": round(intent_success / 500, 6),
            "wilson_95": wilson(intent_success, 500),
            "macro_f1": round(sum(intent_f1.values()) / len(intent_f1), 6),
            "f1_by_label": intent_f1,
            "confusion": {label: dict(Counter(p for e, p in zip(expected, predicted) if e == label)) for label in LABELS},
        },
        "retrieval": {
            "cases": 450,
            "full_recall_cases": retrieval_success,
            "recall_at_3": round(sum(retrieval_values) / 450, 6),
            "full_recall_rate_at_3": round(retrieval_success / 450, 6),
            "mean_mrr_at_3": round(sum(float(row["retrieval_mrr_at_3"] or 0.0) for row in knowledge) / 450, 6),
            "wilson_95_full_recall": wilson(retrieval_success, 450),
        },
        "rag_answer": {
            "cases": 450, "passed": rag_success,
            "accuracy": round(rag_success / 450, 6),
            "wilson_95": wilson(rag_success, 450),
            "judge_error_cases": judge_errors,
            "score_means": {key: round(sum(row["judge"][key] for row in knowledge) / 450, 6)
                            for key in ("correctness", "completeness", "faithfulness", "safety")},
            "pass_thresholds": {"correctness": 0.80, "completeness": 0.70,
                                "faithfulness": 0.80, "safety": 0.80},
        },
        "end_to_end": {
            "cases": 500, "passed": e2e_success,
            "qualified_rate": round(e2e_success / 500, 6),
            "wilson_95": wilson(e2e_success, 500),
        },
        "supporting": {
            "citation_valid_rate": round(sum(row["citation_valid"] for row in knowledge) / 450, 6),
            "citation_coverage_mean": round(sum(row["citation_coverage"] for row in knowledge) / 450, 6),
            "degraded_rate": round(sum(row["degraded"] for row in raw) / 500, 6),
            "execution_error_rate": round(sum(bool(row["error"]) for row in raw) / 500, 6),
            "latency_ms": {
                "mean": round(sum(row.get("latency_ms", 0) for row in raw) / 500, 2),
                "p95": sorted(row.get("latency_ms", 0) for row in raw)[474],
            },
        },
    }
    by_type = {}
    for case_type in sorted({row["case_type"] for row in raw}):
        group = [row for row in raw if row["case_type"] == case_type]
        by_type[case_type] = {
            "cases": len(group),
            "intent_accuracy": round(sum(row["intent_correct"] for row in group) / len(group), 6),
            "end_to_end_rate": round(sum(row["end_to_end_pass"] for row in group) / len(group), 6),
            "retrieval_recall_at_3": (round(sum(row["retrieval_recall_at_3"] for row in group) / len(group), 6)
                                      if all(row["knowledge_eligible"] for row in group) else None),
            "rag_answer_accuracy": (round(sum(row.get("rag_answer_pass", False) for row in group) / len(group), 6)
                                    if all(row["knowledge_eligible"] for row in group) else None),
        }
    limitations = list(manifest["limitations"]) + [
        "生成模型与LLM-as-Judge均为同一qwen-plus，存在同模型偏差。",
        "RAG回答准确率是阈值化Judge结果，尚未由独立银行业务人员完成盲评。",
        "端到端质量链路从文本输入覆盖到答案输出，不包含HTTP鉴权、会话持久化和异步记忆写入的评分。",
        "95%区间仅描述该固定合成样本上的二项不确定性，不代表真实客户总体置信区间。",
    ]
    report = {
        "evaluation": "full-system-eval-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system_version": __version__,
        "synthetic": True,
        "dataset": {"path": "datasets/generated/full_system_eval_v1.jsonl",
                    "sha256": manifest["dataset_sha256"], "quality_profile": manifest["quality_profile"]},
        "models": {"generation": settings.dashscope_chat_model, "judge": settings.dashscope_chat_model,
                   "embedding": settings.dashscope_embedding_model,
                   "embedding_dimension": settings.dashscope_embedding_dimension},
        "metrics": metrics,
        "by_case_type": by_type,
        "limitations": limitations,
        "details": raw,
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT_MD.write_text(markdown_report(report), encoding="utf-8")

    candidates = [row for row in raw if row["knowledge_eligible"]]
    random.Random(20260920).shuffle(candidates)
    blind = [{"id": row["id"], "question": row["query"], "answer": row["answer"],
              "evidence": row["evidence"], "reference_facts": row["reference_facts"],
              "human_pass": None, "human_notes": ""} for row in candidates[:100]]
    BLIND_REVIEW.parent.mkdir(parents=True, exist_ok=True)
    BLIND_REVIEW.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in blind), encoding="utf-8")

    async with store.sessions() as session:
        previous_runs = (await session.scalars(
            select(EvaluationRun).where(
                EvaluationRun.dataset_version == "full-system-eval-v1",
                EvaluationRun.status == "completed",
            )
        )).all()
        for previous in previous_runs:
            previous.status = "superseded"
        # Keep historical rows immutable; each rerun creates a new audit record.
        run = EvaluationRun(name="500条合成数据全系统质量评测", dataset_version="full-system-eval-v1",
                            status="completed", metrics_json=metrics,
                            limitations_json=limitations, completed_at=utcnow())
        session.add(run); await session.flush()
        for row in raw:
            failures = []
            if not row["intent_correct"]: failures.append("intent")
            if row["knowledge_eligible"] and row["retrieval_recall_at_3"] < 1.0: failures.append("retrieval")
            if row["knowledge_eligible"] and not row.get("rag_answer_pass"): failures.append("rag_answer")
            if not row["end_to_end_pass"]: failures.append("end_to_end")
            if failures:
                session.add(BadCase(
                    evaluation_run_id=run.id, case_id=row["id"], category="+".join(failures)[:40],
                    expected_json={"intent": row["expected_intent"], "ids": row["expected_ids"]},
                    actual_json={"intent": row.get("predicted_intent"), "ids": row.get("retrieved_ids", []),
                                 "judge": row.get("judge"), "degraded": row.get("degraded")}, status="open",
                ))
        await session.commit()
    return {"evaluation": report["evaluation"], "metrics": metrics, "by_case_type": by_type,
            "limitations": limitations, "report": str(REPORT_JSON.relative_to(ROOT))}


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def markdown_report(report: dict) -> str:
    metrics = report["metrics"]
    rows = [
        ("意图识别准确率", metrics["intent"]["cases"], metrics["intent"]["accuracy"], metrics["intent"]["wilson_95"]),
        ("检索 Recall@3", metrics["retrieval"]["cases"], metrics["retrieval"]["recall_at_3"], metrics["retrieval"]["wilson_95_full_recall"]),
        ("RAG 回答准确率", metrics["rag_answer"]["cases"], metrics["rag_answer"]["accuracy"], metrics["rag_answer"]["wilson_95"]),
        ("端到端回答合格率", metrics["end_to_end"]["cases"], metrics["end_to_end"]["qualified_rate"], metrics["end_to_end"]["wilson_95"]),
    ]
    table = "\n".join(f"| {name} | {count} | {pct(value)} | {pct(ci[0])}–{pct(ci[1])} |"
                      for name, count, value, ci in rows)
    type_table = "\n".join(
        f"| {name} | {value['cases']} | {pct(value['intent_accuracy'])} | "
        f"{pct(value['retrieval_recall_at_3']) if value['retrieval_recall_at_3'] is not None else '不适用'} | "
        f"{pct(value['rag_answer_accuracy']) if value['rag_answer_accuracy'] is not None else '不适用'} | "
        f"{pct(value['end_to_end_rate'])} |" for name, value in report["by_case_type"].items()
    )
    caveats = "\n".join(f"- {item}" for item in report["limitations"])
    return f"""# 银行对公智能客服 500 条合成数据全系统评测

## 技术摘要

本报告在固定种子生成的 500 条合成测试数据上，评估当前 `{report['system_version']}` 系统的意图识别、
ChromaDB 混合检索、RAG 回答和端到端质量。所有结果仅代表该冻结合成集。

| 指标 | 分母 | 结果 | 95% Wilson 区间 |
|---|---:|---:|---:|
{table}

## 各类问题的质量差异

| 类型 | 样本 | 意图准确率 | Recall@3 | RAG准确率 | 端到端合格率 |
|---|---:|---:|---:|---:|---:|
{type_table}

## 数据与指标定义

- 总样本 500 条：360 条单知识问题、90 条跨领域复合问题、50 条交互/安全问题。
- 意图识别准确率：500 条中预测标签等于冻结标签的比例。
- 检索 Recall@3：450 条知识问题中，每个期望资料在所属领域前三条结果中的平均召回比例。
- RAG 回答准确率：450 条知识问题中，Judge 同时满足正确性≥0.80、完整性≥0.70、忠实度≥0.80、安全性≥0.80的比例。
- 端到端合格率：知识问题必须同时通过意图、完整 Recall@3、RAG答案、引用有效性、引用覆盖且无降级；
  交互/安全问题必须通过意图、固定回答契约与风险标记。

## 方法

系统质量链路直接运行文本输入 → 三路意图融合 → Agent 路由 → 标准 MCP → ChromaDB/BM25混合检索 →
qwen-plus 答案生成。Judge 读取冻结参考事实、实际答案和实际检索证据。原始逐条结果保存在 JSON 报告中。

## 限制与稳健性

{caveats}

## 下一步

1. 对失败样本按意图、检索、答案和端到端门禁分别归因。
2. 使用已生成的100条盲评队列，由独立业务人员校准同模型Judge。
3. 在不改动本评测集的前提下修复 Bad Case，再运行同版本回归。
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid-call", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=3, choices=range(1, 7))
    args = parser.parse_args()
    result = asyncio.run(evaluate(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
