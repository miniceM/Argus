from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.evaluators import (  # noqa: E402
    EvaluatorRegistry,
    evaluate_item_quality,
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
