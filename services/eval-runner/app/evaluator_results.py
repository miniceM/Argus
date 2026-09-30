"""Typed Evaluator results (Argus Issue #82).

The legacy execution path coerced every measurement to ``float`` and defaulted a
missing value to ``0.0``, which made "really zero", "no result" and "the
Evaluator crashed" indistinguishable. This module makes the typed result the
source of truth:

* :class:`TypedEvaluationResult` records what the frozen Binding actually
  produced — boolean / numeric / categorical / text — plus an explainable
  status, provenance and structured error.
* ``normalized_value`` is produced **only** by an explicit rule frozen in the
  Binding contract. Text and unordered categories stay ``None`` and therefore
  never enter a mean or a numeric delta.
* :func:`project_legacy_scores` is the restricted backwards-compatible view:
  it emits numbers for numeric results only and never coerces booleans,
  categories, text, failures or missing values into ``0``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .evaluator_binding import EvaluatorBinding

RESULT_STATUS_SUCCEEDED = "succeeded"
RESULT_STATUS_FAILED = "failed"
RESULT_STATUS_SKIPPED = "skipped"
RESULT_STATUS_NO_RESULT = "no_result"

RESULT_STATUSES = (
    RESULT_STATUS_SUCCEEDED,
    RESULT_STATUS_FAILED,
    RESULT_STATUS_SKIPPED,
    RESULT_STATUS_NO_RESULT,
)

SUPPORTED_RESULT_TYPES = ("boolean", "numeric", "categorical", "text")

# Structured, user-readable failure reasons (Issue #82).
RESULT_ERROR_CODES: dict[str, str] = {
    "EVALUATION_VALUE_MISSING": "评测没有返回任何结果值。",
    "EVALUATION_VALUE_NOT_FINITE": "评测返回了非有限数值（NaN/Infinity），不能作为测量。",
    "EVALUATION_TYPE_MISMATCH": "评测返回的类型与冻结契约声明的类型不一致。",
    "EVALUATION_CATEGORY_NOT_ALLOWED": "评测返回的分类取值不在冻结契约的枚举范围内。",
    "EVALUATION_BOOLEAN_COERCED": "布尔结果不会被隐式当作数字使用。",
    "EVALUATION_FAILED": "评测执行失败。",
    "EVALUATION_SKIPPED": "该指标本次未评测。",
    "EVALUATION_PROVENANCE_UNKNOWN": "历史记录缺少新的冻结证据来源，仅按旧 numeric 兼容读取。",
}


class ResultContractError(ValueError):
    """Raised when a raw Evaluator output violates its frozen typed contract."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or RESULT_ERROR_CODES.get(code, code))
        self.code = code
        self.message = message or RESULT_ERROR_CODES.get(code, code)


def coerce_typed_value(
    raw_value: Any,
    *,
    result_type: str,
    category_values: tuple[str, ...] | None = None,
) -> Any:
    """Validate a raw Evaluator value against its frozen type contract.

    Raises :class:`ResultContractError` instead of inventing a number, so a
    missing, NaN or mistyped value can never be displayed as ``0``.
    """
    if result_type == "numeric":
        if raw_value is None:
            raise ResultContractError("EVALUATION_VALUE_MISSING")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise ResultContractError("EVALUATION_TYPE_MISMATCH")
        number = float(raw_value)
        if not math.isfinite(number):
            raise ResultContractError("EVALUATION_VALUE_NOT_FINITE")
        return number

    if result_type == "boolean":
        if raw_value is None:
            raise ResultContractError("EVALUATION_VALUE_MISSING")
        if not isinstance(raw_value, bool):
            raise ResultContractError("EVALUATION_TYPE_MISMATCH")
        return raw_value

    if result_type == "categorical":
        if raw_value is None:
            raise ResultContractError("EVALUATION_VALUE_MISSING")
        if not isinstance(raw_value, str):
            raise ResultContractError("EVALUATION_TYPE_MISMATCH")
        if category_values and raw_value not in category_values:
            raise ResultContractError("EVALUATION_CATEGORY_NOT_ALLOWED")
        return raw_value

    if result_type == "text":
        if raw_value is None:
            raise ResultContractError("EVALUATION_VALUE_MISSING")
        if not isinstance(raw_value, str):
            raise ResultContractError("EVALUATION_TYPE_MISMATCH")
        return raw_value

    raise ResultContractError("EVALUATION_TYPE_MISMATCH")


