"""基于不可变 Snapshot 的发布策略与门禁，不读取活动分数或 Langfuse。"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_validator

from .aggregation import aggregate_run, compare_case_results
from .baselines import normalize_environment
from .comparison_contracts import aggregation_comparison_digest, assess_comparability
from .evaluator_binding import canonical_digest
from .runner_identity import RunnerIdentity

ENGINE_VERSION = "release-gate-v3"
ABSOLUTE_METRICS = {"pass_rate", "evaluation_coverage", "execution_error_rate", "critical_failure_count", "p95_latency_ms"}
RELATIVE_METRICS = {"regression_count", "pass_rate_delta", "p95_latency_regression_percent"}


class ReleaseRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    metric: str
    operator: Literal[">=", "<="]
    threshold: FiniteFloat = Field(strict=True)

    @field_validator("metric")
    @classmethod
    def valid_metric(cls, value: str) -> str:
        if value not in ABSOLUTE_METRICS | RELATIVE_METRICS:
            if not value.startswith("score_mean:") or not value.removeprefix("score_mean:").strip():
                raise ValueError("Unsupported release metric")
        return value


class ReleasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    agent_id: str = Field(min_length=1, max_length=128)
    environment: str = "production"
    block_critical_failures: bool = Field(default=True, strict=True)
    rules: list[ReleaseRule] = Field(min_length=1, max_length=50)

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str) -> str:
        if value.lower() in {"latest", "dev", "main", "head"}:
            raise ValueError("ReleasePolicy requires an explicit immutable version")
        return value

    @field_validator("environment")
    @classmethod
    def valid_environment(cls, value: str) -> str:
        return normalize_environment(value)

    @model_validator(mode="after")
    def unique_rules(self):
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Release rule IDs must be unique")
        return self

    @property
    def content_digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class GateRuleResult(BaseModel):
    id: str
    metric: str
    operator: Literal[">=", "<="]
    threshold: float
    actual: float | None
    conclusion: Literal["PASS", "FAIL", "UNKNOWN"]
    reason: str


class GateEvaluation(BaseModel):
    decision: Literal["PASS", "FAIL", "UNKNOWN"]
    releasable: bool
    engine_version: str = ENGINE_VERSION
    candidate_snapshot_id: str
    baseline_snapshot_id: str | None = None
    reason_codes: list[str]
    rules: list[GateRuleResult]


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def frozen_environment(snapshot) -> str | None:
    """缺失或未规范化的历史环境不能推定为 production。"""
    comparison = snapshot.manifest.get("comparison") or {}
    value = comparison.get("environment")
    if not isinstance(value, str):
        return None
    try:
        return value if normalize_environment(value) == value else None
    except ValueError:
        return None


def _summary(snapshot) -> dict[str, Any]:
    summary = aggregate_run(snapshot.items, snapshot.manifest.get("evaluators", []))
    if any((latency := _finite(item.get("latency_ms"))) is None or latency < 0 for item in snapshot.items):
        summary["p95_latency_ms"] = None
    # S3.1 的逐规则 critical 证据比旧数字阈值聚合更精确（也支持布尔与分类）。
    if all(isinstance(item.get("quality_evaluation"), dict) for item in snapshot.items):
        summary["critical_failure_count"] = sum(
            any(rule.get("critical") and rule.get("conclusion") == "fail"
                for rule in item["quality_evaluation"].get("rules", []))
            for item in snapshot.items
        )
    return summary


def _evidence_reasons(snapshot, label: str) -> list[str]:
    reasons = []
    if frozen_environment(snapshot) is None:
        reasons.append(f"{label}_ENVIRONMENT_UNKNOWN")
    if snapshot.evidence_state != "COMPLETE" or not snapshot.items:
        reasons.append(f"{label}_EVIDENCE_INCOMPLETE")
    expected = [str(item.get("id")) for item in snapshot.manifest.get("dataset", {}).get("items", [])]
    actual = [str(item.get("dataset_item_id")) for item in snapshot.items]
    if not expected or len(set(expected)) != len(expected) or sorted(expected) != sorted(actual):
        reasons.append(f"{label}_CASE_COVERAGE_INCOMPLETE")
    if _summary(snapshot).get("evaluation_coverage") != 1.0:
        reasons.append(f"{label}_EVIDENCE_INCOMPLETE")
    provenance = assess_comparability(snapshot.manifest, snapshot.manifest)
    if not provenance.comparable:
        reasons.extend(provenance.reason_codes)
    frozen_aggregation = snapshot.manifest.get("contract_digests", {}).get("aggregation_comparison", {}).get("digest")
    if frozen_aggregation and frozen_aggregation != aggregation_comparison_digest():
        reasons.append("AGGREGATION_CONTRACT_UNSUPPORTED")
    dataset = snapshot.manifest.get("dataset", {})
    def frozen_version(value):
        return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {"latest", "active"}

    if not dataset.get("source") or not dataset.get("dataset_id") or not frozen_version(dataset.get("dataset_version")):
        reasons.append("DATASET_IDENTITY_UNKNOWN")
    agent = snapshot.manifest.get("agent") or {}
    if agent.get("agent_id") != snapshot.agent_id or not frozen_version(agent.get("version")):
        reasons.append("AGENT_IDENTITY_UNKNOWN")
    evaluators = snapshot.manifest.get("evaluators") or []
    if not evaluators or any(not spec.get("id") or not frozen_version(spec.get("version")) for spec in evaluators):
        reasons.append("EVALUATOR_IDENTITY_UNKNOWN")
    runner = snapshot.manifest.get("runner") or {}
    identity = RunnerIdentity(runner.get("runner_version", ""), runner.get("build_id", ""), runner.get("mapping_engine_version", ""))
    if not identity.is_reliable or not frozen_version(identity.runner_version):
        reasons.append("RUNNER_IDENTITY_UNKNOWN")
    return list(dict.fromkeys(reasons))


def _comparison(candidate, baseline) -> tuple[dict[str, Any], list[str]]:
    frozen_id = candidate.manifest.get("comparison", {}).get("baseline_snapshot_id")
    if baseline is None or not frozen_id:
        return {}, ["BASELINE_NOT_BOUND"]
    if baseline.id != frozen_id or baseline.agent_id != candidate.agent_id:
        return {}, ["BASELINE_IDENTITY_MISMATCH"]
    baseline_environment, candidate_environment = frozen_environment(baseline), frozen_environment(candidate)
    if baseline_environment is not None and candidate_environment is not None and baseline_environment != candidate_environment:
        return {}, ["BASELINE_ENVIRONMENT_MISMATCH"]
    reasons = _evidence_reasons(baseline, "BASELINE")
    comparability = assess_comparability(baseline.manifest, candidate.manifest)
    reasons.extend(comparability.reason_codes)
    before = {item["dataset_item_id"]: item for item in baseline.items}
    after = {item["dataset_item_id"]: item for item in candidate.items}
    if before.keys() != after.keys():
        reasons.append("COMPARISON_COVERAGE_INCOMPLETE")
    differences = [compare_case_results(
        before.get(case_id), after.get(case_id),
        baseline_evaluators=baseline.manifest.get("evaluators", []),
        evaluator_specs=candidate.manifest.get("evaluators", []),
    ) for case_id in sorted(before.keys() | after.keys())]
    reasons.extend(diff.get("reason", "CASE_NOT_COMPARABLE") for diff in differences
                   if diff["classification"] == "NOT_COMPARABLE")
    if reasons:
        return {}, list(dict.fromkeys(reasons))
    base_summary, candidate_summary = _summary(baseline), _summary(candidate)
    base_latency = _finite(base_summary.get("p95_latency_ms"))
    new_latency = _finite(candidate_summary.get("p95_latency_ms"))
    return {
        "regression_count": sum(diff["classification"] == "REGRESSION" for diff in differences),
        "pass_rate_delta": candidate_summary["pass_rate"] - base_summary["pass_rate"],
        "p95_latency_regression_percent": ((new_latency / base_latency - 1) * 100)
        if base_latency is not None and base_latency > 0 and new_latency is not None else None,
    }, []


def evaluate_gate(policy: ReleasePolicy, candidate, baseline=None) -> GateEvaluation:
    reasons = _evidence_reasons(candidate, "CANDIDATE")
    environment = frozen_environment(candidate)
    if environment is not None and environment != policy.environment:
        reasons.append("CANDIDATE_ENVIRONMENT_MISMATCH")
    declared_critical = {
        rule["evaluator_id"] for rule in candidate.manifest.get("quality_policy", {}).get("rules", [])
        if rule.get("critical") and rule.get("evaluator_id")
    }
    missing_critical = any(
        not declared_critical.issubset({
            rule.get("evaluator_id") for rule in (item.get("quality_evaluation") or {}).get("rules", [])
            if rule.get("critical") and rule.get("conclusion") in {"pass", "fail"}
        }) for item in candidate.items
    )
    needs_critical_evidence = policy.block_critical_failures or any(
        rule.metric == "critical_failure_count" for rule in policy.rules
    )
    if needs_critical_evidence and (missing_critical or any(
        rule.get("critical") and rule.get("conclusion") not in {"pass", "fail"}
        for item in candidate.items
        for rule in (item.get("quality_evaluation") or {}).get("rules", [])
    )):
        reasons.append("CRITICAL_EVIDENCE_INCOMPLETE")
    summary = _summary(candidate)
    relative = any(rule.metric in RELATIVE_METRICS for rule in policy.rules)
    comparison, comparison_reasons = _comparison(candidate, baseline) if relative else ({}, [])
    rule_results = []
    for rule in policy.rules:
        blocked = reasons or (comparison_reasons if rule.metric in RELATIVE_METRICS else [])
        if rule.metric.startswith("score_mean:"):
            evaluator_id = rule.metric.removeprefix("score_mean:")
            raw = summary.get("score_means", {}).get(evaluator_id)
            if summary.get("score_counts", {}).get(evaluator_id) != len(candidate.items):
                raw = None
        else:
            raw = (comparison if rule.metric in RELATIVE_METRICS else summary).get(rule.metric)
        actual = None if blocked else _finite(raw)
        if actual is None:
            conclusion, reason = "UNKNOWN", (blocked[0] if blocked else "METRIC_UNAVAILABLE")
        else:
            satisfied = actual >= rule.threshold if rule.operator == ">=" else actual <= rule.threshold
            conclusion, reason = ("PASS", "THRESHOLD_SATISFIED") if satisfied else ("FAIL", "THRESHOLD_VIOLATED")
        rule_results.append(GateRuleResult(**rule.model_dump(), actual=actual, conclusion=conclusion, reason=reason))
    reasons.extend(comparison_reasons)
    critical_failure = policy.block_critical_failures and summary.get("critical_failure_count", 0) > 0
    if critical_failure:
        reasons.append("CRITICAL_FAILURE")
    if (reasons and any(code != "CRITICAL_FAILURE" for code in reasons)) or any(rule.conclusion == "UNKNOWN" for rule in rule_results):
        decision = "UNKNOWN"
    elif critical_failure or any(rule.conclusion == "FAIL" for rule in rule_results):
        decision = "FAIL"
    else:
        decision = "PASS"
    reasons.extend(rule.reason for rule in rule_results if rule.conclusion != "PASS")
    return GateEvaluation(
        decision=decision, releasable=decision == "PASS", candidate_snapshot_id=candidate.id,
        baseline_snapshot_id=candidate.manifest.get("comparison", {}).get("baseline_snapshot_id"),
        reason_codes=list(dict.fromkeys(reasons)), rules=rule_results,
    )
