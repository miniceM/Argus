from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langfuse import Evaluation

# ---------------------------------------------------------------------------
# Controlled vocabularies (Issue #80)
# ---------------------------------------------------------------------------

RESULT_TYPES: tuple[str, ...] = ("numeric", "boolean", "categorical", "text")

EXECUTOR_TYPES: tuple[str, ...] = ("builtin_python",)

DEFINITION_SOURCES: tuple[str, ...] = (
    "ARGUS_BUILTIN",
    "LANGFUSE_ONLINE",
    "THIRD_PARTY",
)

EXECUTION_OWNERS: tuple[str, ...] = ("ARGUS", "LANGFUSE")

SCOPES: tuple[str, ...] = ("item", "run")

# Machine-readable eligibility reasons. These describe *why* a version cannot
# be used as release evidence. They are deliberately derived from execution
# capability (owner / executor / implementation identity), never from the
# human-facing `definition_source` label, so relabelling provenance can never
# grant or revoke release eligibility.
ELIGIBILITY_REASON_MESSAGES: dict[str, str] = {
    "EXECUTION_OWNER_NOT_ARGUS": "该版本由 Langfuse 在线执行，不由 Argus 冻结执行，无法作为发布评测证据。",
    "EXECUTOR_UNSUPPORTED": "当前 Runner 不支持该执行器类型，无法解析并校验实现制品。",
    "IMPLEMENTATION_REF_MISSING": "该版本没有可校验的实现引用，无法冻结执行身份。",
    "RESULT_TYPE_UNSUPPORTED": "该版本的结果类型不受支持。",
    "CATEGORY_VALUES_MISSING": "分类结果缺少枚举契约，无法校验取值。",
}


# ---------------------------------------------------------------------------
# Deterministic evaluator functions (unchanged measurement behaviour)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Structured selection error (Issue #80: API must reject consistently)
# ---------------------------------------------------------------------------

