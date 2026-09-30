"""Frozen Evaluator execution identity (Argus Issue #81, S3.1).

A Launch is only reproducible when the *executable identity* of every metric is
frozen, not just its id/version pair. This module owns that identity:

* :class:`EvaluatorBinding` — the frozen, content-addressed record stored in the
  Manifest (definition digest + implementation artifact + immutable Runner
  identity + parameter snapshot).
* :class:`EvaluatorExecutor` — the single resolve / validate / execute boundary
  every execution path (Worker, synchronous run, resume) must go through.
* :func:`resolve_execution_plan` / :func:`evaluate_frozen_item` — the shared
  entry points, so no caller can branch per evaluator id or bypass validation.
* Legacy Manifests (schema 1.0/1.1) are read explicitly and marked
  ``HISTORICAL_CONTRACT_UNRECORDED``: missing evidence is reported, never faked.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import textwrap
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .evaluators import (
    Evaluation,
    EvaluatorDefinition,
    EvaluatorSelectionError,
    EvaluatorVersion,
    default_evaluator_registry,
    evaluate_release_eligibility,
)

MANIFEST_BINDING_SCHEMA_VERSION = "1.2"
LEGACY_MANIFEST_SCHEMAS = ("1.0", "1.1")

FROZEN_CONTRACT_STATUS = "FROZEN_VERIFIED"
LEGACY_CONTRACT_STATUS = "HISTORICAL_CONTRACT_UNRECORDED"

SUPPORTED_EXECUTOR_TYPES = ("builtin_python",)

_DIGEST_PREFIX_RE = r"^sha256:[0-9a-f]{64}$"

RECOVERY_HINTS: dict[str, str] = {
    "EVALUATOR_VERSION_UNAVAILABLE": (
        "冻结的评测版本在当前 Runner 中不可用。请恢复与冻结版本一致的评测制品后重试，"
        "或另建一个 Launch 并选择当前可用版本；禁止改用其他版本继续本次评测。"
    ),
    "EVALUATOR_BINDING_DIGEST_MISMATCH": (
        "当前注册的评测定义与冻结摘要不一致，说明实现已被改动。请恢复冻结制品，"
        "或另建 Launch 以冻结当前实现；本次评测不会自动改用新版本。"
    ),
    "EVALUATOR_ARTIFACT_UNRESOLVABLE": (
        "冻结的实现制品无法解析到可执行内容。请恢复该制品后重试，或另建 Launch。"
    ),
    "EVALUATOR_ARTIFACT_DIGEST_MISMATCH": (
        "实现制品摘要与冻结记录不一致，存在篡改或版本漂移。请恢复冻结制品，或另建 Launch。"
    ),
    "EVALUATOR_EXECUTOR_UNSUPPORTED": (
        "当前 Runner 不支持该冻结执行器。请使用包含该执行器的 Runner 镜像恢复执行，或另建 Launch。"
    ),
    "RUNNER_VERSION_MISMATCH": (
        "当前 Runner 身份与冻结记录不一致。请使用冻结的 Runner 镜像恢复执行，或另建 Launch。"
    ),
    "RUNNER_IDENTITY_UNAVAILABLE": (
        "冻结记录或当前 Runner 缺少可验证的构建身份，无法继续执行。请使用可验证构建的 Runner 镜像。"
    ),
}


class EvaluatorBindingError(EvaluatorSelectionError):
    """Stable, user-readable execution precondition failure (Issue #81)."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        evaluator_id: str | None = None,
        version: str | None = None,
        expected: Any = None,
        actual: Any = None,
        recovery: str | None = None,
    ):
        super().__init__(message, code=code, evaluator_id=evaluator_id, version=version)
        self.expected = expected
        self.actual = actual
        self.recovery = recovery or RECOVERY_HINTS.get(code)

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload.update(
            {
                "expected": self.expected,
                "actual": self.actual,
                "recovery": self.recovery,
            }
        )
        return payload


