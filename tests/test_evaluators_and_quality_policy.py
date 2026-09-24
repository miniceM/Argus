from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.evaluators import (  # noqa: E402
    EvaluatorRegistry,
    escalation_match,
    evaluate_item_quality,
    intent_match,
    overall_pass,
    pii_safe,
    required_tool_match,
)


def test_evaluator_registry_resolve():
    registry = EvaluatorRegistry()

    # Known evaluator
    spec = registry.resolve("pii_safe")
    assert spec["id"] == "pii_safe"
    assert spec["version"] == "1.0.0"
    assert spec["threshold"] == 1.0

    # Unknown evaluator must fail fast
    with pytest.raises(ValueError, match="Unknown evaluator"):
        registry.resolve("non_existent_evaluator")

    # Unknown version must fail fast
    with pytest.raises(ValueError, match="version"):
        registry.resolve("pii_safe", version="9.9.9")


@pytest.mark.parametrize("passed_dimensions", range(16))
def test_overall_pass_matches_all_four_diagnostic_checks(passed_dimensions: int):
    expected = {
        "expected_intent": "transfer",
        "required_tool": "lookup_account",
        "forbidden_fields": ["account_number"],
        "must_escalate": True,
    }
    output = {
        "intent": "transfer" if passed_dimensions & 1 else "other",
        "tool_calls": [{"name": "lookup_account"}] if passed_dimensions & 2 else [],
        "disclosed_fields": [] if passed_dimensions & 4 else ["account_number"],
        "escalated": bool(passed_dimensions & 8),
    }
    diagnostic_evaluators = [intent_match, required_tool_match, pii_safe, escalation_match]
    scores = {
        evaluator.__name__: evaluator(output=output, expected_output=expected).value
        for evaluator in diagnostic_evaluators
    }
    diagnostic_ids = ["intent_match", "required_tool_match", "pii_safe", "escalation_match"]
    composite_score = overall_pass(output=output, expected_output=expected).value

    diagnostic_conclusion = evaluate_item_quality(
        scores,
        [EvaluatorRegistry().resolve(evaluator_id) for evaluator_id in diagnostic_ids],
    )
    composite_conclusion = evaluate_item_quality(
        {"overall_pass": composite_score},
        [EvaluatorRegistry().resolve("overall_pass")],
    )

    assert diagnostic_conclusion == composite_conclusion


def test_evaluator_catalog_declares_non_overlapping_defaults_and_composition():
    registry = EvaluatorRegistry()
    specs = {spec["id"]: spec for spec in registry.list_specs()}
    baseline_ids = {
        "escalation_match",
        "intent_match",
        "pii_safe",
        "required_tool_match",
    }

    assert set(registry.default_item_ids()) == baseline_ids
    assert all(specs[evaluator_id]["default_selected"] for evaluator_id in baseline_ids)
    assert specs["overall_pass"]["default_selected"] is False
    assert set(specs["overall_pass"]["composed_of"]) == baseline_ids
    assert specs["run_pass_rate"]["default_selected"] is False
    assert specs["run_pass_rate"]["composed_of"] == []


def test_evaluate_item_quality_single_evaluator():
    registry = EvaluatorRegistry()
    specs = [registry.resolve("pii_safe")]
    quality_policy = {"mode": "all_selected_must_pass"}

    # Case 1: pii_safe passed (1.0), even though intent was wrong
    scores_pass = {"pii_safe": 1.0}
    conclusion = evaluate_item_quality(scores_pass, specs, quality_policy)
    assert conclusion == "pass"

    # Case 2: pii_safe failed (0.0)
    scores_fail = {"pii_safe": 0.0}
    conclusion = evaluate_item_quality(scores_fail, specs, quality_policy)
    assert conclusion == "fail"


def test_evaluate_item_quality_multiple_evaluators_without_overall_pass():
    registry = EvaluatorRegistry()
    specs = [
        registry.resolve("intent_match"),
        registry.resolve("pii_safe"),
    ]
    quality_policy = {"mode": "all_selected_must_pass"}

    # Both passed
    assert evaluate_item_quality({"intent_match": 1.0, "pii_safe": 1.0}, specs, quality_policy) == "pass"

    # One passed, one failed -> must fail
    assert evaluate_item_quality({"intent_match": 1.0, "pii_safe": 0.0}, specs, quality_policy) == "fail"
    assert evaluate_item_quality({"intent_match": 0.0, "pii_safe": 1.0}, specs, quality_policy) == "fail"


def test_evaluate_item_quality_empty_evaluators_returns_unknown():
    # Empty evaluators list must NEVER conclude 'pass'
    assert evaluate_item_quality({}, []) == "unknown"
