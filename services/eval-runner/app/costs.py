from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

COST_POLICY_VERSION = "case-cost-v1"
_COST_SOURCES = {"provider_reported", "langfuse", "argus_pricing"}
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _path_value(value: Any, path: str | None) -> Any:
    if not path:
        return None
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _token(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (int, float, str, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0 or not math.isfinite(float(result)):
        return None
    return result


def extract_usage_cost(payload: Any, mapping: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Extract only explicitly mapped cost evidence; never infer an amount from usage."""
    if not mapping or not isinstance(payload, Mapping):
        return None

    input_tokens = _token(_path_value(payload, mapping.get("input_tokens_path")))
    output_tokens = _token(_path_value(payload, mapping.get("output_tokens_path")))
    raw_total = _path_value(payload, mapping.get("total_tokens_path"))
    total_tokens = _token(raw_total)
    if raw_total is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    raw_amount = _path_value(payload, mapping.get("amount_path"))
    raw_currency = _path_value(payload, mapping.get("currency_path"))
    amount = _amount(raw_amount)
    currency = raw_currency.upper() if isinstance(raw_currency, str) else None
    source = mapping.get("source", "provider_reported")
    scope = mapping.get("measurement_scope")

    unavailable_reason = None
    if raw_amount is None:
        unavailable_reason = "COST_NOT_RECORDED"
    elif (
        amount is None
        or currency is None
        or not _CURRENCY.fullmatch(currency)
        or not isinstance(source, str)
        or source not in _COST_SOURCES
        or not isinstance(scope, str)
        or not scope
    ):
        unavailable_reason = "INVALID_COST_EVIDENCE"
        amount = None
        currency = None

    # No evidence at all is represented as NULL on the Attempt for legacy compatibility.
    if input_tokens is None and output_tokens is None and total_tokens is None and raw_amount is None:
        return None
    return {
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "cost": {
            "amount": format(amount, "f") if amount is not None else None,
            "currency": currency if amount is not None else None,
            "source": source if amount is not None else None,
            "measurement_scope": scope if amount is not None else None,
            "unavailable_reason": unavailable_reason,
        },
    }


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def aggregate_attempt_costs(attempts: list[Any]) -> dict[str, Any]:
    """Freeze all-attempt cost for one Launch/Case; one unknown attempt makes total unknown."""
    selected = sorted(
        attempts,
        key=lambda attempt: (
            _get(attempt, "dispatch_generation", 0),
            _get(attempt, "attempt_no", 0),
            str(_get(attempt, "id", "")),
        ),
    )
    evidence = []
    parsed: list[tuple[Decimal, str, str, str]] = []
    usage_values: dict[str, list[int | None]] = {key: [] for key in ("input_tokens", "output_tokens", "total_tokens")}
    for attempt in selected:
        raw = _get(attempt, "usage_cost")
        usage = raw.get("usage", {}) if isinstance(raw, Mapping) else {}
        cost = raw.get("cost", {}) if isinstance(raw, Mapping) else {}
        for key in usage_values:
            usage_values[key].append(_token(usage.get(key)) if isinstance(usage, Mapping) else None)
        amount = _amount(cost.get("amount")) if isinstance(cost, Mapping) else None
        currency = cost.get("currency") if isinstance(cost, Mapping) else None
        source = cost.get("source") if isinstance(cost, Mapping) else None
        scope = cost.get("measurement_scope") if isinstance(cost, Mapping) else None
        reason = cost.get("unavailable_reason") if isinstance(cost, Mapping) else "COST_NOT_RECORDED"
        valid_cost = (
            amount is not None
            and isinstance(currency, str)
            and _CURRENCY.fullmatch(currency)
            and isinstance(source, str)
            and source in _COST_SOURCES
            and isinstance(scope, str)
            and bool(scope)
        )
        if valid_cost:
            parsed.append((amount, currency, source, scope))
            reason = None
        evidence.append({
            "attempt_id": str(_get(attempt, "id", "")),
            "attempt_no": _get(attempt, "attempt_no"),
            "dispatch_generation": _get(attempt, "dispatch_generation"),
            "amount": format(amount, "f") if amount is not None else None,
            "currency": currency,
            "source": source,
            "measurement_scope": scope,
            "usage": {
                key: _token(usage.get(key)) if isinstance(usage, Mapping) else None
                for key in ("input_tokens", "output_tokens", "total_tokens")
            },
            "unavailable_reason": None if valid_cost else reason or "COST_NOT_RECORDED",
        })

    complete = bool(selected) and len(parsed) == len(selected)
    reason = None
    amount_text = currency = source = scope = None
    if not selected:
        reason = "COST_NOT_RECORDED"
    elif not complete:
        reason = "INVALID_COST_EVIDENCE" if any(row["unavailable_reason"] == "INVALID_COST_EVIDENCE" for row in evidence) else "INCOMPLETE_ATTEMPT_COST"
    else:
        currencies = {row[1] for row in parsed}
        sources = {row[2] for row in parsed}
        scopes = {row[3] for row in parsed}
        if len(currencies) != 1:
            reason = "MIXED_CURRENCIES"
        elif len(sources) != 1:
            reason = "COST_SOURCE_MISMATCH"
        elif len(scopes) != 1:
            reason = "COST_SCOPE_MISMATCH"
        else:
            amount_text = format(sum((row[0] for row in parsed), Decimal(0)), "f")
            currency = next(iter(currencies))
            source = next(iter(sources))
            scope = next(iter(scopes))

    usage = {
        key: sum(values) if values and all(value is not None for value in values) else None
        for key, values in usage_values.items()
    }
    if usage["total_tokens"] is None and usage["input_tokens"] is not None and usage["output_tokens"] is not None:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

    return {
        "usage": usage,
        "cost": {
            "amount": amount_text,
            "currency": currency,
            "source": source,
            "scope": "launch_case_total",
            "policy_version": COST_POLICY_VERSION,
            "measurement_scope": scope,
            "complete": amount_text is not None,
            "unavailable_reason": reason,
        },
        "cost_evidence": {
            "policy_version": COST_POLICY_VERSION,
            "attempt_count": len(selected),
            "recorded_attempt_count": len(parsed),
            "dispatch_generations": sorted({
                _get(attempt, "dispatch_generation") for attempt in selected
            }),
            "attempts": evidence,
        },
    }


def compare_costs(baseline: Mapping[str, Any] | None, candidate: Mapping[str, Any], cohort_case_count: int) -> dict[str, Any]:
    """Compare only complete costs measured on the same exact case cohort and contract."""
    baseline = baseline or {}
    candidate = candidate or {}
    result = {
        "status": "NOT_COMPARABLE",
        "reason": None,
        "cohort": "quality_comparable_cases",
        "case_count": cohort_case_count,
        "currency": candidate.get("cost_currency") or baseline.get("cost_currency"),
        "baseline_cost_per_case": baseline.get("cost_per_case"),
        "candidate_cost_per_case": candidate.get("cost_per_case"),
        "delta": None,
        "baseline_coverage": baseline.get("cost_coverage"),
        "candidate_coverage": candidate.get("cost_coverage"),
        "policy_version": candidate.get("cost_policy_version"),
    }
    incompatible_reasons = {
        "INVALID_COST_EVIDENCE",
        "INCOMPLETE_ATTEMPT_COST",
        "MIXED_CURRENCIES",
        "COST_SOURCE_MISMATCH",
        "COST_SCOPE_MISMATCH",
        "COST_POLICY_MISMATCH",
    }
    checks = (
        (bool(baseline), "BASELINE_NOT_BOUND"),
        (cohort_case_count > 0, "NO_COMPARABLE_CASES"),
        (baseline.get("cost_unavailable_reason") not in incompatible_reasons, baseline.get("cost_unavailable_reason") or "COST_NOT_RECORDED"),
        (candidate.get("cost_unavailable_reason") not in incompatible_reasons, candidate.get("cost_unavailable_reason") or "COST_NOT_RECORDED"),
        (baseline.get("cost_case_count", 0) > 0 and candidate.get("cost_case_count", 0) > 0, "COST_NOT_RECORDED"),
        (baseline.get("cost_case_count") == cohort_case_count and baseline.get("cost_coverage") == 1.0, "PARTIAL_COST_COVERAGE"),
        (candidate.get("cost_case_count") == cohort_case_count and candidate.get("cost_coverage") == 1.0, "PARTIAL_COST_COVERAGE"),
        (baseline.get("cost_currency") == candidate.get("cost_currency") and bool(candidate.get("cost_currency")), "COST_CURRENCY_MISMATCH"),
        (baseline.get("cost_scope") == candidate.get("cost_scope") and bool(candidate.get("cost_scope")), "COST_SCOPE_MISMATCH"),
        (baseline.get("cost_policy_version") == candidate.get("cost_policy_version") and bool(candidate.get("cost_policy_version")), "COST_POLICY_MISMATCH"),
        (baseline.get("cost_source") == candidate.get("cost_source") and bool(candidate.get("cost_source")), "COST_SOURCE_MISMATCH"),
        (isinstance(baseline.get("cost_per_case"), (int, float)) and isinstance(candidate.get("cost_per_case"), (int, float)), "COST_NOT_RECORDED"),
    )
    for passed, reason in checks:
        if not passed:
            result["reason"] = reason
            return result
    result["status"] = "COMPARABLE"
    result["delta"] = candidate["cost_per_case"] - baseline["cost_per_case"]
    return result
