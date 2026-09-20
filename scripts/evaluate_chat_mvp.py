"""离线评测确定性意图与安全层，不产生付费模型调用。"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.intent.recognizer import LABELS, IntentRecognizer
from app.core.safety.risk import inspect_risk


def rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def f1_for(label: str, expected: list[str], predicted: list[str]):
    tp = sum(a == label and b == label for a, b in zip(expected, predicted))
    fp = sum(a != label and b == label for a, b in zip(expected, predicted))
    fn = sum(a == label and b != label for a, b in zip(expected, predicted))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


async def main():
    intent_rows = rows(ROOT / "datasets/generated/intent_eval.jsonl")
    safety_rows = rows(ROOT / "datasets/generated/chat_safety_eval.jsonl")
    recognizer = IntentRecognizer(model_client=None)
    expected, predicted, failures = [], [], []
    for row in intent_rows:
        result = await recognizer.recognize(row["text"])
        expected.append(row["label"])
        predicted.append(result.label)
        if result.label != row["label"]:
            failures.append({"id": row["id"], "expected": row["label"], "predicted": result.label})
    f1 = {label: f1_for(label, expected, predicted) for label in LABELS}
    safety_failures = []
    redaction_failures = []
    for row in safety_rows:
        result = inspect_risk(row["text"])
        if result.blocked != row["blocked"] or row["expected_flag"] not in result.flags:
            safety_failures.append(row["id"])
        if row["expected_flag"] == "credential_or_secret" and any(token in result.safe_message for token in ["123456", "778899", "556677", "112233", "abc123456", "pass9988"]):
            redaction_failures.append(row["id"])
    report = {
        "evaluation": "chat-mvp-v1-offline", "synthetic": True,
        "paid_model_calls": 0, "intent_cases": len(intent_rows),
        "intent_accuracy": sum(a == b for a, b in zip(expected, predicted)) / len(expected),
        "intent_macro_f1": sum(f1.values()) / len(f1), "intent_f1_by_label": f1,
        "intent_failures": failures, "prediction_counts": Counter(predicted),
        "safety_cases": len(safety_rows),
        "safety_rule_accuracy": 1 - len(safety_failures) / len(safety_rows),
        "safety_failures": safety_failures, "credential_redaction_failures": redaction_failures,
        "limitations": [
            "模板化合成集不代表真实客户问题分布。",
            "本报告只评估确定性意图和安全层；真实阿里云、MCP、Chroma 链路另做在线冒烟。",
        ],
    }
    target = ROOT / "evaluation/reports/chat-mvp-v1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=dict))
    if report["intent_macro_f1"] < 0.88 or safety_failures or redaction_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
