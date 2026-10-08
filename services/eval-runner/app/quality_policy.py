"""Frozen QualityPolicy and its PASS / FAIL / UNKNOWN truth table (Issue #83).

Measurement and judgement are two different things. A frozen
:class:`~app.evaluator_binding.EvaluatorBinding` says *what was measured*; this
module owns *how the measurement is judged*:

* :class:`QualityRule` — one immutable rule that references a Binding and
  declares ``required``, the comparison ``operator``, the expected value,
  ``critical`` and how insufficient evidence is handled. Quality thresholds
  live here, never inside a measurement definition.
* :class:`QualityPolicy` — the frozen, digested set of rules. Its
  ``policy_digest`` changes whenever a threshold / operator / critical flag
  changes, while the *measurement* digest of the same Manifest does not.
* :func:`evaluate_quality_policy` — the single decision function.

The truth table is deliberately fail-closed and unknown-safe:

===================================  ==========================================
necessary evidence                   conclusion
===================================  ==========================================
all present, all rules satisfied     ``pass``
all present, at least one violated   ``fail``
any required rule missing / failed   ``unknown`` (never silently ``fail``)
===================================  ==========================================

Insufficient evidence always wins over a known violation, because a quality
FAIL asserts something about the Agent that the evidence cannot support. A known
violation is still reported per rule, so nothing is hidden.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .evaluator_binding import canonical_digest

QUALITY_POLICY_SCHEMA_VERSION = "1.0"

QUALITY_CONCLUSION_PASS = "pass"
QUALITY_CONCLUSION_FAIL = "fail"
QUALITY_CONCLUSION_UNKNOWN = "unknown"

QUALITY_CONCLUSIONS = (QUALITY_CONCLUSION_PASS, QUALITY_CONCLUSION_FAIL, QUALITY_CONCLUSION_UNKNOWN)

#: How the first release treats evidence it cannot judge.
UNKNOWN_HANDLING_BLOCKS_RELEASE = "unknown_not_releasable"

#: Operators accepted in the first release, and the result types they accept.
NUMERIC_OPERATORS = (">=", "<=")
BOOLEAN_OPERATORS = ("==",)
CATEGORICAL_OPERATORS = ("==",)
SUPPORTED_OPERATORS_BY_RESULT_TYPE: dict[str, tuple[str, ...]] = {
    "numeric": NUMERIC_OPERATORS,
    "boolean": BOOLEAN_OPERATORS,
    "categorical": CATEGORICAL_OPERATORS,
    # text is evidence only: it never becomes a pass/fail rule.
    "text": (),
}

RECOVERY_HINTS: dict[str, str] = {
    "QUALITY_POLICY_OPERATOR_UNKNOWN": (
        "该比较运算符不受支持。数值仅支持 >= 与 <=，布尔与分类仅支持 ==；请修正规则后重新提交。"
    ),
    "QUALITY_POLICY_OPERATOR_TYPE_MISMATCH": (
        "比较运算符与指标结果类型不匹配。数值用 >= / <=，布尔与分类用 ==，文本只能作为证据。"
    ),
    "QUALITY_POLICY_TYPE_UNSUPPORTED": (
        "文本结果只能作为证据，不能作为质量判定规则。请将该指标改为非必要，或改用可判定的指标。"
    ),
    "QUALITY_POLICY_EVALUATOR_UNKNOWN": (
        "规则引用了本次任务未选择的评测指标。请只对已选择的指标配置判定规则。"
    ),
    "QUALITY_POLICY_CATEGORY_NOT_ALLOWED": (
        "期望取值不在该指标冻结的分类枚举范围内。请选择枚举内的取值。"
    ),
    "QUALITY_POLICY_THRESHOLD_INVALID": (
        "阈值必须是有限数值，不能是 NaN 或 Infinity。"
    ),
    "QUALITY_POLICY_EXPECTED_VALUE_MISSING": (
        "布尔与分类规则必须显式给出期望取值，不能依赖隐式比较。"
    ),
    "QUALITY_POLICY_THRESHOLD_MISSING": (
        "数值规则必须显式给出阈值。"
    ),
    "QUALITY_POLICY_RULE_DUPLICATE": (
        "同一个指标只能配置一条判定规则。请合并为一条规则后再提交。"
    ),
    "QUALITY_POLICY_NO_REQUIRED_RULE": (
        "质量策略至少需要一条必要规则，否则无法产出可放行的质量结论。"
    ),
    "QUALITY_POLICY_EMPTY": "质量策略不能为空，请至少配置一条判定规则。",
}


class QualityPolicyError(ValueError):
    """A quality policy that must be rejected before a Launch is created."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        evaluator_id: str | None = None,
        expected: Any = None,
        actual: Any = None,
        recovery: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.evaluator_id = evaluator_id
        self.expected = expected
        self.actual = actual
        self.recovery = recovery or RECOVERY_HINTS.get(code)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "evaluator_id": self.evaluator_id,
            "expected": self.expected,
            "actual": self.actual,
            "recovery": self.recovery,
        }


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityRule:
    """One immutable judgement rule over one frozen measurement."""

    evaluator_id: str
    operator: str | None = None
    threshold: float | None = None
    expected_value: Any = None
    result_type: str = "numeric"
    required: bool = True
    critical: bool = False
    unknown_handling: str = UNKNOWN_HANDLING_BLOCKS_RELEASE
    note: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "evaluator_id": self.evaluator_id,
            "operator": self.operator,
            "threshold": self.threshold,
            "expected_value": self.expected_value,
            "result_type": self.result_type,
            "required": bool(self.required),
            "critical": bool(self.critical),
            "unknown_handling": self.unknown_handling,
            "note": self.note,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> QualityRule:
        return cls(
            evaluator_id=str(payload.get("evaluator_id") or ""),
            operator=payload.get("operator"),
            threshold=payload.get("threshold"),
            expected_value=payload.get("expected_value"),
            result_type=str(payload.get("result_type") or "numeric"),
            required=bool(payload.get("required", True)),
            critical=bool(payload.get("critical", False)),
            unknown_handling=str(
                payload.get("unknown_handling") or UNKNOWN_HANDLING_BLOCKS_RELEASE
            ),
            note=payload.get("note"),
        )

    def describe(self) -> str:
        """Human-readable rule text, e.g. ``intent_match ≥ 0.8（必要）``."""
        if self.operator in NUMERIC_OPERATORS and self.threshold is not None:
            expression = f"{self.operator} {self.threshold}"
        elif self.operator == "==" and self.expected_value is not None:
            expression = f"== {_render_value(self.expected_value)}"
        else:
            expression = "仅作为证据"
        suffix = "必要" if self.required else "可选诊断"
        if self.critical:
            suffix += "·关键"
        return f"{self.evaluator_id} {expression}（{suffix}）"


