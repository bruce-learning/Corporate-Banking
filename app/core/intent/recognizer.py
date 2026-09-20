from __future__ import annotations

import json
import math
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.infrastructure.llm.aliyun import ModelServiceError, PaidCallsDisabled
from app.infrastructure.llm.contracts import ChatInput, ModelMessage


Intent = Literal["product", "process", "policy", "composite", "greeting", "escalation", "other"]
LABELS: tuple[str, ...] = ("product", "process", "policy", "composite", "greeting", "escalation", "other")
DOMAIN_LABELS = ("product", "process", "policy")

KEYWORDS = {
    "product": ("功能", "产品", "企业服务", "适合", "支持", "用途", "能做什么", "额度"),
    "process": ("怎么办", "如何办理", "开通", "申请", "材料", "步骤", "流程", "多久", "渠道", "变更", "取消"),
    "policy": ("收费", "费用", "价格", "费率", "利率", "规定", "限制", "合规", "安全", "审批", "有效期"),
    "greeting": ("你好", "您好", "在吗", "早上好", "下午好", "晚上好", "hello", "hi"),
    "escalation": ("人工客服", "转人工", "真人客服", "人工服务", "投诉", "客户经理"),
}

# “产品”本身可能只是收费问题的宾语，不能据此触发 Product Agent。
STRONG_DOMAIN_KEYWORDS = {
    "product": ("功能", "企业服务", "适合", "支持", "用途", "能做什么", "额度"),
    "process": ("怎么办", "如何办理", "开通", "申请", "材料", "步骤", "流程", "渠道", "变更", "取消"),
    "policy": ("收费", "费用", "价格", "费率", "利率", "规定", "限制", "合规", "安全", "审批", "有效期"),
}

PROTOTYPES = {
    "product": "咨询银行对公产品的功能、用途、适用企业、能力或产品条件",
    "process": "咨询对公业务如何申请、办理步骤、所需材料、办理渠道和时效",
    "policy": "咨询收费费率、政策限制、合规安全、审批和有效期",
    "composite": "同时咨询产品功能、办理流程和收费政策等多个领域",
    "greeting": "向客服问好或开始对话",
    "escalation": "要求转接人工客服、客户经理或发起投诉",
    "other": "与银行对公产品咨询无关或无法判断的问题",
}


class IntentDecision(BaseModel):
    label: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    domains: list[Literal["product", "process", "policy"]] = Field(default_factory=list)
    strategy_scores: dict[str, dict[str, float]] = Field(default_factory=dict)
    strategy_weights: dict[str, float] = Field(default_factory=dict)
    degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    model_usage: list[dict] = Field(default_factory=list)


def _pattern_scores(text: str) -> dict[str, float]:
    lowered = text.lower().strip()
    raw: dict[str, float] = {label: 0.0 for label in LABELS}
    for label, words in KEYWORDS.items():
        hits = sum(1 for word in words if word.lower() in lowered)
        raw[label] = min(1.0, hits * 0.38)
    domain_hits = [label for label, words in STRONG_DOMAIN_KEYWORDS.items()
                   if any(word.lower() in lowered for word in words)]
    if len(domain_hits) >= 2:
        raw["composite"] = min(1.0, 0.55 + 0.18 * len(domain_hits))
    if len(lowered) <= 8 and raw["greeting"]:
        raw["greeting"] = 1.0
    if max(raw.values()) == 0:
        raw["other"] = 0.72
    total = sum(raw.values()) or 1.0
    return {label: round(value / total, 6) for label, value in raw.items()}


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no json object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("not object")
    return value