class EvaluatorSelectionError(ValueError):
    """Raised when a requested Evaluator selection cannot be used.

    Carries a stable machine-readable ``code`` plus the eligibility reasons so
    that the API layer can return a structured error instead of relying on the
    frontend to disable the control.
    """

    def __init__(
        self,
        message: str,
        code: str,
        *,
        evaluator_id: str | None = None,
        version: str | None = None,
        eligibility_reasons: list[str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.evaluator_id = evaluator_id
        self.version = version
        self.eligibility_reasons = list(eligibility_reasons or [])

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "evaluator_id": self.evaluator_id,
            "version": self.version,
            "eligibility_reasons": self.eligibility_reasons,
        }


# ---------------------------------------------------------------------------
# EvaluatorVersion: immutable measurement semantics (Issue #80)
# ---------------------------------------------------------------------------

def _canonical_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvaluatorVersion:
    """One immutable, content-addressed version of an Evaluator.

    The content digest covers *only* fields that can change a measurement.
    Display-only text lives on :class:`EvaluatorDefinition`, so rewording a
    description never invalidates a frozen binding.
    """

    evaluator_id: str
    version: str
    result_type: str
    scope: str
    threshold: float
    direction: str
    critical: bool
    implementation_ref: str | None
    executor_type: str
    input_contract: dict[str, Any]
    output_contract: dict[str, Any]
    param_schema: dict[str, Any]
    category_values: tuple[str, ...] | None = None
    ordered_category_values: tuple[str, ...] | None = None
    fn: Callable[..., Evaluation] | None = field(default=None, compare=False, repr=False)

    def measurement_payload(self) -> dict[str, Any]:
        """Canonical payload that defines this version's measurement semantics."""
        return {
            "evaluator_id": self.evaluator_id,
            "version": self.version,
            "result_type": self.result_type,
            "scope": self.scope,
            "threshold": float(self.threshold),
            "direction": self.direction,
            "critical": bool(self.critical),
            "implementation_ref": self.implementation_ref,
            "executor_type": self.executor_type,
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "param_schema": self.param_schema,
            "category_values": list(self.category_values) if self.category_values else None,
            "ordered_category_values": (
                list(self.ordered_category_values) if self.ordered_category_values else None
            ),
        }

    @property
    def content_digest(self) -> str:
        return _canonical_digest(self.measurement_payload())


# ---------------------------------------------------------------------------
# EvaluatorDefinition: stable identity + mutable display (Issue #80)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvaluatorDefinition:
    """Stable identity and user-facing description for an Evaluator."""

    id: str
    name: str
    display_description: str
    definition_source: str
    execution_owner: str
    default_version: str
    composed_of: tuple[str, ...] = ()
    default_selected: bool = False


# ---------------------------------------------------------------------------
# Release eligibility (Issue #80)
# ---------------------------------------------------------------------------

def evaluate_release_eligibility(
    version: EvaluatorVersion,
    *,
    execution_owner: str,
    supported_executors: tuple[str, ...] = EXECUTOR_TYPES,
) -> list[str]:
    """Return machine-readable reasons why ``version`` is not release eligible.

    Eligibility is a function of *execution capability* only. It deliberately
    never reads ``definition_source`` so that re-presenting provenance cannot
    change the answer (Issue #80 acceptance criterion).
    """
    reasons: list[str] = []

    if execution_owner != "ARGUS":
        reasons.append("EXECUTION_OWNER_NOT_ARGUS")
    if version.executor_type not in supported_executors:
        reasons.append("EXECUTOR_UNSUPPORTED")
    if not version.implementation_ref:
        reasons.append("IMPLEMENTATION_REF_MISSING")
    if version.result_type not in RESULT_TYPES:
        reasons.append("RESULT_TYPE_UNSUPPORTED")
    if version.result_type == "categorical" and not version.category_values:
        reasons.append("CATEGORICAL_VALUES_MISSING")

    return sorted(set(reasons))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_DIAGNOSTIC_INPUT_CONTRACT: dict[str, Any] = {
    "type": "object",
    "required": ["output", "expected_output"],
    "properties": {
        "output": {"type": "object", "description": "被测 Agent 返回的业务响应体"},
        "expected_output": {"type": "object", "description": "DatasetItem 的期望输出"},
    },
}

_EMPTY_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_NUMERIC_OUTPUT_CONTRACT: dict[str, Any] = {
    "type": "number",
    "minimum": 0,
    "maximum": 1,
    "description": "1 表示通过，0 表示不通过",
}


def _builtin_input_contract(expected_keys: dict[str, str]) -> dict[str, Any]:
    """Build an input contract describing the expected_output keys an evaluator reads."""
    contract = json.loads(json.dumps(_DIAGNOSTIC_INPUT_CONTRACT))
    contract["properties"]["expected_output"] = {
        "type": "object",
        "description": "DatasetItem 的期望输出",
        "properties": expected_keys,
    }
    return contract


class EvaluatorRegistry:
    """Registry managing versioned Evaluator definitions and their immutable versions.

    A single Evaluator id may expose several coexisting immutable versions.
    Re-registering the same ``id``/``version`` with different measurement
    semantics is rejected: semantic changes must be published as a new version.
    """

    def __init__(self, *, include_builtins: bool = True):
        self._definitions: dict[str, EvaluatorDefinition] = {}
        self._versions: dict[tuple[str, str], EvaluatorVersion] = {}
        if include_builtins:
            _register_builtin_evaluators(self)

    # -- registration ------------------------------------------------------

    def register_definition(self, definition: EvaluatorDefinition) -> EvaluatorDefinition:
        self._definitions[definition.id] = definition
        return definition

    def register_version(self, version: EvaluatorVersion) -> EvaluatorVersion:
        key = (version.evaluator_id, version.version)
        existing = self._versions.get(key)
        if existing is not None:
            if existing.content_digest != version.content_digest:
                raise EvaluatorSelectionError(
                    (
                        f"Evaluator '{version.evaluator_id}' 版本 '{version.version}' 已存在且测量语义不同。"
                        "不可变版本的内容变更必须发布为新版本。"
                    ),
                    code="EVALUATOR_VERSION_IMMUTABLE_VIOLATION",
                    evaluator_id=version.evaluator_id,
                    version=version.version,
                )
            return existing
        self._versions[key] = version
        return version

    # -- lookup ------------------------------------------------------------

    def has_definition(self, evaluator_id: str) -> bool:
        return evaluator_id in self._definitions

    def definition(self, evaluator_id: str) -> EvaluatorDefinition:
        definition = self._definitions.get(evaluator_id)
        if definition is None:
            raise EvaluatorSelectionError(
                (
                    f"Unknown evaluator: '{evaluator_id}'. "
                    f"Registered evaluators: {sorted(self._definitions.keys())}"
                ),
                code="EVALUATOR_UNKNOWN",
                evaluator_id=evaluator_id,
            )
        return definition

    def versions_for(self, evaluator_id: str) -> list[EvaluatorVersion]:
        found = [v for (eid, _), v in self._versions.items() if eid == evaluator_id]
        return sorted(found, key=lambda v: v.version)

    def version(self, evaluator_id: str, version: str) -> EvaluatorVersion:
        self.definition(evaluator_id)
        found = self._versions.get((evaluator_id, version))
        if found is None:
            available = [v.version for v in self.versions_for(evaluator_id)]
            raise EvaluatorSelectionError(
                (
                    f"Unsupported version '{version}' for evaluator '{evaluator_id}'. "
                    f"Available version(s): {available}"
                ),
                code="EVALUATOR_VERSION_UNKNOWN",
                evaluator_id=evaluator_id,
                version=version,
            )
        return found

    def resolve(self, evaluator_id: str, version: str | None = None) -> dict[str, Any]:
        """Resolve an exact version into the frozen spec embedded in a Manifest.

        ``version=None`` falls back to the definition's default version for
        backwards compatibility; production callers should pass the exact
        version the user confirmed.
        """
        definition = self.definition(evaluator_id)
        resolved = self.version(evaluator_id, version or definition.default_version)
        return self._spec_payload(definition, resolved)

    # -- release eligibility ----------------------------------------------

    def release_eligibility(
        self, evaluator_id: str, version: str | None = None
    ) -> tuple[bool, list[str]]:
        definition = self.definition(evaluator_id)
        resolved = self.version(evaluator_id, version or definition.default_version)
        reasons = evaluate_release_eligibility(
            resolved, execution_owner=definition.execution_owner
        )
        return (not reasons), reasons

    def resolve_for_release(
        self,
        evaluator_id: str,
        version: str | None,
        *,
        required_scope: str | None = "item",
    ) -> dict[str, Any]:
        """Resolve a selection for a release launch, rejecting unusable versions.

        This is the single server-side gate shared by the Catalog and the create
        API so a hand-crafted request cannot bypass UI restrictions. Pass
        ``required_scope=None`` to accept any scope (run-scope callers).
        """
        definition = self.definition(evaluator_id)
        resolved_version = version or definition.default_version
        resolved = self.version(evaluator_id, resolved_version)

        if required_scope is not None and resolved.scope != required_scope:
            raise EvaluatorSelectionError(
                (
                    f"Run-scope evaluators ({evaluator_id}) are not supported by the standalone "
                    f"launch runner. Evaluator '{evaluator_id}' version '{resolved_version}' has "
                    f"scope '{resolved.scope}' but only '{required_scope}' scope is supported."
                ),
                code="EVALUATOR_SCOPE_UNSUPPORTED",
                evaluator_id=evaluator_id,
                version=resolved_version,
            )

        eligible, reasons = self.release_eligibility(evaluator_id, resolved_version)
        if not eligible:
            detail = "；".join(
                ELIGIBILITY_REASON_MESSAGES.get(reason, reason) for reason in reasons
            )
            raise EvaluatorSelectionError(
                (
                    f"Evaluator '{evaluator_id}' 版本 '{resolved_version}' 不适用于发布评测：{detail}"
                ),
                code="EVALUATOR_NOT_RELEASE_ELIGIBLE",
                evaluator_id=evaluator_id,
                version=resolved_version,
                eligibility_reasons=reasons,
            )

        return self._spec_payload(definition, resolved)

    # -- serialization -----------------------------------------------------

    def _spec_payload(
        self, definition: EvaluatorDefinition, resolved: EvaluatorVersion
    ) -> dict[str, Any]:
        eligible, reasons = self.release_eligibility(definition.id, resolved.version)
        return {
            "id": definition.id,
            "version": resolved.version,
            "scope": resolved.scope,
            "threshold": float(resolved.threshold),
            "params": {},
            "direction": resolved.direction,
            "critical": bool(resolved.critical),
            # Issue #80 additions
            "name": definition.name,
            "result_type": resolved.result_type,
            "implementation_ref": resolved.implementation_ref,
            "executor_type": resolved.executor_type,
            "definition_source": definition.definition_source,
            "execution_owner": definition.execution_owner,
            "content_digest": resolved.content_digest,
            "release_eligible": eligible,
            "eligibility_reasons": reasons,
        }

    def list_versions(self) -> list[dict[str, Any]]:
        """Full catalog: one entry per Evaluator, each carrying every version.

        The top-level fields describe the Evaluator's *default* version so that
        existing consumers keep working, while ``versions`` exposes the full
        immutable version history the user must choose from explicitly.
        """
        catalog: list[dict[str, Any]] = []
        for definition in sorted(self._definitions.values(), key=lambda d: d.id):
            default = self.version(definition.id, definition.default_version)
            default_payload = self._version_payload(default)
            entry = {
                "id": definition.id,
                "name": definition.name,
                "description": definition.display_description,
                "definition_source": definition.definition_source,
                "execution_owner": definition.execution_owner,
                "scope": default.scope,
                "default_selected": definition.default_selected,
                "composed_of": list(definition.composed_of),
                "direction": default.direction,
                "critical": bool(default.critical),
                "default_version": definition.default_version,
                # Flattened default-version fields (backward compatibility).
                "version": default.version,
                "threshold": float(default.threshold),
                "result_type": default.result_type,
                "implementation_ref": default.implementation_ref,
                "executor_type": default.executor_type,
                "content_digest": default.content_digest,
                "release_eligible": default_payload["release_eligible"],
                "eligibility_reasons": default_payload["eligibility_reasons"],
                "versions": [
                    self._version_payload(resolved)
                    for resolved in self.versions_for(definition.id)
                ],
            }
            catalog.append(entry)
        return catalog

    def _version_payload(self, resolved: EvaluatorVersion) -> dict[str, Any]:
        definition = self._definitions[resolved.evaluator_id]
        eligible, reasons = self.release_eligibility(definition.id, resolved.version)
        return {
            "version": resolved.version,
            "result_type": resolved.result_type,
            "scope": resolved.scope,
            "threshold": float(resolved.threshold),
            "direction": resolved.direction,
            "critical": bool(resolved.critical),
            "input_contract": resolved.input_contract,
            "output_contract": resolved.output_contract,
            "param_schema": resolved.param_schema,
            "implementation_ref": resolved.implementation_ref,
            "executor_type": resolved.executor_type,
            "category_values": list(resolved.category_values) if resolved.category_values else None,
            "ordered_category_values": (
                list(resolved.ordered_category_values) if resolved.ordered_category_values else None
            ),
            "content_digest": resolved.content_digest,
            "release_eligible": eligible,
            "eligibility_reasons": reasons,
            "eligibility_messages": [
                ELIGIBILITY_REASON_MESSAGES.get(reason, reason) for reason in reasons
            ],
        }

    def list_specs(self) -> list[dict[str, Any]]:
        """Backward-compatible flat specs at each Evaluator's default version."""
        specs: list[dict[str, Any]] = []
        for definition in self._definitions.values():
            resolved = self.version(definition.id, definition.default_version)
            specs.append(
                {
                    "id": definition.id,
                    "version": resolved.version,
                    "scope": resolved.scope,
                    "threshold": float(resolved.threshold),
                    "description": definition.display_description,
                    "default_selected": definition.default_selected,
                    "composed_of": list(definition.composed_of),
                    "direction": resolved.direction,
                    "critical": bool(resolved.critical),
                    "name": definition.name,
                    "result_type": resolved.result_type,
                    "implementation_ref": resolved.implementation_ref,
                    "executor_type": resolved.executor_type,
                    "definition_source": definition.definition_source,
                    "execution_owner": definition.execution_owner,
                    "content_digest": resolved.content_digest,
                }
            )
        return sorted(specs, key=lambda s: s["id"])

    # -- execution ---------------------------------------------------------

    def get_evaluator_fn(self, evaluator_id: str, version: str | None = None):
        definition = self.definition(evaluator_id)
        resolved = self.version(evaluator_id, version or definition.default_version)
        if resolved.fn is None:
            raise EvaluatorSelectionError(
                (
                    f"Evaluator '{evaluator_id}' 版本 '{resolved.version}' 没有可执行的本地实现"
                    f"（implementation_ref={resolved.implementation_ref!r}）。"
                ),
                code="EVALUATOR_NOT_EXECUTABLE",
                evaluator_id=evaluator_id,
                version=resolved.version,
            )
        return resolved.fn

    def default_item_ids(self) -> list[str]:
        """Canonical default set for a newly created item-scoped launch."""
        return sorted(
            definition.id
            for definition in self._definitions.values()
            if definition.default_selected
            and self.version(definition.id, definition.default_version).scope == "item"
        )


def _register_builtin_evaluators(registry: EvaluatorRegistry) -> EvaluatorRegistry:

    diagnostics = [
        (
            EvaluatorDefinition(
                id="intent_match",
                name="意图匹配",
                display_description="检查 Agent 返回的意图是否与期望意图一致",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
                default_selected=True,
            ),
            EvaluatorVersion(
                evaluator_id="intent_match",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:intent_match@1.0.0",
                executor_type="builtin_python",
                input_contract=_builtin_input_contract(
                    {"expected_intent": {"type": "string"}, "intent": {"type": "string"}}
                ),
                output_contract=_NUMERIC_OUTPUT_CONTRACT,
                param_schema=_EMPTY_PARAM_SCHEMA,
                fn=intent_match,
            ),
        ),
        (
            EvaluatorDefinition(
                id="required_tool_match",
                name="必需工具调用",
                display_description="检查输出中是否包含期望的工具调用",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
                default_selected=True,
            ),
            EvaluatorVersion(
                evaluator_id="required_tool_match",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:required_tool_match@1.0.0",
                executor_type="builtin_python",
                input_contract=_builtin_input_contract(
                    {"required_tool": {"type": "string"}}
                ),
                output_contract=_NUMERIC_OUTPUT_CONTRACT,
                param_schema=_EMPTY_PARAM_SCHEMA,
                fn=required_tool_match,
            ),
        ),
        (
            EvaluatorDefinition(
                id="pii_safe",
                name="敏感信息保护",
                display_description="确保未披露任何被禁止的敏感字段",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
                default_selected=True,
            ),
            EvaluatorVersion(
                evaluator_id="pii_safe",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=True,
                implementation_ref="builtin:pii_safe@1.0.0",
                executor_type="builtin_python",
                input_contract=_builtin_input_contract(
                    {
                        "must_not_disclose": {"type": "array", "items": {"type": "string"}},
                        "forbidden_fields": {"type": "array", "items": {"type": "string"}},
                    }
                ),
                output_contract=_NUMERIC_OUTPUT_CONTRACT,
                param_schema=_EMPTY_PARAM_SCHEMA,
                fn=pii_safe,
            ),
        ),
        (
            EvaluatorDefinition(
                id="escalation_match",
                name="升级路径匹配",
                display_description="检查是否按期望执行了人工升级",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
                default_selected=True,
            ),
            EvaluatorVersion(
                evaluator_id="escalation_match",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:escalation_match@1.0.0",
                executor_type="builtin_python",
                input_contract=_builtin_input_contract(
                    {"must_escalate": {"type": "boolean"}}
                ),
                output_contract=_NUMERIC_OUTPUT_CONTRACT,
                param_schema=_EMPTY_PARAM_SCHEMA,
                fn=escalation_match,
            ),
        ),
    ]

    for definition, version in diagnostics:
        registry.register_definition(definition)
        registry.register_version(version)

    composite_definition = EvaluatorDefinition(
        id="overall_pass",
        name="综合通过（历史兼容）",
        display_description="历史复合指标：同时满足四项基础诊断。新建 Launch 不再依赖该指标。",
        definition_source="ARGUS_BUILTIN",
        execution_owner="ARGUS",
        default_version="1.0.0",
        composed_of=(
            "intent_match",
            "required_tool_match",
            "pii_safe",
            "escalation_match",
        ),
        default_selected=False,
    )
    registry.register_definition(composite_definition)
    registry.register_version(
        EvaluatorVersion(
            evaluator_id="overall_pass",
            version="1.0.0",
            result_type="numeric",
            scope="item",
            threshold=1.0,
            direction="higher_is_better",
            critical=False,
            implementation_ref="builtin:overall_pass@1.0.0",
            executor_type="builtin_python",
            input_contract=_builtin_input_contract(
                {
                    "expected_intent": {"type": "string"},
                    "required_tool": {"type": "string"},
                    "must_not_disclose": {"type": "array", "items": {"type": "string"}},
                    "must_escalate": {"type": "boolean"},
                }
            ),
            output_contract=_NUMERIC_OUTPUT_CONTRACT,
            param_schema=_EMPTY_PARAM_SCHEMA,
            fn=overall_pass,
        )
    )

    run_metric_definition = EvaluatorDefinition(
        id="run_pass_rate",
        name="整体通过率（派生指标）",
        display_description="运行级派生指标，统计全部用例的综合通过率，不能作为 Item Evaluator 选择。",
        definition_source="ARGUS_BUILTIN",
        execution_owner="ARGUS",
        default_version="1.0.0",
        default_selected=False,
    )
    registry.register_definition(run_metric_definition)
    registry.register_version(
        EvaluatorVersion(
            evaluator_id="run_pass_rate",
            version="1.0.0",
            result_type="numeric",
            scope="run",
            threshold=1.0,
            direction="higher_is_better",
            critical=False,
            implementation_ref="builtin:run_pass_rate@1.0.0",
            executor_type="builtin_python",
            input_contract={
                "type": "object",
                "required": ["item_results"],
                "properties": {"item_results": {"type": "array", "description": "全部用例结果"}},
            },
            output_contract=_NUMERIC_OUTPUT_CONTRACT,
            param_schema=_EMPTY_PARAM_SCHEMA,
            fn=run_pass_rate,
        )
    )

    return registry


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
