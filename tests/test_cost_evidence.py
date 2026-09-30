from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.aggregation import aggregate_run  # noqa: E402
from app.costs import aggregate_attempt_costs, compare_costs, extract_usage_cost  # noqa: E402
from app.models import ComparisonResponse, RunSummaryResponse  # noqa: E402

MAPPING = {
    "input_tokens_path": "usage.input_tokens",
    "output_tokens_path": "usage.output_tokens",
    "total_tokens_path": "usage.total_tokens",
    "amount_path": "billing.cost",
    "currency_path": "billing.currency",
    "source": "provider_reported",
    "measurement_scope": "agent_invocation_total",
}


def test_extracts_usage_and_zero_cost_without_treating_missing_as_zero():
    evidence = extract_usage_cost(
        {"usage": {"input_tokens": 0, "output_tokens": 0}, "billing": {"cost": 0, "currency": "usd"}},
        MAPPING,
    )

    assert evidence["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert evidence["cost"] == {
        "amount": "0",
        "currency": "USD",
        "source": "provider_reported",
        "measurement_scope": "agent_invocation_total",
        "unavailable_reason": None,
    }
    missing = extract_usage_cost({"usage": {"input_tokens": 4}}, MAPPING)
    assert missing["usage"]["input_tokens"] == 4
    assert missing["cost"]["amount"] is None
    assert missing["cost"]["unavailable_reason"] == "COST_NOT_RECORDED"


def test_rejects_invalid_cost_and_does_not_sum_unknown_retry_attempt():
    invalid = extract_usage_cost(
        {"billing": {"cost": -0.2, "currency": "USD"}}, MAPPING
    )
    assert invalid["cost"]["amount"] is None
    assert invalid["cost"]["unavailable_reason"] == "INVALID_COST_EVIDENCE"

    rolled = aggregate_attempt_costs(
        [
            {"id": "a1", "attempt_no": 1, "dispatch_generation": 3,
             "usage_cost": extract_usage_cost({"billing": {"cost": 0.003, "currency": "USD"}}, MAPPING)},
            {"id": "a2", "attempt_no": 2, "dispatch_generation": 3,
             "usage_cost": None},
            {"id": "old", "attempt_no": 1, "dispatch_generation": 2,
             "usage_cost": extract_usage_cost({"billing": {"cost": 9, "currency": "USD"}}, MAPPING)},
        ],
    )
    assert rolled["cost"]["amount"] is None
    assert rolled["cost"]["complete"] is False
    assert rolled["cost"]["unavailable_reason"] == "INCOMPLETE_ATTEMPT_COST"
    assert rolled["cost_evidence"]["attempt_count"] == 3
    assert rolled["cost_evidence"]["dispatch_generations"] == [2, 3]
    known_retry_total = aggregate_attempt_costs([
        {"id": "first-generation", "attempt_no": 1, "dispatch_generation": 2,
         "usage_cost": extract_usage_cost({"billing": {"cost": 0.002, "currency": "USD"}}, MAPPING)},
        {"id": "retry-generation", "attempt_no": 1, "dispatch_generation": 3,
         "usage_cost": extract_usage_cost({"billing": {"cost": 0.003, "currency": "USD"}}, MAPPING)},
    ])
    assert known_retry_total["cost"]["amount"] == "0.005"


def test_aggregates_only_complete_case_costs_and_reports_coverage():
    summary = aggregate_run(
        [
            {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass",
             "cost": {"amount": "0.01", "currency": "USD", "complete": True,
                      "source": "provider_reported", "scope": "launch_case_total",
                      "policy_version": "case-cost-v1"}},
            {"execution_status": "failed", "eval_status": "skipped", "quality_conclusion": "unknown",
             "cost": {"amount": "0.03", "currency": "USD", "complete": True,
                      "source": "provider_reported", "scope": "launch_case_total",
                      "policy_version": "case-cost-v1"}},
            {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass",
             "cost": {"amount": None, "currency": None, "complete": False,
                      "unavailable_reason": "COST_NOT_RECORDED"}},
        ],
        evaluator_specs=[],
    )
    assert summary["total_cost"] == 0.04
    assert summary["cost_per_case"] == 0.02
    assert summary["cost_case_count"] == 2
    assert summary["cost_coverage"] == pytest.approx(2 / 3)
    assert summary["cost_partial"] is True