def _field(source: Any, name: str) -> Any:
    """Read one field from either a Mapping payload or a dataclass result.

    The policy is evaluated both against live :class:`TypedEvaluationResult`
    objects during execution and against plain dicts read back from the API or
    a Snapshot, so both shapes must be understood.
    """
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "—"
    return str(value)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityPolicy:
    """The frozen, digested judgement policy of one Launch."""

    policy_id: str
    version: str
    schema_version: str
    rules: tuple[QualityRule, ...]
    unknown_handling: str = UNKNOWN_HANDLING_BLOCKS_RELEASE
    description: str | None = None
    rules_digest: str = field(default="", compare=False)

    def to_payload(self) -> dict[str, Any]:
        """Alias of :meth:`payload`, mirroring ``EvaluatorBinding.to_payload``."""
        return self.payload()

    def payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "rules": [rule.payload() for rule in self.rules],
            "unknown_handling": self.unknown_handling,
            "description": self.description,
            "policy_digest": self.policy_digest,
        }

    @property
    def policy_digest(self) -> str:
        return canonical_digest(
            {
                "policy_id": self.policy_id,
                "version": self.version,
                "schema_version": self.schema_version,
                "rules": [rule.payload() for rule in self.rules],
                "unknown_handling": self.unknown_handling,
            }
        )

    def rule_for(self, evaluator_id: str) -> QualityRule | None:
        for rule in self.rules:
            if rule.evaluator_id == evaluator_id:
                return rule
        return None

    @property
    def required_rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.evaluator_id for rule in self.rules if rule.required)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> QualityPolicy:
        return cls(
            policy_id=str(payload.get("policy_id") or ""),
            version=str(payload.get("version") or ""),
            schema_version=str(payload.get("schema_version") or QUALITY_POLICY_SCHEMA_VERSION),
            rules=tuple(
                QualityRule.from_payload(rule) for rule in (payload.get("rules") or [])
            ),
            unknown_handling=str(
                payload.get("unknown_handling") or UNKNOWN_HANDLING_BLOCKS_RELEASE
            ),
            description=payload.get("description"),
        )


