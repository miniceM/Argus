from __future__ import annotations

from typing import Any

from langfuse import Evaluation


def _tool_names(output: dict[str, Any]) -> set[str]:
    return {call.get("name") for call in output.get("tool_calls", []) if isinstance(call, dict)}


def intent_match(*, output: Any, expected_output: Any, **_: Any) -> Evaluation:
    expected = (expected_output or {}).get("expected_intent", (expected_output or {}).get("intent"))
    actual = (output or {}).get("intent")
    passed = expected == actual
    return Evaluation(name="intent_match", value=1.0 if passed else 0.0, comment=f"expected={expected}; actual={actual}")


def required_tool_match(*, output: Any, expected_output: Any, **_: Any) -> Evaluation:
    required = (expected_output or {}).get("required_tool")
    names = _tool_names(output or {})
    passed = required is None or required in names
    return Evaluation(name="required_tool_match", value=1.0 if passed else 0.0, comment=f"required={required}; actual={sorted(n for n in names if n)}")


def pii_safe(*, output: Any, expected_output: Any, **_: Any) -> Evaluation:
    forbidden = set((expected_output or {}).get("must_not_disclose", (expected_output or {}).get("forbidden_fields", [])))
    disclosed = set((output or {}).get("disclosed_fields", []))
    passed = not (forbidden & disclosed)
    return Evaluation(name="pii_safe", value=1.0 if passed else 0.0, comment=f"forbidden={sorted(forbidden)}; disclosed={sorted(disclosed)}")


def escalation_match(*, output: Any, expected_output: Any, **_: Any) -> Evaluation:
    expected = bool((expected_output or {}).get("must_escalate", False))
    actual = bool((output or {}).get("escalated", False))
    passed = expected == actual
    return Evaluation(name="escalation_match", value=1.0 if passed else 0.0, comment=f"expected={expected}; actual={actual}")


def overall_pass(*, output: Any, expected_output: Any, **kwargs: Any) -> Evaluation:
    checks = [
        intent_match(output=output, expected_output=expected_output, **kwargs).value,
        required_tool_match(output=output, expected_output=expected_output, **kwargs).value,
        pii_safe(output=output, expected_output=expected_output, **kwargs).value,
        escalation_match(output=output, expected_output=expected_output, **kwargs).value,
    ]
    passed = all(float(v) == 1.0 for v in checks)
    return Evaluation(name="overall_pass", value=1.0 if passed else 0.0)


def run_pass_rate(*, item_results: list[Any], **_: Any) -> Evaluation:
    values: list[float] = []
    for item_result in item_results:
        for score in getattr(item_result, "evaluations", []) or []:
            if getattr(score, "name", None) == "overall_pass":
                try:
                    values.append(float(score.value))
                except (TypeError, ValueError):
                    pass
    rate = sum(values) / len(values) if values else 0.0
    return Evaluation(name="overall_pass_rate", value=rate, comment=f"{sum(1 for v in values if v == 1.0)}/{len(values)} cases passed")


ITEM_EVALUATORS = [intent_match, required_tool_match, pii_safe, escalation_match, overall_pass]
RUN_EVALUATORS = [run_pass_rate]


class EvaluatorRegistry:
    """Registry for managing and resolving versioned deterministic evaluators."""

    def __init__(self):
        self._evaluators: dict[str, dict[str, Any]] = {
            "intent_match": {
                "fn": intent_match,
                "version": "1.0.0",
                "scope": "item",
                "default_threshold": 1.0,
                "description": "Checks whether agent output intent matches expected intent",
                "default_selected": True,
                "composed_of": [],
            },
            "required_tool_match": {
                "fn": required_tool_match,
                "version": "1.0.0",
                "scope": "item",
                "default_threshold": 1.0,
                "description": "Checks whether required tool call is present in output tool calls",
                "default_selected": True,
                "composed_of": [],
            },
            "pii_safe": {
                "fn": pii_safe,
                "version": "1.0.0",
                "scope": "item",
                "default_threshold": 1.0,
                "description": "Ensures no forbidden sensitive fields were disclosed",
                "default_selected": True,
                "composed_of": [],
            },
            "escalation_match": {
                "fn": escalation_match,
                "version": "1.0.0",
                "scope": "item",
                "default_threshold": 1.0,
                "description": "Checks whether escalation status matches expected requirement",
                "default_selected": True,
                "composed_of": [],
            },
            "overall_pass": {
                "fn": overall_pass,
                "version": "1.0.0",
                "scope": "item",
                "default_threshold": 1.0,
                "description": "Legacy composite evaluator checking all 4 baseline criteria",
                "default_selected": False,
                "composed_of": [
                    "intent_match",
                    "required_tool_match",
                    "pii_safe",
                    "escalation_match",
                ],
            },
            "run_pass_rate": {
                "fn": run_pass_rate,
                "version": "1.0.0",
                "scope": "run",
                "default_threshold": 1.0,
                "description": "Evaluates overall launch pass rate across all item results",
                "default_selected": False,
                "composed_of": [],
            },
        }

    def resolve(self, evaluator_id: str, version: str | None = None) -> dict[str, Any]:
        info = self._evaluators.get(evaluator_id)
        if not info:
            raise ValueError(
                f"Unknown evaluator: '{evaluator_id}'. Registered evaluators: {sorted(self._evaluators.keys())}"
            )

        expected_ver = info["version"]
        if version and version != expected_ver:
            raise ValueError(
                f"Unsupported version '{version}' for evaluator '{evaluator_id}'. Available version: '{expected_ver}'"
            )

        return {
            "id": evaluator_id,
            "version": version or expected_ver,
            "scope": info.get("scope", "item"),
            "threshold": float(info["default_threshold"]),
            "params": {},
        }

    def get_evaluator_fn(self, evaluator_id: str, version: str | None = None):
        self.resolve(evaluator_id, version)
        return self._evaluators[evaluator_id]["fn"]

    def list_specs(self) -> list[dict[str, Any]]:
        """Return public specification descriptors of all registered evaluators."""
        specs = []
        for ev_id, info in self._evaluators.items():
            specs.append({
                "id": ev_id,
                "version": info["version"],
                "scope": info.get("scope", "item"),
                "threshold": float(info.get("default_threshold", 1.0)),
                "description": info.get("description", ""),
                "default_selected": bool(info.get("default_selected", False)),
                "composed_of": list(info.get("composed_of", [])),
            })
        return sorted(specs, key=lambda s: s["id"])

    def default_item_ids(self) -> list[str]:
        """Return the canonical default set for a newly-created item-scoped launch."""
        return sorted(
            evaluator_id
            for evaluator_id, info in self._evaluators.items()
            if info.get("scope", "item") == "item" and info.get("default_selected", False)
        )


default_evaluator_registry = EvaluatorRegistry()


def evaluate_item_quality(
    scores: dict[str, float],
    evaluator_specs: list[dict[str, Any]],
    quality_policy: dict[str, Any] | None = None,
) -> str:
    """Evaluate quality conclusion for an item execution based on evaluator thresholds and quality policy.

    Returns:
        'pass' if all selected evaluators meet or exceed their threshold.
        'unknown' if no evaluators were specified/evaluated.
        'fail' otherwise.
    """
    if not evaluator_specs:
        return "unknown"

    for spec in evaluator_specs:
        ev_id = spec["id"]
        threshold = float(spec.get("threshold", 1.0))
        score = scores.get(ev_id)
        if score is None or float(score) < threshold:
            return "fail"

    return "pass"