def test_cost_comparison_requires_complete_compatible_cohort():
    baseline = {"total_cost": 0.04, "cost_per_case": 0.02, "cost_coverage": 1.0,
                "cost_case_count": 2, "cost_currency": "USD", "cost_scope": "launch_case_total",
                "cost_policy_version": "case-cost-v1", "cost_source": "provider_reported"}
    candidate = {**baseline, "total_cost": 0.03, "cost_per_case": 0.015}
    result = compare_costs(baseline, candidate, cohort_case_count=2)
    assert result["status"] == "COMPARABLE"
    assert result["delta"] == pytest.approx(-0.005)
    assert compare_costs(baseline, {**candidate, "cost_coverage": 0.5}, cohort_case_count=2)["delta"] is None
    assert compare_costs(baseline, {**candidate, "cost_currency": "EUR"}, cohort_case_count=2)["reason"] == "COST_CURRENCY_MISMATCH"

    mixed_run = {**baseline, "cost_unavailable_reason": "MIXED_CURRENCIES", "cost_currency": None, "cost_per_case": None}
    assert compare_costs(baseline, mixed_run, cohort_case_count=2)["reason"] == "MIXED_CURRENCIES"
    invalid_run = {**baseline, "cost_case_count": 0, "cost_coverage": 0,
                   "cost_unavailable_reason": "INVALID_COST_EVIDENCE", "cost_currency": None,
                   "cost_per_case": None}
    assert compare_costs(baseline, invalid_run, cohort_case_count=2)["reason"] == "INVALID_COST_EVIDENCE"


def test_cost_aggregate_rejects_mixed_currencies():
    from app.aggregation import aggregate_run

    summary = aggregate_run(
        [
            {"cost": {"amount": "0.01", "currency": "USD", "complete": True,
                      "source": "provider_reported", "scope": "launch_case_total",
                      "policy_version": "case-cost-v1"}},
            {"cost": {"amount": "0.02", "currency": "EUR", "complete": True,
                      "source": "provider_reported", "scope": "launch_case_total",
                      "policy_version": "case-cost-v1"}},
        ], evaluator_specs=[],
    )
    assert summary["cost_coverage"] == 1.0
    assert summary["total_cost"] is None
    assert summary["cost_per_case"] is None
    assert summary["cost_unavailable_reason"] == "MIXED_CURRENCIES"


def test_usage_cost_mapping_requires_safe_dot_paths_and_explicit_scope():
    from app.models import UsageCostMapping
    from pydantic import ValidationError

    valid = UsageCostMapping(
        amount_path="billing.cost",
        currency_path="billing.currency",
        measurement_scope="agent_invocation_total",
    )
    assert valid.source == "provider_reported"
    with pytest.raises(ValidationError):
        UsageCostMapping(amount_path="billing[0].cost", currency_path="billing.currency",
                         measurement_scope="agent_invocation_total")
    with pytest.raises(ValidationError):
        UsageCostMapping(amount_path="billing.cost", measurement_scope="agent_invocation_total")


def test_absent_cost_is_unavailable_but_explicit_zero_is_valid():
    absent = aggregate_run([{"execution_status": "succeeded", "eval_status": "succeeded",
                             "quality_conclusion": "pass"}], evaluator_specs=[])
    assert absent["cost_per_case"] is None
    assert absent["cost_coverage"] == 0
    assert absent["cost_unavailable_reason"] == "COST_NOT_RECORDED"

    zero = aggregate_run([{"cost": {"amount": "0", "currency": "USD", "complete": True,
                             "source": "provider_reported", "scope": "launch_case_total",
                             "policy_version": "case-cost-v1"}}], evaluator_specs=[])
    assert zero["cost_per_case"] == 0
    assert zero["total_cost"] == 0
    assert zero["cost_coverage"] == 1
    assert zero["cost_unavailable_reason"] is None