# ---------------------------------------------------------------------------
# Freezing + validation
# ---------------------------------------------------------------------------

def _binding_result_type(binding: Any) -> str:
    return str(getattr(binding, "result_type", "numeric") or "numeric")


def _binding_category_values(binding: Any) -> tuple[str, ...] | None:
    values = getattr(binding, "category_values", None)
    return tuple(str(value) for value in values) if values else None


def _validate_rule(rule: QualityRule, binding: Any) -> None:
    result_type = rule.result_type or _binding_result_type(binding)
    allowed = SUPPORTED_OPERATORS_BY_RESULT_TYPE.get(result_type)

    if allowed is None:
        raise QualityPolicyError(
            f"指标 '{rule.evaluator_id}' 的结果类型 '{result_type}' 不受支持。",
            code="QUALITY_POLICY_TYPE_UNSUPPORTED",
            evaluator_id=rule.evaluator_id,
            expected=sorted(SUPPORTED_OPERATORS_BY_RESULT_TYPE),
            actual=result_type,
        )

    if not allowed:
        # text: evidence only. It may be listed, but it can never decide quality.
        raise QualityPolicyError(
            f"指标 '{rule.evaluator_id}' 返回文本，文本只能作为证据，不能作为质量判定规则。",
            code="QUALITY_POLICY_TYPE_UNSUPPORTED",
            evaluator_id=rule.evaluator_id,
            expected="evidence_only",
            actual=result_type,
        )

    if rule.operator not in allowed:
        if rule.operator in {op for ops in SUPPORTED_OPERATORS_BY_RESULT_TYPE.values() for op in ops}:
            raise QualityPolicyError(
                f"比较运算符 '{rule.operator}' 与 {result_type} 类型不匹配。",
                code="QUALITY_POLICY_OPERATOR_TYPE_MISMATCH",
                evaluator_id=rule.evaluator_id,
                expected=list(allowed),
                actual=rule.operator,
            )
        raise QualityPolicyError(
            f"比较运算符 '{rule.operator}' 不受支持。",
            code="QUALITY_POLICY_OPERATOR_UNKNOWN",
            evaluator_id=rule.evaluator_id,
            expected=list(allowed),
            actual=rule.operator,
        )

    if result_type == "numeric":
        if rule.threshold is None:
            raise QualityPolicyError(
                f"指标 '{rule.evaluator_id}' 的数值规则必须显式给出阈值。",
                code="QUALITY_POLICY_THRESHOLD_MISSING",
                evaluator_id=rule.evaluator_id,
            )
        if isinstance(rule.threshold, bool) or not isinstance(rule.threshold, (int, float)) or not math.isfinite(float(rule.threshold)):
            raise QualityPolicyError(
                f"指标 '{rule.evaluator_id}' 的阈值必须是有限数值。",
                code="QUALITY_POLICY_THRESHOLD_INVALID",
                evaluator_id=rule.evaluator_id,
                actual=rule.threshold,
            )
        return

    # boolean / categorical: an explicit typed match, never a coercion.
    if rule.expected_value is None:
        raise QualityPolicyError(
            f"指标 '{rule.evaluator_id}' 的规则必须显式给出期望取值。",
            code="QUALITY_POLICY_EXPECTED_VALUE_MISSING",
            evaluator_id=rule.evaluator_id,
        )
    if result_type == "boolean" and not isinstance(rule.expected_value, bool):
        raise QualityPolicyError(
            f"指标 '{rule.evaluator_id}' 是布尔类型，期望取值必须是 true 或 false。",
            code="QUALITY_POLICY_OPERATOR_TYPE_MISMATCH",
            evaluator_id=rule.evaluator_id,
            expected="boolean",
            actual=rule.expected_value,
        )
    if result_type == "categorical":
        category_values = _binding_category_values(binding)
        if category_values and str(rule.expected_value) not in category_values:
            raise QualityPolicyError(
                f"期望取值 '{rule.expected_value}' 不在指标 '{rule.evaluator_id}' 的分类枚举内。",
                code="QUALITY_POLICY_CATEGORY_NOT_ALLOWED",
                evaluator_id=rule.evaluator_id,
                expected=list(category_values),
                actual=rule.expected_value,
            )