def canonical_digest(payload: dict[str, Any]) -> str:
    """Field-order independent SHA-256 (``sha256:`` prefixed) of a payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def is_digest_shaped(value: Any) -> bool:
    return isinstance(value, str) and re.match(_DIGEST_PREFIX_RE, value) is not None


def normalize_digest(value: Any) -> str:
    """Accept legacy bare-hex digests and expose them as ``sha256:<hex>``."""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.lower().startswith("sha256:"):
        return text.lower()
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return f"sha256:{text.lower()}"
    return text


# ---------------------------------------------------------------------------
# Implementation artifact (verifiable, never a mutable tag)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImplementationArtifact:
    """Verifiable identity of the code that will actually execute.

    ``digest`` must come from a build product. For the built-in Python
    executors it is the SHA-256 of the real implementation source plus its
    pinned ``implementation_ref`` and versioned dependency digests — never an
    arbitrary environment string or a mutable tag.
    """

    kind: str
    locator: str
    digest: str
    runtime: str
    dependencies: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "digest": self.digest,
            "runtime": self.runtime,
            "dependencies": list(self.dependencies),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> ImplementationArtifact | None:
        if not isinstance(payload, dict):
            return None
        digest = payload.get("digest")
        if not is_digest_shaped(digest):
            return None
        return cls(
            kind=str(payload.get("kind") or "unknown"),
            locator=str(payload.get("locator") or ""),
            digest=str(digest),
            runtime=str(payload.get("runtime") or "unknown"),
            dependencies=tuple(str(d) for d in (payload.get("dependencies") or [])),
        )


def python_source_artifact(
    fn: Any,
    implementation_ref: str,
    *,
    dependencies: tuple[str, ...] = (),
) -> ImplementationArtifact | None:
    """Derive a verifiable artifact identity from the implementation source."""
    try:
        source = textwrap.dedent(inspect.getsource(fn)).strip()
    except (OSError, TypeError):  # pragma: no cover - non-source callables
        return None
    if not source:
        return None
    digest = canonical_digest(
        {
            "implementation_ref": implementation_ref,
            "source": source,
            "dependencies": sorted(dependencies),
        }
    )
    module = getattr(fn, "__module__", "unknown")
    qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", "callable"))
    return ImplementationArtifact(
        kind="python_source",
        locator=f"{module}:{qualname}",
        digest=digest,
        runtime="cpython",
        dependencies=tuple(sorted(dependencies)),
    )


# ---------------------------------------------------------------------------
# Frozen binding
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvaluatorBinding:
    """Everything needed to re-execute one metric exactly as frozen."""

    binding_id: str
    evaluator_id: str
    version: str
    scope: str
    result_type: str
    threshold: float
    direction: str
    critical: bool
    input_contract: dict[str, Any]
    output_contract: dict[str, Any]
    params: dict[str, Any]
    definition_digest: str
    implementation_ref: str | None
    executor_type: str
    artifact: ImplementationArtifact | None
    runner: dict[str, Any] = field(default_factory=dict)
    composed_of: tuple[str, ...] = ()
    contract_status: str = FROZEN_CONTRACT_STATUS
    release_eligible: bool = True
    eligibility_reasons: tuple[str, ...] = ()

    def binding_payload(self) -> dict[str, Any]:
        """Canonical payload the binding digest is computed over."""
        return {
            "evaluator_id": self.evaluator_id,
            "version": self.version,
            "scope": self.scope,
            "result_type": self.result_type,
            "threshold": float(self.threshold),
            "direction": self.direction,
            "critical": bool(self.critical),
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "params": self.params,
            "definition_digest": self.definition_digest,
            "content_digest": self.definition_digest.split(":", 1)[-1],
            "implementation_ref": self.implementation_ref,
            "executor_type": self.executor_type,
            "artifact": self.artifact.payload() if self.artifact else None,
            "runner": self.runner,
            "composed_of": list(self.composed_of),
            "contract_status": self.contract_status,
        }

    @property
    def binding_digest(self) -> str:
        return canonical_digest(self.binding_payload())

    def to_payload(self) -> dict[str, Any]:
        payload = {
            # Legacy-compatible spec fields (Manifest schema <= 1.1 readers)
            "id": self.evaluator_id,
            "version": self.version,
            "scope": self.scope,
            "threshold": float(self.threshold),
            "params": dict(self.params),
            "direction": self.direction,
            "critical": bool(self.critical),
            "name": self.evaluator_id,
            # Issue #81 frozen execution identity
            "binding_id": self.binding_id,
            "binding_schema_version": MANIFEST_BINDING_SCHEMA_VERSION,
            "result_type": self.result_type,
            # Issue #80 readers keep the bare-hex digest under its legacy key.
            "content_digest": self.definition_digest.split(":", 1)[-1],
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "definition_digest": self.definition_digest,
            "implementation_ref": self.implementation_ref,
            "executor_type": self.executor_type,
            "implementation_artifact": self.artifact.payload() if self.artifact else None,
            "runner": dict(self.runner),
            "composed_of": list(self.composed_of),
            "contract_status": self.contract_status,
            "binding_digest": self.binding_digest,
            "verification_status": self.verification_status,
            # Issue #80 readers keep the release-eligibility view available.
            "release_eligible": bool(self.release_eligible),
            "eligibility_reasons": list(self.eligibility_reasons),
        }
        return payload

    @property
    def verification_status(self) -> str:
        """Evidence completeness, independent from whether it validates now."""
        if self.contract_status == LEGACY_CONTRACT_STATUS:
            return LEGACY_CONTRACT_STATUS
        if self.artifact is None:
            return "ARTIFACT_UNRECORDED"
        return "RECORDED"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EvaluatorBinding:
        """Read a frozen binding, or a legacy spec without fabricated evidence."""
        evaluator_id = str(payload.get("id") or payload.get("evaluator_id") or "")
        version = str(payload.get("version") or "")
        contract_status = str(payload.get("contract_status") or LEGACY_CONTRACT_STATUS)
        definition_digest = payload.get("definition_digest") or payload.get("content_digest") or ""
        binding_seed = {
            "evaluator_id": evaluator_id,
            "version": version,
            "definition_digest": str(definition_digest),
            "runner": payload.get("runner") or {},
            "contract_status": contract_status,
        }
        return cls(
            binding_id=str(
                payload.get("binding_id")
                or f"bind_{canonical_digest(binding_seed).split(':', 1)[1][:16]}"
            ),
            evaluator_id=evaluator_id,
            version=version,
            scope=str(payload.get("scope") or "item"),
            result_type=str(payload.get("result_type") or "numeric"),
            threshold=float(payload.get("threshold", 1.0)),
            direction=str(payload.get("direction") or "higher_is_better"),
            critical=bool(payload.get("critical", False)),
            input_contract=dict(payload.get("input_contract") or {}),
            output_contract=dict(payload.get("output_contract") or {}),
            params=dict(payload.get("params") or {}),
            definition_digest=normalize_digest(definition_digest),
            implementation_ref=payload.get("implementation_ref"),
            executor_type=str(payload.get("executor_type") or "builtin_python"),
            artifact=ImplementationArtifact.from_payload(payload.get("implementation_artifact")),
            runner=dict(payload.get("runner") or {}),
            composed_of=tuple(str(c) for c in (payload.get("composed_of") or [])),
            contract_status=contract_status,
            release_eligible=bool(payload.get("release_eligible", True)),
            eligibility_reasons=tuple(
                str(r) for r in (payload.get("eligibility_reasons") or [])
            ),
        )


def freeze_binding(
    definition: EvaluatorDefinition,
    version: EvaluatorVersion,
    *,
    runner_identity: dict[str, Any],
    composed_of: tuple[str, ...] = (),
) -> EvaluatorBinding:
    """Freeze the execution identity of one resolved Evaluator version.

    Raises :class:`EvaluatorBindingError` when the implementation artifact
    cannot be verified at creation time; creation must never record an
    unverifiable binding.
    """
    if version.executor_type not in SUPPORTED_EXECUTOR_TYPES:
        raise EvaluatorBindingError(
            f"Evaluator '{definition.id}' 版本 '{version.version}' 的执行器 "
            f"'{version.executor_type}' 不受支持，无法冻结执行身份。",
            code="EVALUATOR_EXECUTOR_UNSUPPORTED",
            evaluator_id=definition.id,
            version=version.version,
            expected=list(SUPPORTED_EXECUTOR_TYPES),
            actual=version.executor_type,
        )
    if version.fn is None:
        raise EvaluatorBindingError(
            f"Evaluator '{definition.id}' 版本 '{version.version}' 没有可解析的实现制品，"
            "无法冻结执行身份。",
            code="EVALUATOR_ARTIFACT_UNRESOLVABLE",
            evaluator_id=definition.id,
            version=version.version,
            expected=version.implementation_ref,
            actual=None,
        )

    reasons = evaluate_release_eligibility(version, execution_owner=definition.execution_owner)
    dependencies = tuple(composed_of)
    artifact = python_source_artifact(
        version.fn, str(version.implementation_ref or ""), dependencies=dependencies
    )
    if artifact is None or not is_digest_shaped(artifact.digest):
        raise EvaluatorBindingError(
            f"Evaluator '{definition.id}' 版本 '{version.version}' 的实现制品摘要不可验证。",
            code="EVALUATOR_ARTIFACT_UNRESOLVABLE",
            evaluator_id=definition.id,
            version=version.version,
            expected="sha256:<64 hex>",
            actual=None,
        )

    binding_seed = {
        "evaluator_id": definition.id,
        "version": version.version,
        "definition_digest": normalize_digest(version.content_digest),
        "artifact": artifact.payload(),
        "runner": runner_identity,
    }
    binding = EvaluatorBinding(
        binding_id=f"bind_{canonical_digest(binding_seed).split(':', 1)[1][:16]}",
        evaluator_id=definition.id,
        version=version.version,
        scope=version.scope,
        result_type=version.result_type,
        threshold=float(version.threshold),
        direction=version.direction,
        critical=bool(version.critical),
        input_contract=dict(version.input_contract),
        output_contract=dict(version.output_contract),
        params={},
        definition_digest=normalize_digest(version.content_digest),
        implementation_ref=version.implementation_ref,
        executor_type=version.executor_type,
        artifact=artifact,
        runner=dict(runner_identity),
        composed_of=dependencies,
        contract_status=FROZEN_CONTRACT_STATUS,
        release_eligible=not reasons,
        eligibility_reasons=tuple(reasons),
    )
    return binding


# ---------------------------------------------------------------------------
# Executor boundary: resolve / validate / execute
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedEvaluator:
    binding: EvaluatorBinding
    implementation: Any
    verification: str

    @property
    def callable(self) -> Any:
        """The validated callable, for SDK/Langfuse-compatible execution paths."""
        return getattr(self.implementation, "fn", self.implementation)


class EvaluatorExecutor(ABC):
    """Uniform execution boundary for every frozen binding."""

    executor_type: str = "abstract"

    @abstractmethod
    def resolve(self, binding: EvaluatorBinding) -> Any:
        """Resolve the frozen implementation. Never falls back to a default."""

    @abstractmethod
    def validate(self, binding: EvaluatorBinding, implementation: Any) -> str | None:
        """Return a stable error code when the frozen identity cannot be honored."""

    @abstractmethod
    def execute(self, implementation: Any, *, output: Any, expected_output: Any) -> Evaluation:
        """Run the resolved implementation for one item."""


class BuiltinPythonExecutor(EvaluatorExecutor):
    executor_type = "builtin_python"

    def __init__(self, registry: Any):
        self.registry = registry

    def resolve(self, binding: EvaluatorBinding) -> EvaluatorVersion:
        try:
            version = self.registry.version(binding.evaluator_id, binding.version)
        except EvaluatorSelectionError as exc:
            raise EvaluatorBindingError(
                f"冻结的评测版本不可用：{exc}",
                code="EVALUATOR_VERSION_UNAVAILABLE",
                evaluator_id=binding.evaluator_id,
                version=binding.version,
                expected=f"{binding.evaluator_id}@{binding.version}",
                actual=None,
            ) from exc
        if version.fn is None:
            raise EvaluatorBindingError(
                f"Evaluator '{binding.evaluator_id}' 版本 '{binding.version}' 的实现制品不可解析。",
                code="EVALUATOR_ARTIFACT_UNRESOLVABLE",
                evaluator_id=binding.evaluator_id,
                version=binding.version,
                expected=binding.implementation_ref,
                actual=None,
            )
        return version

    def validate(self, binding: EvaluatorBinding, implementation: EvaluatorVersion) -> str | None:
        if binding.contract_status == LEGACY_CONTRACT_STATUS:
            # Historical manifests carry no artifact evidence: keep them runnable,
            # but never claim they satisfy the new frozen-execution contract.
            return None

        if normalize_digest(implementation.content_digest) != normalize_digest(
            binding.definition_digest
        ):
            return "EVALUATOR_BINDING_DIGEST_MISMATCH"

        artifact = python_source_artifact(
            implementation.fn,
            str(implementation.implementation_ref or ""),
            dependencies=binding.composed_of,
        )
        if binding.artifact is None or artifact is None:
            return "EVALUATOR_ARTIFACT_UNRESOLVABLE"
        if artifact.digest != binding.artifact.digest:
            return "EVALUATOR_ARTIFACT_DIGEST_MISMATCH"
        if implementation.fn is None:
            return "EVALUATOR_ARTIFACT_UNRESOLVABLE"
        return None

    def execute(self, implementation: Any, *, output: Any, expected_output: Any) -> Evaluation:
        fn = getattr(implementation, "fn", implementation)
        if fn is None:
            raise EvaluatorBindingError(
                "冻结的实现不可执行。",
                code="EVALUATOR_ARTIFACT_UNRESOLVABLE",
            )
        return fn(output=output, expected_output=expected_output)


def build_executor_registry(registry: Any) -> dict[str, EvaluatorExecutor]:
    """Executors available in this Runner, keyed by their frozen executor type."""
    return {BuiltinPythonExecutor.executor_type: BuiltinPythonExecutor(registry)}


def executor_for(
    binding: EvaluatorBinding, executors: dict[str, EvaluatorExecutor]
) -> EvaluatorExecutor:
    executor = executors.get(binding.executor_type)
    if executor is None:
        raise EvaluatorBindingError(
            f"当前 Runner 不支持冻结执行器 '{binding.executor_type}'。",
            code="EVALUATOR_EXECUTOR_UNSUPPORTED",
            evaluator_id=binding.evaluator_id,
            version=binding.version,
            expected=sorted(executors.keys()),
            actual=binding.executor_type,
        )
    return executor


def _raise_for_code(
    code: str, binding: EvaluatorBinding, *, expected: Any = None, actual: Any = None
) -> None:
    raise EvaluatorBindingError(
        RECOVERY_HINTS.get(code, f"冻结评测身份校验失败：{code}"),
        code=code,
        evaluator_id=binding.evaluator_id,
        version=binding.version,
        expected=expected,
        actual=actual,
    )


def validate_binding(
    binding: EvaluatorBinding,
    *,
    registry: Any = None,
    executors: dict[str, EvaluatorExecutor] | None = None,
) -> ResolvedEvaluator:
    """Resolve + validate one frozen binding, or raise a stable error."""
    registry = registry or default_evaluator_registry
    executors = executors if executors is not None else build_executor_registry(registry)
    executor = executor_for(binding, executors)
    implementation = executor.resolve(binding)
    code = executor.validate(binding, implementation)
    if code:
        expected, actual = _mismatch_details(binding, implementation, code)
        _raise_for_code(code, binding, expected=expected, actual=actual)
    verification = (
        LEGACY_CONTRACT_STATUS
        if binding.contract_status == LEGACY_CONTRACT_STATUS
        else "VERIFIED"
    )
    return ResolvedEvaluator(
        binding=binding, implementation=implementation, verification=verification
    )


def _mismatch_details(
    binding: EvaluatorBinding, implementation: Any, code: str
) -> tuple[Any, Any]:
    if code == "EVALUATOR_BINDING_DIGEST_MISMATCH":
        return (
            normalize_digest(binding.definition_digest),
            normalize_digest(getattr(implementation, "content_digest", None)),
        )
    if code == "EVALUATOR_ARTIFACT_DIGEST_MISMATCH":
        current = python_source_artifact(
            implementation.fn,
            str(getattr(implementation, "implementation_ref", "") or ""),
            dependencies=binding.composed_of,
        )
        return (
            binding.artifact.digest if binding.artifact else None,
            current.digest if current else None,
        )
    if code == "EVALUATOR_ARTIFACT_UNRESOLVABLE":
        return binding.artifact.digest if binding.artifact else None, None
    return None, None


def resolve_langfuse_evaluators(
    manifest: dict[str, Any] | None,
    *,
    registry: Any = None,
    executors: dict[str, EvaluatorExecutor] | None = None,
) -> tuple[list[Any], list[Any]]:
    """Validated callables for the Langfuse experiment path (item, run scope)."""
    plan = resolve_execution_plan(manifest, registry=registry, executors=executors)
    item_callables = [r.callable for r in plan if r.binding.scope == "item"]
    run_callables = [r.callable for r in plan if r.binding.scope == "run"]
    return item_callables, run_callables


def bindings_from_manifest(manifest: dict[str, Any] | None) -> list[EvaluatorBinding]:
    """Read frozen bindings, or legacy specs marked as historical contract."""
    manifest = manifest or {}
    entries = manifest.get("evaluators") or []
    return [EvaluatorBinding.from_payload(dict(entry)) for entry in entries]


def manifest_contract_status(manifest: dict[str, Any] | None) -> str:
    """Report whether the Manifest records the Issue #81 frozen identity."""
    manifest = manifest or {}
    schema = str(manifest.get("schema_version") or "")
    bindings = manifest.get("evaluators") or []
    if schema >= MANIFEST_BINDING_SCHEMA_VERSION and bindings and all(
        isinstance(entry, dict) and entry.get("binding_digest") for entry in bindings
    ):
        return FROZEN_CONTRACT_STATUS
    return LEGACY_CONTRACT_STATUS