def normalized_value_for(
    value: Any,
    *,
    result_type: str,
    normalization_rule: dict[str, Any] | None,
) -> float | None:
    """Apply an explicitly frozen normalization rule; otherwise stay ``None``.

    Only an ordered mapping that the Binding froze may produce a number. Text
    and unordered categories are never numericized.
    """
    if normalization_rule is None:
        # Numeric results are already their own normalized form.
        return float(value) if result_type == "numeric" and isinstance(value, (int, float)) else None
    kind = normalization_rule.get("kind")
    if kind == "ordered_category":
        mapping = normalization_rule.get("mapping") or {}
        if not isinstance(mapping, dict):
            raise ResultContractError("EVALUATION_TYPE_MISMATCH")
        mapped = mapping.get(value) if isinstance(value, str) else None
        if mapped is None:
            return None
        number = coerce_typed_value(mapped, result_type="numeric")
        return float(number)
    if kind == "boolean":
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        raise ResultContractError("EVALUATION_TYPE_MISMATCH")
    raise ResultContractError("EVALUATION_TYPE_MISMATCH")


@dataclass(frozen=True)
class ResultProvenance:
    """Where a result came from, so forged or stale evidence is detectable."""

    binding_id: str | None
    evaluator_id: str
    evaluator_version: str | None
    definition_digest: str | None
    executor_type: str | None
    manifest_schema_version: str | None
    contract_status: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "definition_digest": self.definition_digest,
            "executor_type": self.executor_type,
            "manifest_schema_version": self.manifest_schema_version,
            "contract_status": self.contract_status,
        }

    @classmethod
    def from_binding(
        cls, binding: EvaluatorBinding, *, manifest_schema_version: str | None = None
    ) -> ResultProvenance:
        return cls(
            binding_id=binding.binding_id,
            evaluator_id=binding.evaluator_id,
            evaluator_version=binding.version,
            definition_digest=binding.definition_digest or None,
            executor_type=binding.executor_type,
            manifest_schema_version=manifest_schema_version,
            contract_status=binding.contract_status,
        )


@dataclass(frozen=True)
class TypedEvaluationResult:
    """One explainable measurement produced by one frozen Binding."""

    evaluator_id: str
    result_type: str
    status: str
    provenance: ResultProvenance
    value: Any = None
    normalized_value: float | None = None
    comment: str | None = None
    evidence: dict[str, Any] | None = None
    duration_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    normalization_rule: dict[str, Any] | None = None

    @property
    def is_measurable(self) -> bool:
        return self.status == RESULT_STATUS_SUCCEEDED and self.value is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "evaluator_id": self.evaluator_id,
            "result_type": self.result_type,
            "status": self.status,
            "value": self.value,
            "normalized_value": self.normalized_value,
            "comment": self.comment,
            "evidence": self.evidence,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "provenance": self.provenance.payload(),
        }


def build_result(
    binding: EvaluatorBinding,
    raw_value: Any,
    *,
    comment: str | None = None,
    evidence: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    manifest_schema_version: str | None = None,
    raw_value_present: bool = True,
) -> TypedEvaluationResult:
    """Turn one raw Evaluator output into an explainable typed result.

    A value-less "successful" output is invalid: it becomes ``no_result`` with a
    structured reason instead of a fabricated zero.
    """
    provenance = ResultProvenance.from_binding(
        binding, manifest_schema_version=manifest_schema_version
    )
    if not raw_value_present:
        return TypedEvaluationResult(
            evaluator_id=binding.evaluator_id,
            result_type=binding.result_type,
            status=RESULT_STATUS_NO_RESULT,
            provenance=provenance,
            comment=comment,
            evidence=evidence,
            duration_ms=duration_ms,
            error_code="EVALUATION_VALUE_MISSING",
            error_message=RESULT_ERROR_CODES["EVALUATION_VALUE_MISSING"],
        )

    try:
        typed_value = coerce_typed_value(
            raw_value,
            result_type=binding.result_type,
            category_values=binding.category_values,
        )
        normalized = normalized_value_for(
            typed_value,
            result_type=binding.result_type,
            normalization_rule=binding.normalization_rule,
        )
    except ResultContractError as exc:
        return TypedEvaluationResult(
            evaluator_id=binding.evaluator_id,
            result_type=binding.result_type,
            status=RESULT_STATUS_FAILED,
            provenance=provenance,
            comment=comment,
            evidence=evidence,
            duration_ms=duration_ms,
            error_code=exc.code,
            error_message=exc.message,
        )

    return TypedEvaluationResult(
        evaluator_id=binding.evaluator_id,
        result_type=binding.result_type,
        status=RESULT_STATUS_SUCCEEDED,
        provenance=provenance,
        value=typed_value,
        normalized_value=normalized,
        comment=comment,
        evidence=evidence,
        duration_ms=duration_ms,
    )


def failed_result(
    binding: EvaluatorBinding,
    *,
    error_code: str = "EVALUATION_FAILED",
    error_message: str | None = None,
    comment: str | None = None,
    evidence: dict[str, Any] | None = None,
    duration_ms: float | None = None,
    manifest_schema_version: str | None = None,
) -> TypedEvaluationResult:
    """Record an execution failure without discarding the other results."""
    return TypedEvaluationResult(
        evaluator_id=binding.evaluator_id,
        result_type=binding.result_type,
        status=RESULT_STATUS_FAILED,
        provenance=ResultProvenance.from_binding(
            binding, manifest_schema_version=manifest_schema_version
        ),
        comment=comment,
        evidence=evidence,
        duration_ms=duration_ms,
        error_code=error_code,
        error_message=error_message or RESULT_ERROR_CODES.get(error_code, error_code),
    )