def test_attempt_cost_evidence_only_has_unavailable_reason_when_cost_is_missing():
    def attempt_evidence(usage_cost=None):
        attempt = {"id": "attempt-1", "attempt_no": 1, "dispatch_generation": 0}
        if usage_cost is not None:
            attempt["usage_cost"] = usage_cost
        return aggregate_attempt_costs([attempt])["cost_evidence"]["attempts"][0]

    positive = attempt_evidence({"cost": {
        "amount": "0.01",
        "currency": "USD",
        "source": "provider_reported",
        "measurement_scope": "agent_invocation_total",
        "unavailable_reason": None,
    }})
    assert positive["amount"] == "0.01"
    assert positive["unavailable_reason"] is None

    zero = attempt_evidence({"cost": {
        "amount": "0",
        "currency": "USD",
        "source": "provider_reported",
        "measurement_scope": "agent_invocation_total",
        "unavailable_reason": None,
    }})
    assert zero["amount"] == "0"
    assert zero["unavailable_reason"] is None

    missing = attempt_evidence()
    assert missing["amount"] is None
    assert missing["unavailable_reason"] == "COST_NOT_RECORDED"


def test_cost_summary_response_fields_are_explicit_and_typed_in_schema():
    run_schema = RunSummaryResponse.model_json_schema()
    run_summary_ref = run_schema["properties"]["summary"]["$ref"]
    run_summary_name = run_summary_ref.rsplit("/", 1)[1]
    run_cost_fields = run_schema["$defs"][run_summary_name]["properties"]
    assert run_cost_fields["cost_per_case"]["anyOf"][0]["type"] == "number"
    assert run_cost_fields["cost_per_case"]["anyOf"][1]["type"] == "null"
    assert run_cost_fields["cost_coverage"]["anyOf"][0]["type"] == "number"
    run_reason_schema = run_cost_fields["cost_unavailable_reason"]
    assert any(option.get("type") == "null" for option in run_reason_schema["anyOf"])
    run_reason_ref = next(option["$ref"] for option in run_reason_schema["anyOf"] if "$ref" in option)
    run_reason_name = run_reason_ref.rsplit("/", 1)[1]
    assert set(run_schema["$defs"][run_reason_name]["enum"]) == {
        "COST_NOT_RECORDED", "INVALID_COST_EVIDENCE", "INCOMPLETE_ATTEMPT_COST",
        "MIXED_CURRENCIES", "COST_SOURCE_MISMATCH", "COST_SCOPE_MISMATCH",
        "COST_POLICY_MISMATCH", "PARTIAL_COST_COVERAGE",
    }

    comparison_schema = ComparisonResponse.model_json_schema()
    comparison_summary_ref = comparison_schema["properties"]["summary"]["$ref"]
    comparison_summary_name = comparison_summary_ref.rsplit("/", 1)[1]
    comparison_fields = comparison_schema["$defs"][comparison_summary_name]["properties"]
    cost_comparison_ref = comparison_fields["cost_comparison"]["$ref"]
    cost_comparison_name = cost_comparison_ref.rsplit("/", 1)[1]
    cost_comparison_fields = comparison_schema["$defs"][cost_comparison_name]["properties"]
    assert set(comparison_schema["$defs"][cost_comparison_name]["required"]) == set(cost_comparison_fields)
    status_ref = cost_comparison_fields["status"]["$ref"]
    status_name = status_ref.rsplit("/", 1)[1]
    assert set(comparison_schema["$defs"][status_name]["enum"]) == {"COMPARABLE", "NOT_COMPARABLE"}
    reason_schema = cost_comparison_fields["reason"]
    assert any(option.get("type") == "null" for option in reason_schema["anyOf"])
    reason_ref = next(option["$ref"] for option in reason_schema["anyOf"] if "$ref" in option)
    reason_name = reason_ref.rsplit("/", 1)[1]
    assert set(comparison_schema["$defs"][reason_name]["enum"]) == {
        "COST_NOT_RECORDED", "INVALID_COST_EVIDENCE", "INCOMPLETE_ATTEMPT_COST",
        "MIXED_CURRENCIES", "COST_SOURCE_MISMATCH", "COST_SCOPE_MISMATCH",
        "COST_POLICY_MISMATCH", "PARTIAL_COST_COVERAGE", "COST_CURRENCY_MISMATCH",
        "BASELINE_NOT_BOUND", "NO_COMPARABLE_CASES",
    }
    assert "cost_comparison" in comparison_schema["$defs"][comparison_summary_name]["required"]