def resolve_execution_plan(
    manifest: dict[str, Any] | None,
    *,
    registry: Any = None,
    executors: dict[str, EvaluatorExecutor] | None = None,
) -> list[ResolvedEvaluator]:
    """Validate every frozen binding of a Launch before any Agent call."""
    registry = registry or default_evaluator_registry
    executors = executors if executors is not None else build_executor_registry(registry)
    plan: list[ResolvedEvaluator] = []
    for binding in bindings_from_manifest(manifest):
        executor = executor_for(binding, executors)
        implementation = executor.resolve(binding)
        code = executor.validate(binding, implementation)
        if code:
            expected, actual = _mismatch_details(binding, implementation, code)
            _raise_for_code(code, binding, expected=expected, actual=actual)
        plan.append(
            ResolvedEvaluator(
                binding=binding,
                implementation=implementation,
                verification=(
                    LEGACY_CONTRACT_STATUS
                    if binding.contract_status == LEGACY_CONTRACT_STATUS
                    else "VERIFIED"
                ),
            )
        )
    return plan


@dataclass(frozen=True)
class FrozenItemEvaluation:
    """Result of evaluating one item through the frozen execution identity."""

    scores: dict[str, float]
    quality_conclusion: str
    eval_status: str
    eval_error: str | None
    verification_status: str
    evaluators: tuple[dict[str, Any], ...] = ()