def freeze_quality_policy(
    *,
    policy_id: str,
    version: str,
    rules: Sequence[QualityRule],
    bindings: Mapping[str, Any],
    description: str | None = None,
    unknown_handling: str = UNKNOWN_HANDLING_BLOCKS_RELEASE,
) -> QualityPolicy:
    """Validate and freeze a policy against the Manifest's frozen Bindings.

    Every illegal rule is rejected here — at creation time — so a Launch can
    never carry a policy that would be undecidable at run time.
    """
    ordered = tuple(rules)
    if not ordered:
        raise QualityPolicyError(
            "质量策略不能为空。", code="QUALITY_POLICY_EMPTY"
        )

    seen: set[str] = set()
    for rule in ordered:
        if rule.evaluator_id in seen:
            raise QualityPolicyError(
                f"指标 '{rule.evaluator_id}' 配置了多条判定规则。",
                code="QUALITY_POLICY_RULE_DUPLICATE",
                evaluator_id=rule.evaluator_id,
            )
        seen.add(rule.evaluator_id)
        binding = bindings.get(rule.evaluator_id)
        if binding is None:
            raise QualityPolicyError(
                f"规则引用了本次任务未选择的评测指标 '{rule.evaluator_id}'。",
                code="QUALITY_POLICY_EVALUATOR_UNKNOWN",
                evaluator_id=rule.evaluator_id,
                expected=sorted(bindings),
                actual=rule.evaluator_id,
            )
        _validate_rule(rule, binding)

    if not any(rule.required for rule in ordered):
        raise QualityPolicyError(
            "质量策略至少需要一条必要规则。",
            code="QUALITY_POLICY_NO_REQUIRED_RULE",
        )

    return QualityPolicy(
        policy_id=policy_id,
        version=version,
        schema_version=QUALITY_POLICY_SCHEMA_VERSION,
        rules=ordered,
        unknown_handling=unknown_handling,
        description=description,
    )


def default_quality_policy(bindings: Sequence[Any]) -> QualityPolicy:
    """The default all-required policy over the four diagnostic metrics.

    A numeric binding keeps its own frozen direction: higher-is-better uses
    ``>=``, lower-is-better uses ``<=``. A non-numeric binding becomes an
    optional diagnostic so a new Launch never depends on a composite metric.
    """
    rules: list[QualityRule] = []
    for binding in bindings:
        result_type = _binding_result_type(binding)
        evaluator_id = str(getattr(binding, "evaluator_id", ""))
        if result_type == "numeric":
            operator = "<=" if str(getattr(binding, "direction", "")) == "lower_is_better" else ">="
            rules.append(
                QualityRule(
                    evaluator_id=evaluator_id,
                    operator=operator,
                    threshold=float(getattr(binding, "threshold", 1.0)),
                    result_type="numeric",
                    required=True,
                )
            )
        else:
            # Non-numeric measurements are recorded and shown, but they do not
            # decide quality unless the user writes an explicit typed rule.
            rules.append(
                QualityRule(
                    evaluator_id=evaluator_id,
                    operator=None,
                    result_type=result_type,
                    required=False,
                )
            )
    return freeze_quality_policy(
        policy_id="default-all-required",
        version=QUALITY_POLICY_SCHEMA_VERSION,
        rules=rules,
        bindings={str(getattr(b, "evaluator_id", "")): b for b in bindings},
        description="默认策略：全部诊断指标均为必要规则，按各自冻结方向判定。",
    )


