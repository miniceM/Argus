from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

_COMPARABLE_QUALITY = {"pass", "fail"}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _item_evaluator_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [spec for spec in specs if spec.get("scope", "item") == "item"]


def _numeric_value_from_typed(result: dict[str, Any]) -> float | None:
    """The aggregatable number of one typed result, or None.

    Only a *succeeded numeric* result contributes. Booleans, categories, text,
    failures, skips and missing values are excluded by construction so they can
    never drag a mean toward zero (Issue #82).
    """
    if str(result.get("status", "")).lower() != "succeeded":
        return None
    if str(result.get("result_type", "")).lower() != "numeric":
        return None
    return _finite_number(result.get("value"))


def _item_numeric_values(item: dict[str, Any]) -> dict[str, float]:
    """Numeric measurement per evaluator for one item.

    Typed results are authoritative when present; historical items that only
    carry the legacy ``scores`` map fall back to it.
    """
    typed = item.get("evaluation_results")
    if isinstance(typed, list) and typed:
        values: dict[str, float] = {}
        for result in typed:
            if not isinstance(result, dict):
                continue
            value = _numeric_value_from_typed(result)
            if value is not None:
                values[str(result.get("evaluator_id"))] = value
        return values
    scores = item.get("scores") or {}
    return {
        str(key): number
        for key, raw in scores.items()
        if (number := _finite_number(raw)) is not None
    }


def _item_result_types(item: dict[str, Any]) -> dict[str, str]:
    """Declared result type per evaluator, used to label non-aggregatable metrics."""
    types: dict[str, str] = {}
    typed = item.get("evaluation_results")
    if isinstance(typed, list):
        for result in typed:
            if isinstance(result, dict) and result.get("evaluator_id"):
                types[str(result["evaluator_id"])] = str(result.get("result_type") or "unknown")
    return types