def evaluate_frozen_item(
    manifest: dict[str, Any] | None,
    *,
    output: Any,
    expected_output: Any,
    registry: Any = None,
    executors: dict[str, EvaluatorExecutor] | None = None,
    quality_policy: dict[str, Any] | None = None,
) -> FrozenItemEvaluation:
    """The single evaluation entry point for Worker, run and resume paths."""
    from .evaluators import evaluate_item_quality

    registry = registry or default_evaluator_registry
    executors = executors if executors is not None else build_executor_registry(registry)
    manifest = manifest or {}

    plan: list[ResolvedEvaluator] = []
    try:
        plan = [
            resolved
            for resolved in (
                _resolve_one(binding, registry, executors) for binding in bindings_from_manifest(manifest)
            )
            if resolved.binding.scope == "item"
        ]
    except EvaluatorBindingError as exc:
        return FrozenItemEvaluation(
            scores={},
            quality_conclusion="unknown",
            eval_status="failed",
            eval_error=str(exc),
            verification_status=manifest_contract_status(manifest),
        )

    if not plan:
        return FrozenItemEvaluation(
            scores={},
            quality_conclusion="unknown",
            eval_status="skipped",
            eval_error=None,
            verification_status=manifest_contract_status(manifest),
        )

    scores: dict[str, float] = {}
    evaluators: list[dict[str, Any]] = []
    verification_status = manifest_contract_status(manifest)
    try:
        for resolved in plan:
            executor = executor_for(resolved.binding, executors)
            result = executor.execute(
                resolved.implementation, output=output, expected_output=expected_output
            )
            scores[resolved.binding.evaluator_id] = float(getattr(result, "value", 0.0))
            evaluators.append(
                {
                    "id": resolved.binding.evaluator_id,
                    "version": resolved.binding.version,
                    "binding_id": resolved.binding.binding_id,
                    "verification": resolved.verification,
                }
            )
        quality_conclusion = evaluate_item_quality(
            scores,
            [resolved.binding.to_payload() for resolved in plan],
            manifest.get("quality_policy") or quality_policy,
        )
    except Exception as exc:  # noqa: BLE001 - evaluators must never crash a Worker
        return FrozenItemEvaluation(
            scores=scores,
            quality_conclusion="unknown",
            eval_status="failed",
            eval_error=str(exc),
            verification_status=verification_status,
            evaluators=tuple(evaluators),
        )

    if any(resolved.verification != "VERIFIED" for resolved in plan):
        verification_status = LEGACY_CONTRACT_STATUS

    return FrozenItemEvaluation(
        scores=scores,
        quality_conclusion=quality_conclusion,
        eval_status="succeeded",
        eval_error=None,
        verification_status=verification_status,
        evaluators=tuple(evaluators),
    )