def legacy_quality_policy(bindings: Sequence[Any]) -> QualityPolicy | None:
    """Reproduce the pre-#83 judgement of a Manifest that froze no policy.

    Issue #83 must not rewrite history: a Launch created before QualityPolicy
    existed was judged by "every selected numeric evaluator >= its frozen
    threshold", and that is exactly how its recorded result is re-read. The
    returned policy carries the ``legacy-manifest`` id so a reader can tell an
    inherited verdict from one a user actually configured.
    """
    rules: list[QualityRule] = []
    for binding in bindings:
        if _binding_result_type(binding) != "numeric":
            continue
        rules.append(
            QualityRule(
                evaluator_id=str(getattr(binding, "evaluator_id", "")),
                operator=">=",
                threshold=float(getattr(binding, "threshold", 1.0)),
                result_type="numeric",
                required=True,
            )
        )
    if not rules:
        return None
    return freeze_quality_policy(
        policy_id="legacy-manifest",
        version=QUALITY_POLICY_SCHEMA_VERSION,
        rules=rules,
        bindings={str(getattr(b, "evaluator_id", "")): b for b in bindings},
        description="历史任务在冻结质量策略之前的判定口径：全部数值指标 >= 各自冻结阈值。",
    )


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleEvaluation:
    """The outcome of one rule for one item, with a user-readable reason."""

    evaluator_id: str
    result_type: str
    required: bool
    critical: bool
    operator: str | None
    expected: Any
    observed_value: Any = None
    observed_status: str | None = None
    conclusion: str = QUALITY_CONCLUSION_UNKNOWN
    reason_code: str | None = None
    explanation: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "evaluator_id": self.evaluator_id,
            "result_type": self.result_type,
            "required": self.required,
            "critical": self.critical,
            "operator": self.operator,
            "expected": self.expected,
            "observed_value": self.observed_value,
            "observed_status": self.observed_status,
            "conclusion": self.conclusion,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class QualityDecision:
    """The whole per-item decision: one conclusion plus every rule's reason."""

    conclusion: str
    policy_id: str | None
    policy_version: str | None
    policy_digest: str | None
    decided_by: str
    rule_evaluations: tuple[RuleEvaluation, ...] = ()
    unknown_reasons: tuple[str, ...] = ()
    releasable: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "conclusion": self.conclusion,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "decided_by": self.decided_by,
            "releasable": self.releasable,
            "unknown_reasons": list(self.unknown_reasons),
            "rules": [rule.payload() for rule in self.rule_evaluations],
        }

    def rule_for(self, evaluator_id: str) -> RuleEvaluation | None:
        for rule in self.rule_evaluations:
            if rule.evaluator_id == evaluator_id:
                return rule
        return None


def _unknown_decision(
    *, policy_id: str | None, policy_version: str | None, policy_digest: str | None,
    decided_by: str, reason: str,
) -> QualityDecision:
    return QualityDecision(
        conclusion=QUALITY_CONCLUSION_UNKNOWN,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_digest=policy_digest,
        decided_by=decided_by,
        rule_evaluations=(),
        unknown_reasons=(reason,),
        releasable=False,
    )


def _compare(rule: QualityRule, observed: Any) -> tuple[bool, str]:
    if rule.operator == ">=":
        ok = float(observed) >= float(rule.threshold)
        return ok, f"实际值 {observed} {'>=' if ok else '<'} 阈值 {rule.threshold}"
    if rule.operator == "<=":
        ok = float(observed) <= float(rule.threshold)
        return ok, f"实际值 {observed} {'<=' if ok else '>'} 阈值 {rule.threshold}"
    # Explicit typed equality: a boolean is never compared to 1/0, and a
    # category is never ordered.
    ok = type(observed) is type(rule.expected_value) and observed == rule.expected_value
    return ok, f"实际值 {_render_value(observed)} 与期望 {_render_value(rule.expected_value)} {'一致' if ok else '不一致'}"


