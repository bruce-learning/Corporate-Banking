"""小样本真实在线回答评测；生成与 Judge 分开调用，但使用同一模型，结果不得外推。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import random
import re
import sys

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.infrastructure.database.store import Store
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.llm.contracts import ChatInput, ModelMessage
from app.models.entities.storage import EvaluationRun, utcnow

CASES = [
    ("online-product", "企业网银有哪些功能？"),
    ("online-process", "企业网银怎么办理，需要哪些材料？"),
    ("online-policy", "不同产品是怎么收费的？"),
    ("online-complex", "请先介绍银企直联功能，再说明收费限制，之后给出开通流程。"),
]


def parse_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("Judge 未返回 JSON")
    value = json.loads(match.group(0))
    required = ["relevance", "completeness", "faithfulness", "safety"]
    scores = {key: max(0.0, min(1.0, float(value[key]))) for key in required}
    scores["reason"] = str(value.get("reason") or "")[:300]
    return scores


async def main():
    settings = Settings()
    if not settings.enable_paid_model_calls:
        raise RuntimeError("需要显式启用 ENABLE_PAID_MODEL_CALLS")
    password = (ROOT / "var/secrets/admin-initial-password").read_text(encoding="utf-8").strip()
    judge = AliyunModelClient(settings)
    results, blind = [], []
    try:
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=120) as client:
            login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
            login.raise_for_status()
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            for case_id, question in CASES:
                created = await client.post("/api/v1/conversations", headers=headers,
                                            json={"title": f"在线评测-{case_id}"})
                created.raise_for_status(); conversation_id = created.json()["id"]
                try:
                    response = await client.post("/api/v1/chat", headers=headers, json={
                        "conversation_id": conversation_id, "message": question,
                    })
                    response.raise_for_status(); answer = response.json()
                finally:
                    await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)
                evidence = "\n".join(
                    f"[资料:{item['id']}] {item['content']}" for item in answer["evidence"]
                )
                judged = await judge.chat(ChatInput(messages=[
                    ModelMessage(role="system", content=(
                        "你是只读银行合成演示系统的评测器。只输出 JSON，字段 relevance、completeness、"
                        "faithfulness、safety 为 0 到 1，reason 为不超过100字说明。"
                        "faithfulness 只看回答是否能被证据支持；不得因为文风给高分。"
                    )),
                    ModelMessage(role="user", content=f"问题：{question}\n\n回答：{answer['answer']}\n\n证据：{evidence}"),
                ], max_tokens=450))
                scores = parse_json(judged.text)
                results.append({
                    "id": case_id, "question": question, "intent": answer["intent"],
                    "execution_mode": answer["execution_mode"], "degraded": answer["degraded"],
                    "citation_valid": answer["citation_valid"], "citation_coverage": answer["citation_coverage"],
                    "evidence_ids": [item["id"] for item in answer["evidence"]],
                    "scores": scores, "judge_model": judged.model,
                })
                blind.append({
                    "id": case_id, "question": question, "answer": answer["answer"],
                    "evidence": answer["evidence"],
                    "human_scores": {"relevance": None, "completeness": None,
                                     "faithfulness": None, "safety": None},
                    "human_notes": "",
                })
    finally:
        await judge.aclose()
    dimensions = ["relevance", "completeness", "faithfulness", "safety"]
    means = {key: sum(item["scores"][key] for item in results) / len(results) for key in dimensions}
    metrics = {
        "cases": len(results), "score_means": means,
        "citation_valid_rate": sum(item["citation_valid"] for item in results) / len(results),
        "citation_coverage_mean": sum(item["citation_coverage"] for item in results) / len(results),
        "degraded_rate": sum(item["degraded"] for item in results) / len(results),
        "generation_model": settings.dashscope_chat_model,
        "judge_model": settings.dashscope_chat_model,
    }
    report = {
        "evaluation": "answer-judge-v1", "synthetic": True, "metrics": metrics,
        "cases": results,
        "limitations": ["仅4条在线小样本。", "生成模型与Judge相同，存在同模型偏差。",
                        "尚未完成独立人工盲评，不能作为生产质量结论。"],
    }
    report_dir = ROOT / "evaluation/reports"; report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "answer-judge-v1.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    random.Random(20260920).shuffle(blind)
    review_dir = ROOT / "evaluation/human_review"; review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "answer-judge-v1-blind.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in blind), encoding="utf-8"
    )
    store = Store(settings.database_url.get_secret_value())
    try:
        async with store.sessions() as session:
            session.add(EvaluationRun(
                name="真实在线回答小样本 Judge", dataset_version="answer-judge-v1",
                status="completed", metrics_json=metrics,
                limitations_json=report["limitations"], completed_at=utcnow(),
            ))
            await session.commit()
    finally:
        await store.close()
    print(json.dumps({"evaluation": report["evaluation"], "metrics": metrics,
                      "limitations": report["limitations"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
