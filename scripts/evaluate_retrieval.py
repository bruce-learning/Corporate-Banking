"""在冻结检索集上评测真实阿里云向量 + BM25 + RRF。"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.core.rag.hybrid import HybridSearchService
from app.infrastructure.database.store import Store
from app.infrastructure.llm.aliyun import AliyunModelClient
from app.infrastructure.vector.chroma import ChromaVectorStore


def load_cases() -> list[dict]:
    path = ROOT / "datasets" / "generated" / "retrieval_eval.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def metrics(rows: list[dict]) -> dict:
    total = len(rows)
    recall = sum(item["hit"] for item in rows) / total if total else 0.0
    mrr = sum(item["reciprocal_rank"] for item in rows) / total if total else 0.0
    return {"cases": total, "recall_at_3": round(recall, 6), "mrr_at_3": round(mrr, 6)}


async def run(confirm: bool, keyword_only: bool = False, reuse_cache: bool = False) -> dict:
    if not keyword_only and not confirm:
        raise RuntimeError("必须传入 --confirm-paid-call")
    settings = get_settings()
    if not keyword_only and not reuse_cache and not settings.enable_paid_model_calls:
        raise RuntimeError("ENABLE_PAID_MODEL_CALLS 必须仅对本次评测命令显式开启")
    cases = load_cases()
    store = Store(settings.database_url.get_secret_value())
    client = AliyunModelClient(settings)
    vector_store = None if keyword_only else ChromaVectorStore(settings)
    service = HybridSearchService(store, None if keyword_only else client, vector_store)
    try:
        cache = ROOT / "evaluation" / "cache" / "retrieval-v2-query-vectors.json"
        if keyword_only:
            vectors = [None for _ in cases]
        elif reuse_cache:
            vectors = json.loads(cache.read_text(encoding="utf-8"))
        else:
            vectors = []
            for start in range(0, len(cases), 10):
                vectors.extend((await client.embed([item["query"] for item in cases[start:start + 10]])).vectors)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(vectors, separators=(",", ":")), encoding="utf-8")
        if len(vectors) != len(cases):
            raise RuntimeError("查询向量缓存与冻结评测集数量不一致")
        details = []
        for case, vector in zip(cases, vectors):
            response = await service.search(case["query"], domain=case["domain"], top_k=3, query_vector=vector)
            ids = [item.id for item in response.items]
            ranks = [ids.index(expected) + 1 for expected in case["expected_ids"] if expected in ids]
            rank = min(ranks) if ranks else None
            details.append({
                "id": case["id"], "split": case["split"], "domain": case["domain"],
                "expected_ids": case["expected_ids"], "retrieved_ids": ids,
                "hit": rank is not None, "reciprocal_rank": 1.0 / rank if rank else 0.0,
                "mode": response.mode,
            })
        grouped = defaultdict(list)
        for item in details:
            grouped["split:" + item["split"]].append(item)
            grouped["domain:" + item["domain"]].append(item)
        candidates = await service.candidates()
        leaked_controls = [row[0].id for row in candidates if row[0].metadata_json.get("must_not_retrieve")]
        report = {
            "report_version": "retrieval-v2",
            "data_version": "haiyue-corporate-banking-v2",
            "mode": "bm25" if keyword_only else "bm25+aliyun_embedding+weighted_rrf",
            "embedding_model": settings.dashscope_embedding_model,
            "embedding_dimension": settings.dashscope_embedding_dimension,
            "vector_backend": "none" if keyword_only else "chromadb",
            "query_embedding_requests": 0 if keyword_only or reuse_cache else (len(cases) + 9) // 10,
            "query_vectors_reused": bool(reuse_cache),
            "overall": metrics(details),
            "groups": {name: metrics(rows) for name, rows in sorted(grouped.items())},
            "eligible_candidates": len(candidates),
            "control_leakage": leaked_controls,
            "details": details,
        }
        output_name = "retrieval-v2-keyword.json" if keyword_only else "retrieval-v2.json"
        output = ROOT / "evaluation" / "reports" / output_name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if report["overall"]["recall_at_3"] < 0.90 or report["overall"]["mrr_at_3"] < 0.85 or leaked_controls:
            raise RuntimeError("冻结检索评测未达到基线或存在过滤泄漏")
        return {key: value for key, value in report.items() if key != "details"}
    finally:
        if vector_store:
            await vector_store.close()
        await client.aclose()
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid-call", action="store_true")
    parser.add_argument("--keyword-only", action="store_true")
    parser.add_argument("--reuse-query-vector-cache", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(
        args.confirm_paid_call, keyword_only=args.keyword_only,
        reuse_cache=args.reuse_query_vector_cache,
    )), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