def evaluate_quality_policy(
    results: Mapping[str, Any] | Sequence[Any] | None,
    policy: QualityPolicy | None,
    *,
    decided_by: str = "QUALITY_POLICY",
) -> QualityDecision:
    """Decide one item's quality conclusion from typed results and a policy.

    ``results`` accepts either the ``evaluator_id -> result`` mapping or a
    sequence of result payloads. A missing / failed / skipped / valueless
    required rule yields UNKNOWN — never a fabricated FAIL.
    """
    if policy is None:
        return _unknown_decision(
            policy_id=None,
            policy_version=None,
            policy_digest=None,
            decided_by="NO_FROZEN_POLICY",
            reason="本次任务没有冻结质量策略，无法给出质量结论。",
        )

    indexed: dict[str, Any] = {}
    if isinstance(results, Mapping):
        indexed = {str(key): value for key, value in results.items()}
    else:
        for entry in results or ():
            evaluator_id = _field(entry, "evaluator_id")
            if evaluator_id:
                indexed[str(evaluator_id)] = entry

    evaluations: list[RuleEvaluation] = []
    has_unknown_required = False
    has_violation = False

    for rule in policy.rules:
        raw = indexed.get(rule.evaluator_id)
        if raw is None:
            reason_code = "QUALITY_EVIDENCE_MISSING"
            base = RuleEvaluation(
                evaluator_id=rule.evaluator_id,
                result_type=rule.result_type,
                required=rule.required,
                critical=rule.critical,
                operator=rule.operator,
                expected=rule.threshold if rule.threshold is not None else rule.expected_value,
                conclusion=QUALITY_CONCLUSION_UNKNOWN,
                reason_code=reason_code,
                explanation=f"必要指标 '{rule.evaluator_id}' 没有结果，证据不足，无法判定通过或不通过。",
            )
            evaluations.append(base)
            if rule.required:
                has_unknown_required = True
            continue

        status = str(_field(raw, "status") or "")
        observed = _field(raw, "value")
        error_code = _field(raw, "error_code")
        error_message = _field(raw, "error_message")

        if status != "succeeded" or observed is None:
            reason = error_message or {
                "failed": "评测执行失败",
                "skipped": "该指标本次未评测",
                "no_result": "评测没有产出结果值",
            }.get(status, "评测未产出可用结果")
            evaluations.append(
                RuleEvaluation(
                    evaluator_id=rule.evaluator_id,
                    result_type=rule.result_type,
                    required=rule.required,
                    critical=rule.critical,
                    operator=rule.operator,
                    expected=rule.threshold if rule.threshold is not None else rule.expected_value,
                    observed_value=observed,
                    observed_status=status or None,
                    conclusion=QUALITY_CONCLUSION_UNKNOWN,
                    reason_code=error_code or f"QUALITY_EVIDENCE_{status.upper() or 'MISSING'}",
                    explanation=f"必要指标 '{rule.evaluator_id}' 证据不足（{reason}），无法判定质量结论。",
                )
            )
            if rule.required:
                has_unknown_required = True
            continue

        ok, explanation = _compare(rule, observed)
        evaluations.append(
            RuleEvaluation(
                evaluator_id=rule.evaluator_id,
                result_type=rule.result_type,
                required=rule.required,
                critical=rule.critical,
                operator=rule.operator,
                expected=rule.threshold if rule.threshold is not None else rule.expected_value,
                observed_value=observed,
                observed_status=status,
                conclusion=QUALITY_CONCLUSION_PASS if ok else QUALITY_CONCLUSION_FAIL,
                reason_code=None if ok else "QUALITY_RULE_VIOLATED",
                explanation=explanation if ok else f"必要指标 '{rule.evaluator_id}' 违反规则：{explanation}。",
            )
        )
        if not ok and rule.required:
            has_violation = True

    unknown_reasons = tuple(
        rule.explanation for rule in evaluations if rule.conclusion == QUALITY_CONCLUSION_UNKNOWN and rule.required
    )

    if has_unknown_required:
        conclusion = QUALITY_CONCLUSION_UNKNOWN
    elif has_violation:
        conclusion = QUALITY_CONCLUSION_FAIL
    else:
        conclusion = QUALITY_CONCLUSION_PASS

    return QualityDecision(
        conclusion=conclusion,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_digest=policy.policy_digest,
        decided_by=decided_by,
        rule_evaluations=tuple(evaluations),
        unknown_reasons=unknown_reasons,
        releasable=conclusion in (QUALITY_CONCLUSION_PASS, QUALITY_CONCLUSION_FAIL),
    )
