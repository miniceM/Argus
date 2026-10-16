"""Issue #198 S6.3：run 级 token 聚合（分母=total_cases，失败/重试全计入，fail-closed）。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.aggregation import aggregate_run  # noqa: E402
from app.costs import aggregate_attempt_costs, extract_usage_cost  # noqa: E402
from app.models import RunCostSummaryResponse  # noqa: E402

TOKEN_MAPPING = {
    "source": "provider_reported",
    "input_tokens_path": "usage.prompt_tokens",
    "output_tokens_path": "usage.completion_tokens",
    "total_tokens_path": "usage.total_tokens",
}


def _attempt(attempt_no: int, usage_cost: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"attempt-{attempt_no}",
        attempt_no=attempt_no,
        dispatch_generation=1,
        usage_cost=usage_cost,
    )


def _case_tokens(attempt_no: int, prompt: int, completion: int, total: int) -> dict:
    return extract_usage_cost(
        {"usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}},
        TOKEN_MAPPING,
    )


def _item(dataset_item_id: str, usage: dict | None, **extra) -> dict:
    base = {
        "dataset_item_id": dataset_item_id,
        "id": dataset_item_id,
        "execution_status": "succeeded",
        "eval_status": "succeeded",
        "quality_conclusion": "pass",
        "scores": {"overall_pass": 1.0},
        "evaluation_results": [
            {"evaluator_id": "overall_pass", "status": "succeeded", "result_type": "numeric", "value": 1.0}
        ],
        "latency_ms": 10,
    }
    base.update({"usage": usage, "cost": {"amount": None, "complete": False, "unavailable_reason": "INCOMPLETE_ATTEMPT_COST"}})
    base.update(extra)
    return base


def test_retry_attempt_tokens_are_summed_per_case():
    attempts = [
        _attempt(1, _case_tokens(1, 35, 15, 50)),
        _attempt(2, _case_tokens(2, 10, 5, 15)),
    ]
    aggregated = aggregate_attempt_costs(attempts)
    assert aggregated["usage"] == {"input_tokens": 45, "output_tokens": 20, "total_tokens": 65}


def test_usage_per_case_denominator_is_total_cases_incl_failures_and_retries():
    usable_a = {"input_tokens": 35, "output_tokens": 15, "total_tokens": 50}
    usable_c = {"input_tokens": 145, "output_tokens": 80, "total_tokens": 225}
    failed_b = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    items = [
        _item("case-a", usable_a),
        _item("case-b", failed_b, execution_status="failed", eval_status="failed", quality_conclusion="unknown"),
        _item("case-c", usable_c),
    ]
    summary = aggregate_run(items, [])
    assert summary["total_cases"] == 3
    assert summary["usage_tokens_per_case"] == (50 + 225) / 3
    assert summary["usage_input_tokens_per_case"] == (35 + 145) / 3
    assert summary["usage_output_tokens_per_case"] == (15 + 80) / 3
    assert summary["usage_case_count"] == 2
    assert summary["usage_coverage"] == 2 / 3
    assert summary["usage_partial"] is True
    assert summary["usage_unavailable_reason"] == "COST_NOT_RECORDED"


def test_partial_attempt_usage_reports_incomplete_reason():
    partial = {"input_tokens": 10, "output_tokens": None, "total_tokens": None}
    usable = {"input_tokens": 35, "output_tokens": 15, "total_tokens": 50}
    items = [_item("case-a", usable), _item("case-b", partial)]
    summary = aggregate_run(items, [])
    assert summary["usage_case_count"] == 1
    assert summary["usage_unavailable_reason"] == "INCOMPLETE_ATTEMPT_COST"
    assert summary["usage_partial"] is True


def test_missing_all_usage_never_fabricates_zero():
    items = [
        _item("case-a", {"input_tokens": None, "output_tokens": None, "total_tokens": None}),
        _item("case-b", {"input_tokens": None, "output_tokens": None, "total_tokens": None}),
    ]
    summary = aggregate_run(items, [])
    assert summary["usage_tokens_per_case"] is None
    assert summary["usage_input_tokens_per_case"] is None
    assert summary["usage_output_tokens_per_case"] is None
    assert summary["usage_case_count"] == 0
    assert summary["usage_coverage"] == 0.0
    assert summary["usage_partial"] is True
    assert summary["usage_unavailable_reason"] == "COST_NOT_RECORDED"


def test_full_usage_evidence_has_no_reason():
    usable = {"input_tokens": 35, "output_tokens": 15, "total_tokens": 50}
    items = [_item("case-a", usable), _item("case-b", usable)]
    summary = aggregate_run(items, [])
    assert summary["usage_tokens_per_case"] == 50.0
    assert summary["usage_case_count"] == 2
    assert summary["usage_coverage"] == 1.0
    assert summary["usage_partial"] is False
    assert summary["usage_unavailable_reason"] is None


def test_run_cost_summary_response_carries_usage_fields():
    summary = aggregate_run(
        [_item("case-a", {"input_tokens": 35, "output_tokens": 15, "total_tokens": 50})], []
    )
    summary["cost_per_case"] = None
    summary["cost_partial"] = True
    response = RunCostSummaryResponse.model_validate(summary)
    assert response.usage_tokens_per_case == 50.0
    assert response.usage_case_count == 1
    assert response.usage_unavailable_reason is None