class IntentRecognizer:
    def __init__(self, model_client=None, threshold: float = 0.58):
        self.model_client = model_client
        self.threshold = threshold
        self._prototype_vectors: list[list[float]] | None = None

    async def _embedding_scores(self, message: str) -> tuple[dict[str, float], list[dict]]:
        usage = []
        if self._prototype_vectors is None:
            prototype = await self.model_client.embed(list(PROTOTYPES.values()))
            self._prototype_vectors = prototype.vectors
            usage.append({"operation": "intent_prototypes", "model": prototype.model,
                          "input_tokens": prototype.input_tokens, "output_tokens": 0})
        query_output = await self.model_client.embed([message])
        query = query_output.vectors[0]
        usage.append({"operation": "intent_embedding", "model": query_output.model,
                      "input_tokens": query_output.input_tokens, "output_tokens": 0})
        similarities = [max(0.0, _cosine(query, vector)) for vector in self._prototype_vectors]
        total = sum(similarities) or 1.0
        return {label: value / total for label, value in zip(LABELS, similarities)}, usage

    async def _llm_scores(self, message: str) -> tuple[dict[str, float], dict]:
        output = await self.model_client.chat(ChatInput(messages=[
            ModelMessage(role="system", content=(
                "你是银行对公咨询意图分类器。只输出 JSON 对象，键必须是 product、process、policy、"
                "composite、greeting、escalation、other，值为 0 到 1 的概率，总和为 1。"
                "不要回答用户问题。"
            )),
            ModelMessage(role="user", content=message),
        ], max_tokens=220))
        data = _json_object(output.text)
        scores = {label: max(0.0, float(data.get(label, 0.0))) for label in LABELS}
        total = sum(scores.values())
        if total <= 0:
            raise ValueError("empty scores")
        return ({label: value / total for label, value in scores.items()},
                {"operation": "intent_llm", "model": output.model,
                 "input_tokens": output.input_tokens, "output_tokens": output.output_tokens})

    async def recognize(self, message: str) -> IntentDecision:
        pattern = _pattern_scores(message)
        lowered = message.lower()
        strong_domains = [label for label, words in STRONG_DOMAIN_KEYWORDS.items()
                          if any(word.lower() in lowered for word in words)]
        strategies: dict[str, dict[str, float]] = {"pattern": pattern}
        configured = {"pattern": 0.45, "embedding": 0.25, "llm": 0.30}
        reasons: list[str] = []
        model_usage: list[dict] = []
        if self.model_client is not None:
            try:
                strategies["embedding"], usage = await self._embedding_scores(message)
                model_usage.extend(usage)
            except PaidCallsDisabled:
                reasons.append("intent_embedding_paid_calls_disabled")
            except ModelServiceError as error:
                reasons.append("intent_embedding_" + error.category)
            except Exception:
                reasons.append("intent_embedding_unavailable")
            try:
                strategies["llm"], usage = await self._llm_scores(message)
                model_usage.append(usage)
            except PaidCallsDisabled:
                reasons.append("intent_llm_paid_calls_disabled")
            except ModelServiceError as error:
                reasons.append("intent_llm_" + error.category)
            except Exception:
                reasons.append("intent_llm_invalid")
        available_total = sum(configured[name] for name in strategies)
        weights = {name: configured[name] / available_total for name in strategies}
        fused = {label: sum(scores[label] * weights[name] for name, scores in strategies.items()) for label in LABELS}
        label = max(LABELS, key=lambda item: fused[item])
        confidence = fused[label]
        if len(strong_domains) >= 2:
            label = "composite"
            confidence = max(confidence, pattern["composite"])
        elif len(strong_domains) == 1 and label not in {"greeting", "escalation"}:
            label = strong_domains[0]
            confidence = max(confidence, pattern[label])
        elif confidence < self.threshold and label not in {"greeting", "escalation", "composite"}:
            label = "other"
            confidence = max(confidence, fused["other"])
        domains = list(strong_domains)
        if label in DOMAIN_LABELS:
            domains = [label]
        elif label == "composite" and len(domains) < 2:
            domains = list(DOMAIN_LABELS)
        return IntentDecision(
            label=label, confidence=min(1.0, round(confidence, 6)), domains=domains,
            strategy_scores={name: {key: round(value, 6) for key, value in scores.items()} for name, scores in strategies.items()},
            strategy_weights={name: round(value, 6) for name, value in weights.items()},
            degraded=len(strategies) < 3, degradation_reasons=reasons, model_usage=model_usage,
        )
