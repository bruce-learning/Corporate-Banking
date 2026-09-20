from scripts.evaluate_full_system_500 import retrieval_scores, wilson
from scripts.generate_full_system_eval_dataset import build_rows, load_knowledge, validate_rows


def test_full_system_dataset_has_exact_balanced_contract():
    knowledge = load_knowledge()
    rows = build_rows(knowledge)
    profile = validate_rows(rows, knowledge)

    assert profile["rows"] == 500
    assert profile["unique_ids"] == 500
    assert profile["unique_normalized_queries"] == 500
    assert profile["knowledge_eligible"] == 450
    assert profile["case_type_distribution"] == {
        "composite": 90,
        "direct": 180,
        "escalation": 15,
        "greeting": 15,
        "other": 10,
        "paraphrase": 180,
        "safety": 10,
    }
    assert profile["missing_reference_count"] == 0
    assert profile["issues"] == []


def test_retrieval_scores_rank_each_expected_document_inside_its_domain():
    case = {"knowledge_eligible": True,
            "expected_ids": ["product-a", "process-b", "policy-c"]}
    evidence = [
        {"id": "product-x", "domain": "product"},
        {"id": "product-a", "domain": "product"},
        {"id": "process-b", "domain": "process"},
        {"id": "policy-x", "domain": "policy"},
        {"id": "policy-y", "domain": "policy"},
        {"id": "policy-c", "domain": "policy"},
    ]
    recall, mrr, detail = retrieval_scores(case, evidence)

    assert recall == 1.0
    assert round(mrr, 6) == round((0.5 + 1.0 + 1 / 3) / 3, 6)
    assert detail["ranks"]["policy-c"] == 3


def test_wilson_interval_contains_observed_rate():
    lower, upper = wilson(450, 500)
    assert lower < 0.9 < upper