def aggregate_run(items: list[dict[str, Any]], evaluator_specs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build deterministic run-level metrics without conflating execution and quality failures."""
    evaluator_specs = _item_evaluator_specs(evaluator_specs)
    total = len(items)
    evaluated = [
        item
        for item in items
        if str(item.get("execution_status", "")).lower() == "succeeded"
        and str(item.get("eval_status", "")).lower() == "succeeded"
        and str(item.get("quality_conclusion", "")).lower() in _COMPARABLE_QUALITY
    ]
    passed = sum(str(item.get("quality_conclusion", "")).lower() == "pass" for item in evaluated)
    # Issue #83: the three-way quality split. UNKNOWN is a first-class outcome,
    # never folded into FAIL, so the Console can say "not enough evidence"
    # instead of "the Agent is bad".
    quality_pass_count = sum(
        str(item.get("quality_conclusion", "")).lower() == "pass" for item in items
    )
    quality_fail_count = sum(
        str(item.get("quality_conclusion", "")).lower() == "fail" for item in items
    )
    quality_unknown_count = sum(
        str(item.get("quality_conclusion", "")).lower() not in {"pass", "fail"} for item in items
    )
    decided_count = quality_pass_count + quality_fail_count
    execution_errors = sum(
        str(item.get("execution_status", "")).lower() in {"failed", "timed_out"} for item in items
    )
    evaluator_errors = sum(
        str(item.get("execution_status", "")).lower() == "succeeded"
        and str(item.get("eval_status", "")).lower() == "failed"
        for item in items
    )

    score_values: dict[str, list[float]] = {str(spec["id"]): [] for spec in evaluator_specs}
    critical_failed_cases: set[str] = set()
    spec_by_id = {str(spec["id"]): spec for spec in evaluator_specs}
    result_types: dict[str, str] = {
        str(spec["id"]): str(spec.get("result_type") or "numeric") for spec in evaluator_specs
    }
    for item in items:
        if str(item.get("execution_status", "")).lower() != "succeeded" or str(item.get("eval_status", "")).lower() != "succeeded":
            continue
        numeric_values = _item_numeric_values(item)
        item_types = _item_result_types(item)
        case_id = str(item.get("dataset_item_id", item.get("id", "")))
        for evaluator_id, spec in spec_by_id.items():
            # The observed typed result type is more precise than the spec.
            if evaluator_id in item_types:
                result_types[evaluator_id] = item_types[evaluator_id]
            value = numeric_values.get(evaluator_id)
            if value is not None:
                score_values[evaluator_id].append(value)
            if spec.get("critical") and (value is None or value < float(spec.get("threshold", 1.0))):
                critical_failed_cases.add(case_id)

    score_means = {
        evaluator_id: sum(values) / len(values) if values else None
        for evaluator_id, values in score_values.items()
    }
    # Valid sample count per evaluator: how many items actually contributed a
    # number. A non-aggregatable type (text / category / boolean) always shows 0.
    score_counts = {evaluator_id: len(values) for evaluator_id, values in score_values.items()}
    result_type_by_evaluator = {
        evaluator_id: result_types.get(evaluator_id, "numeric") for evaluator_id in score_values
    }

    latencies = sorted(
        latency
        for item in items
        if str(item.get("execution_status", "")).lower() == "succeeded"
        if (latency := _finite_number(item.get("latency_ms"))) is not None and latency >= 0
    )
    # Nearest-rank percentile: rank = ceil(p * n), then convert the 1-based rank to an index.
    p95 = latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None

    cost_rows: list[tuple[Decimal, str, str, str, str]] = []
    cost_reasons: list[str] = []
    for item in items:
        cost = item.get("cost") or {}
        if not isinstance(cost, dict) or cost.get("complete") is not True:
            reason = cost.get("unavailable_reason") if isinstance(cost, dict) else None
            cost_reasons.append(reason or "COST_NOT_RECORDED")
            continue
        try:
            amount = Decimal(str(cost.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            cost_reasons.append("INVALID_COST_EVIDENCE")
            continue
        currency = cost.get("currency")
        source = cost.get("source")
        scope = cost.get("scope")
        policy = cost.get("policy_version")
        if (
            not amount.is_finite()
            or amount < 0
            or not math.isfinite(float(amount))
            or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or not isinstance(source, str)
            or source not in {"provider_reported", "langfuse", "argus_pricing"}
            or not all(isinstance(value, str) and value for value in (scope, policy))
        ):
            cost_reasons.append("INVALID_COST_EVIDENCE")
            continue
        cost_rows.append((amount, currency, source, scope, policy))

    cost_case_count = len(cost_rows)
    cost_coverage = cost_case_count / total if total else None
    cost_currencies = {row[1] for row in cost_rows}
    cost_sources = {row[2] for row in cost_rows}
    cost_scopes = {row[3] for row in cost_rows}
    cost_policies = {row[4] for row in cost_rows}
    cost_reason = None
    total_cost = cost_per_case = None
    cost_currency = next(iter(cost_currencies)) if len(cost_currencies) == 1 else None
    cost_source = next(iter(cost_sources)) if len(cost_sources) == 1 else None
    cost_scope = next(iter(cost_scopes)) if len(cost_scopes) == 1 else None
    cost_policy_version = next(iter(cost_policies)) if len(cost_policies) == 1 else None
    if cost_rows:
        if len(cost_currencies) != 1:
            cost_reason = "MIXED_CURRENCIES"
        elif len(cost_sources) != 1:
            cost_reason = "COST_SOURCE_MISMATCH"
        elif len(cost_scopes) != 1:
            cost_reason = "COST_SCOPE_MISMATCH"
        elif len(cost_policies) != 1:
            cost_reason = "COST_POLICY_MISMATCH"
        else:
            total_amount = sum((row[0] for row in cost_rows), Decimal(0))
            mean_amount = total_amount / cost_case_count
            if math.isfinite(float(total_amount)) and math.isfinite(float(mean_amount)):
                total_cost = float(total_amount)
                cost_per_case = float(mean_amount)
            else:
                cost_reason = "INVALID_COST_EVIDENCE"
    elif total:
        cost_reason = next((reason for reason in cost_reasons if reason != "COST_NOT_RECORDED"), "COST_NOT_RECORDED")
    if cost_reason is None and total and cost_case_count < total:
        cost_reason = "PARTIAL_COST_COVERAGE"

    return {
        "total_cases": total,
        "evaluated_cases": len(evaluated),
        "passed_cases": passed,
        "failed_quality_cases": sum(str(item.get("quality_conclusion", "")).lower() == "fail" for item in evaluated),
        "pass_rate": passed / len(evaluated) if evaluated else None,
        "evaluation_coverage": len(evaluated) / total if total else None,
        # Issue #83: named denominators. `decided_pass_rate` is PASS/(PASS+FAIL)
        # — an UNKNOWN case leaves the denominator instead of dragging it down —
        # and `decision_coverage` is (PASS+FAIL)/total. Both are null when their
        # denominator is zero, so "no data" never renders as 0% or 100%.
        "quality_pass_count": quality_pass_count,
        "quality_fail_count": quality_fail_count,
        "quality_unknown_count": quality_unknown_count,
        "decided_case_count": decided_count,
        "decided_pass_rate": (quality_pass_count / decided_count) if decided_count else None,
        "decision_coverage": (decided_count / total) if total else None,
        "execution_error_count": execution_errors,
        "execution_error_rate": execution_errors / total if total else None,
        "evaluator_error_count": evaluator_errors,
        "critical_failure_count": len(critical_failed_cases),
        "score_means": score_means,
        "score_counts": score_counts,
        # Issue #82: lets the Console explain *why* a metric has no mean
        # (text / category / boolean are not aggregatable).
        "score_result_types": result_type_by_evaluator,
        "p95_latency_ms": p95,
        "total_cost": total_cost,
        "cost_per_case": cost_per_case,
        "cost_currency": cost_currency,
        "cost_case_count": cost_case_count,
        "cost_coverage": cost_coverage,
        "cost_source": cost_source,
        "cost_scope": cost_scope,
        "cost_policy_version": cost_policy_version,
        "cost_partial": bool(total and cost_case_count < total),
        "cost_unavailable_reason": cost_reason,
    }


def _comparable_values(item: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    """Split one item's results into delta-comparable numbers and the rest.

    Issue #86: only a numeric typed result, or a value a *frozen* normalization
    rule mapped onto a number, may produce a delta. Unordered categories and
    free text are reported for diagnosis but never turned into an invented
    numeric difference.
    """
    typed = item.get("evaluation_results")
    if not isinstance(typed, list) or not typed:
        return dict(_item_numeric_values(item)), []

    values: dict[str, float] = {}
    non_numeric: list[str] = []
    for result in typed:
        if not isinstance(result, dict):
            continue
        evaluator_id = str(result.get("evaluator_id"))
        if str(result.get("status", "")).lower() != "succeeded":
            non_numeric.append(evaluator_id)
            continue
        # A frozen normalization rule is the only sanctioned way to order a
        # non-numeric value.
        normalized = _finite_number(result.get("normalized_value"))
        if normalized is not None:
            values[evaluator_id] = normalized
            continue
        if str(result.get("result_type", "")).lower() == "numeric":
            number = _finite_number(result.get("value"))
            if number is not None:
                values[evaluator_id] = number
                continue
        non_numeric.append(evaluator_id)
    return values, sorted(set(non_numeric))


def _contract(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "version", "scope", "threshold", "params", "critical", "direction")
    return sorted(
        [{key: spec.get(key) for key in keys} for spec in specs],
        key=lambda item: (str(item.get("id")), str(item.get("version"))),
    )


def compare_case_results(
    baseline: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    *,
    evaluator_specs: list[dict[str, Any]],
    baseline_evaluators: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify one dataset item pair under an identical dataset/evaluator contract."""
    baseline = baseline or {}
    candidate = candidate or {}
    evaluator_specs = _item_evaluator_specs(evaluator_specs)
    baseline_evaluators = _item_evaluator_specs(baseline_evaluators)
    result: dict[str, Any] = {
        "dataset_item_id": candidate.get("dataset_item_id") or baseline.get("dataset_item_id"),
        "baseline_trace_id": baseline.get("trace_id"),
        "candidate_trace_id": candidate.get("trace_id"),
        "baseline_trace_url": baseline.get("trace_url"),
        "candidate_trace_url": candidate.get("trace_url"),
        "baseline_experiment_url": baseline.get("experiment_url"),
        "candidate_experiment_url": candidate.get("experiment_url"),
        "baseline_quality_conclusion": baseline.get("quality_conclusion", "unknown"),
        "candidate_quality_conclusion": candidate.get("quality_conclusion", "unknown"),
        "baseline_scores": baseline.get("scores") or {},
        "candidate_scores": candidate.get("scores") or {},
        "score_deltas": {},
        "non_numeric_evaluators": [],
    }

    if not baseline or not candidate:
        result.update(classification="NOT_COMPARABLE", reason="CASE_MISSING")
        return result
    digests = (baseline.get("case_digest"), candidate.get("case_digest"))
    if any(not isinstance(digest, str) or not digest for digest in digests):
        result.update(classification="NOT_COMPARABLE", reason="CASE_CONTENT_UNKNOWN")
        return result
    if digests[0] != digests[1]:
        result.update(classification="NOT_COMPARABLE", reason="CASE_CONTENT_CHANGED")
        return result
    if _contract(baseline_evaluators) != _contract(evaluator_specs):
        result.update(classification="NOT_COMPARABLE", reason="EVALUATION_CONTRACT_CHANGED")
        return result
    if any(str(item.get("execution_status", "")).lower() != "succeeded" for item in (baseline, candidate)):
        result.update(classification="NOT_COMPARABLE", reason="EXECUTION_ERROR")
        return result
    if any(str(item.get("eval_status", "")).lower() == "failed" for item in (baseline, candidate)):
        result.update(classification="NOT_COMPARABLE", reason="EVALUATOR_ERROR")
        return result
    if any(str(item.get("eval_status", "")).lower() != "succeeded" for item in (baseline, candidate)):
        result.update(classification="NOT_COMPARABLE", reason="EVALUATION_NOT_COMPLETED")
        return result
    baseline_scores, baseline_non_numeric = _comparable_values(baseline)
    candidate_scores, candidate_non_numeric = _comparable_values(candidate)
    result["non_numeric_evaluators"] = sorted(set(baseline_non_numeric) | set(candidate_non_numeric))
    result["baseline_scores"] = baseline_scores
    result["candidate_scores"] = candidate_scores
    has_regression = False
    has_improvement = False
    for spec in evaluator_specs:
        evaluator_id = str(spec["id"])
        if evaluator_id in result["non_numeric_evaluators"]:
            # Diagnosed qualitatively; no numeric delta may be invented.
            continue
        before = _finite_number(baseline_scores.get(evaluator_id))
        after = _finite_number(candidate_scores.get(evaluator_id))
        if before is None or after is None:
            result.update(classification="NOT_COMPARABLE", reason="SCORE_MISSING")
            return result
        delta = after - before
        result["score_deltas"][evaluator_id] = delta
        if abs(delta) <= 1e-9:
            continue
        higher_is_better = spec.get("direction", "higher_is_better") == "higher_is_better"
        worsened = delta < 0 if higher_is_better else delta > 0
        has_regression |= worsened
        has_improvement |= not worsened

    before_quality = str(baseline.get("quality_conclusion", "unknown")).lower()
    after_quality = str(candidate.get("quality_conclusion", "unknown")).lower()
    if before_quality not in _COMPARABLE_QUALITY or after_quality not in _COMPARABLE_QUALITY:
        result.update(classification="NOT_COMPARABLE", reason="QUALITY_CONCLUSION_UNKNOWN")
    elif before_quality == "pass" and after_quality == "fail":
        result.update(classification="REGRESSION", reason="QUALITY_CONCLUSION_CHANGED")
    elif before_quality == "fail" and after_quality == "pass":
        result.update(classification="IMPROVEMENT", reason="QUALITY_CONCLUSION_CHANGED")
    elif has_regression:
        result.update(
            classification="REGRESSION",
            reason="MIXED_SCORE_CHANGES" if has_improvement else "SCORE_DECREASED",
        )
    elif has_improvement:
        result.update(classification="IMPROVEMENT", reason="SCORE_INCREASED")
    else:
        result.update(classification="UNCHANGED", reason="NO_QUALITY_CHANGE")
    return result