def skipped_result(
    binding: EvaluatorBinding,
    *,
    reason: str | None = None,
    manifest_schema_version: str | None = None,
) -> TypedEvaluationResult:
    return TypedEvaluationResult(
        evaluator_id=binding.evaluator_id,
        result_type=binding.result_type,
        status=RESULT_STATUS_SKIPPED,
        provenance=ResultProvenance.from_binding(
            binding, manifest_schema_version=manifest_schema_version
        ),
        error_code="EVALUATION_SKIPPED",
        error_message=reason or RESULT_ERROR_CODES["EVALUATION_SKIPPED"],
    )


def project_legacy_scores(results: list[TypedEvaluationResult]) -> dict[str, float]:
    """Restricted compatibility projection: successful **numeric** results only.

    Booleans, categories, text, failures and missing values are intentionally
    absent — the legacy `scores` map must never acquire a fabricated ``0``.
    """
    projected: dict[str, float] = {}
    for result in results:
        if result.status != RESULT_STATUS_SUCCEEDED or result.result_type != "numeric":
            continue
        if result.value is None or not math.isfinite(float(result.value)):
            continue
        projected[result.evaluator_id] = float(result.value)
    return projected


def legacy_numeric_results(
    scores: dict[str, Any] | None,
    *,
    manifest_schema_version: str | None = None,
) -> list[TypedEvaluationResult]:
    """Explicit adapter for historical `scores` maps (Issue #82).

    Historical rows carry no typed evidence, so the adapter marks the provenance
    as unknown instead of inventing a typed record that looks authoritative.
    """
    provenance = ResultProvenance(
        binding_id=None,
        evaluator_id="",
        evaluator_version=None,
        definition_digest=None,
        executor_type=None,
        manifest_schema_version=manifest_schema_version,
        contract_status="LEGACY_SCORES_ADAPTER",
    )
    results: list[TypedEvaluationResult] = []
    for evaluator_id, raw in (scores or {}).items():
        result_provenance = ResultProvenance(
            binding_id=provenance.binding_id,
            evaluator_id=str(evaluator_id),
            evaluator_version=provenance.evaluator_version,
            definition_digest=provenance.definition_digest,
            executor_type=provenance.executor_type,
            manifest_schema_version=manifest_schema_version,
            contract_status=provenance.contract_status,
        )
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            # Historical non-numeric entries are never coerced into a score.
            results.append(
                TypedEvaluationResult(
                    evaluator_id=str(evaluator_id),
                    result_type="unknown",
                    status=RESULT_STATUS_NO_RESULT,
                    provenance=result_provenance,
                    error_code="EVALUATION_PROVENANCE_UNKNOWN",
                    error_message=RESULT_ERROR_CODES["EVALUATION_PROVENANCE_UNKNOWN"],
                )
            )
            continue
        number = float(raw)
        if not math.isfinite(number):
            results.append(
                TypedEvaluationResult(
                    evaluator_id=str(evaluator_id),
                    result_type="numeric",
                    status=RESULT_STATUS_NO_RESULT,
                    provenance=result_provenance,
                    error_code="EVALUATION_VALUE_NOT_FINITE",
                    error_message=RESULT_ERROR_CODES["EVALUATION_VALUE_NOT_FINITE"],
                )
            )
            continue
        results.append(
            TypedEvaluationResult(
                evaluator_id=str(evaluator_id),
                result_type="numeric",
                status=RESULT_STATUS_SUCCEEDED,
                provenance=result_provenance,
                value=number,
                normalized_value=number,
                comment="历史 numeric 结果（兼容投影，缺少冻结证据）",
                error_code="EVALUATION_PROVENANCE_UNKNOWN",
                error_message=RESULT_ERROR_CODES["EVALUATION_PROVENANCE_UNKNOWN"],
            )
        )
    return results


def aggregate_numeric(
    results: list[TypedEvaluationResult],
) -> tuple[dict[str, float | None], dict[str, int]]:
    """Mean and valid-sample count per numeric evaluator.

    Text, categories, booleans and every non-successful status are excluded by
    construction: they never contribute a number to the mean.
    """
    values: dict[str, list[float]] = {}
    for result in results:
        if result.status != RESULT_STATUS_SUCCEEDED or result.result_type != "numeric":
            continue
        if result.value is None:
            continue
        number = float(result.value)
        if not math.isfinite(number):
            continue
        values.setdefault(result.evaluator_id, []).append(number)
    means = {key: (sum(vals) / len(vals) if vals else None) for key, vals in values.items()}
    counts = {key: len(vals) for key, vals in values.items()}
    return means, counts