def _resolve_one(
    binding: EvaluatorBinding, registry: Any, executors: dict[str, EvaluatorExecutor]
) -> ResolvedEvaluator:
    executor = executor_for(binding, executors)
    implementation = executor.resolve(binding)
    code = executor.validate(binding, implementation)
    if code:
        expected, actual = _mismatch_details(binding, implementation, code)
        _raise_for_code(code, binding, expected=expected, actual=actual)
    return ResolvedEvaluator(
        binding=binding,
        implementation=implementation,
        verification=(
            LEGACY_CONTRACT_STATUS
            if binding.contract_status == LEGACY_CONTRACT_STATUS
            else "VERIFIED"
        ),
    )


def freeze_bindings_for_launch(
    registry: Any,
    evaluator_ids: list[str],
    *,
    runner_identity: dict[str, Any],
    required_scope: str | None = "item",
) -> list[dict[str, Any]]:
    """Freeze bindings for every resolved Evaluator at Launch creation time."""
    payloads: list[dict[str, Any]] = []
    for evaluator_id in sorted(evaluator_ids):
        spec = registry.resolve_for_release(
            evaluator_id, None, required_scope=required_scope
        )
        definition = registry.definition(evaluator_id)
        version = registry.version(evaluator_id, spec["version"])
        binding = freeze_binding(
            definition,
            version,
            runner_identity=runner_identity,
            composed_of=definition.composed_of,
        )
        payloads.append(binding.to_payload())
    return payloads
