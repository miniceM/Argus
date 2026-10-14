"""冻结 Judge 注册及准备结果桥接；同步执行器不访问 Provider。"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field, replace
from decimal import Decimal

from ..evaluator_binding import BuiltinPythonExecutor, EvaluatorBindingError
from .codec import canonical_dumps, canonical_loads
from .judge import FrozenJudgeDefinition, ParsedJudgeResult
from .judge_input import (
    adjudicate_judge_criteria,
    build_judge_request,
    validate_prepared_judge_request,
)

_SIGNING_KEY = secrets.token_bytes(32)
_REF = "trajectory_judge:1.0"
_PARAM_SCHEMA = {"type": "object", "properties": {"judge_definition_json": {"type": "string"}},
    "required": ["judge_definition_json"], "additionalProperties": False}
_OUTPUT = {"type": "string", "enum": ["PASS", "FAIL"]}
_FAILURES = frozenset({
    "JUDGE_OUTPUT_INVALID", "JUDGE_TIMEOUT", "JUDGE_PROVIDER_RESPONSE_INVALID", "JUDGE_PROVIDER_USAGE_INVALID",
    "JUDGE_PROVIDER_CONFIGURATION_INVALID", "JUDGE_PROVIDER_IDENTITY_MISMATCH", "JUDGE_PROVIDER_HTTP_ERROR",
    "JUDGE_PROVIDER_RESPONSE_TOO_LARGE", "JUDGE_MODEL_IDENTITY_MISMATCH", "JUDGE_PROVIDER_NETWORK_ERROR",
    "JUDGE_PROVIDER_CAPABILITY_UNAVAILABLE",
    "JUDGE_PREPARED_REQUEST_INVALID", "JUDGE_PROVIDER_ERROR", "JUDGE_PROVIDER_RESULT_IDENTITY_MISMATCH",
    "JUDGE_BUDGET_UNAVAILABLE", "JUDGE_BUDGET_STATE_INVALID", "JUDGE_BUDGET_DIALECT_UNSUPPORTED",
    "JUDGE_BUDGET_SCOPE_INVALID", "JUDGE_BUDGET_CANCELLED", "JUDGE_BUDGET_LEASE_LOST",
    "JUDGE_BUDGET_BINDING_MISMATCH", "JUDGE_CALL_IDENTITY_MISMATCH", "JUDGE_CALL_OUTCOME_UNKNOWN",
    "JUDGE_CALL_ALREADY_RESERVED", "JUDGE_BUDGET_EXHAUSTED", "JUDGE_RESERVATION_STALE", "JUDGE_CALL_NOT_SENT",
    "JUDGE_USAGE_BOUND_EXCEEDED", "JUDGE_USAGE_CONFLICT", "JUDGE_BUDGET_STORAGE_UNAVAILABLE",
})
_NO_RESULTS = frozenset({"JUDGE_UNCERTAIN", "JUDGE_LOW_CONFIDENCE", "JUDGE_EVIDENCE_REF_INVALID"})


def _invalid(code="JUDGE_PREPARED_RESULT_INVALID"):
    raise EvaluatorBindingError(code, code=code) from None


def _input_contract(definition):
    return {"input_kind": "trajectory", "trajectory_schema_version": "1.0", "judge_kind": definition.judge_kind,
        "judge_definition_digest": definition.definition_digest, "required_capabilities": list(definition.required_capabilities)}


def judge_implementation_artifact():
    from ..evaluator_binding import python_source_artifact
    return python_source_artifact(execute_prepared_judge_result, _REF)


def _definition(payload):
    try:
        if type(payload) is not dict or set(payload) != {"judge_definition_json"}:
            raise ValueError
        text = payload["judge_definition_json"]
        data = canonical_loads(text, max_bytes=64 * 1024)
        # JSON 数值 0/1 经 codec 读为 int；只在声明的 Decimal 字段转为精确 Decimal。
        # bool/string 均不转换，也不经过 Pydantic 的 float JSON 解码。
        paths = [(name,) for name in ("temperature", "top_p", "request_timeout_seconds",
            "trigger_confidence_threshold", "sample_rate")]
        paths += [("calibration_policy", name) for name in ("max_false_accept_rate", "confidence_level", "max_disagreement_rate")]
        paths += [("output_policy", "min_confidence")]
        for path in paths:
            target = data
            for key in path[:-1]:
                target = target[key]
            if type(target[path[-1]]) is int:
                target[path[-1]] = Decimal(target[path[-1]])
        value = FrozenJudgeDefinition.model_validate(data)
        if text != canonical_dumps(value):
            raise ValueError
        if value.implementation_ref != _REF or value.implementation_artifact_digest != judge_implementation_artifact().digest:
            _invalid("EVALUATOR_ARTIFACT_DIGEST_MISMATCH")
        return value
    except EvaluatorBindingError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError):
        _invalid("JUDGE_BINDING_INVALID")


def register_judge_version(registry, *, evaluator_id, definition):
    """显式注册一个不可变测量版本；不默认选中，也不宣称完成 Provider 准入。"""
    from pydantic import TypeAdapter

    from ..evaluators import EvaluatorDefinition, EvaluatorVersion
    from .schema import Identity

    try:
        TypeAdapter(Identity).validate_python(evaluator_id, strict=True)
        definition = FrozenJudgeDefinition.model_validate(definition.model_dump())
    except (ValueError, TypeError, AttributeError):
        _invalid("JUDGE_BINDING_INVALID")
    _definition({"judge_definition_json": canonical_dumps(definition)})
    version = EvaluatorVersion(evaluator_id=evaluator_id, version=definition.version,
        result_type="categorical", scope="item", threshold=1.0, direction="higher_is_better", critical=False,
        implementation_ref=_REF, executor_type="trajectory_judge", input_contract=_input_contract(definition),
        output_contract=dict(_OUTPUT), param_schema=dict(_PARAM_SCHEMA), category_values=("PASS", "FAIL"),
        fn=execute_prepared_judge_result)
    # 不可变版本冲突发生前不修改现有 Definition 的显示/默认版本。
    if registry.has_definition(evaluator_id):
        return registry.register_version(version)
    display = EvaluatorDefinition(id=evaluator_id, name=evaluator_id, display_description="基于冻结证据的受治理轨迹 Judge",
        definition_source="ARGUS_BUILTIN", execution_owner="ARGUS", default_version=definition.version, default_selected=False)
    registry.register_definition(display)
    return registry.register_version(version)


def freeze_judge_config(version, payload):
    value = _definition(payload)
    if (version.executor_type != "trajectory_judge" or version.fn is not execute_prepared_judge_result
        or version.version != value.version or version.result_type != "categorical" or version.scope != "item"
        or version.input_contract != _input_contract(value) or version.output_contract != _OUTPUT
        or version.param_schema != _PARAM_SCHEMA or version.category_values != ("PASS", "FAIL")
        or version.ordered_category_values is not None):
        _invalid("JUDGE_BINDING_INVALID")
    if version.critical:
        _invalid("CRITICAL_JUDGE_SINGLE_SOURCE")
    return {"judge_definition_json": canonical_dumps(value)}, _input_contract(value)


def validate_judge_contract(binding, contract, manifest_schema_version):
    if binding.executor_type != "trajectory_judge":
        return
    if (manifest_schema_version != "1.3" or contract is None
        or contract.redaction_policy.content_policy_version != "pii-v1"):
        _invalid("JUDGE_INPUT_CONTRACT_UNAVAILABLE")


def _verified_binding(binding):
    """异步请求前也重验完整 Binding，不能只凭同一 Judge params 领取预算。"""
    from ..evaluator_binding import freeze_binding
    from ..evaluators import EvaluatorRegistry

    value = _definition(binding.params)
    registry = EvaluatorRegistry(include_builtins=False)
    version = register_judge_version(registry, evaluator_id=binding.evaluator_id, definition=value)
    expected = freeze_binding(registry.definition(binding.evaluator_id), version,
        runner_identity=binding.runner, composed_of=binding.composed_of, params=binding.params)
    if binding.critical:
        _invalid("CRITICAL_JUDGE_SINGLE_SOURCE")
    if (binding.binding_id != expected.binding_id or binding.binding_digest != expected.binding_digest
        or binding.release_eligible != expected.release_eligible or binding.eligibility_reasons != expected.eligibility_reasons):
        _invalid("JUDGE_BINDING_INVALID")
    return value


def judge_policy_mode(binding):
    """策略冻结只读取经过完整 Binding 校验的模式，不接受旁路配置。"""
    return _verified_binding(binding).mode


@dataclass(frozen=True)
class PreparedJudgeEvaluation:
    binding_digest: str
    scope: object
    evidence_ref: object
    input_digest: str
    criteria_digest: str
    judge_definition_digest: str
    evaluation_generation: int
    result: object
    _seal: str = field(default="", repr=False, compare=False)


def _seal(value):
    proof = canonical_dumps({"schema_version": "prepared-judge-result-v1", "binding_digest": value.binding_digest,
        "scope": value.scope.model_dump(), "evidence_ref": value.evidence_ref.model_dump(),
        "input_digest": value.input_digest, "criteria_digest": value.criteria_digest,
        "judge_definition_digest": value.judge_definition_digest, "evaluation_generation": value.evaluation_generation,
        "result": value.result.to_payload()})
    return hmac.new(_SIGNING_KEY, proof.encode(), hashlib.sha256).hexdigest()


def _typed_outcome(binding, request, definition, outcome, schema):
    from ..evaluator_results import build_result, failed_result
    from .judge_provider import JudgeProviderOutcome, JudgeUsage

    if type(outcome) is not JudgeProviderOutcome or type(outcome.result) is not ParsedJudgeResult:
        _invalid()
    result = outcome.result
    if (type(outcome.duration_ms) is not int or not 0 <= outcome.duration_ms <= 600000
        or type(result.evidence_step_ids) is not tuple or len(set(result.evidence_step_ids)) != len(result.evidence_step_ids)
        or any(type(step) is not str for step in result.evidence_step_ids)
        or not set(result.evidence_step_ids) <= request.local_step_ids
        or len(result.evidence_step_ids) > definition.output_policy.max_evidence_refs):
        _invalid()
    if result.confidence is not None and (type(result.confidence) is not Decimal or not result.confidence.is_finite()
        or not Decimal(0) <= result.confidence <= Decimal(1)):
        _invalid()
    if result.status == "succeeded":
        if (result.value not in ("PASS", "FAIL") or result.reason_code not in definition.output_policy.reason_codes
            or result.confidence is None or result.confidence < definition.output_policy.min_confidence
            or not result.evidence_step_ids and not definition.output_policy.allow_empty_evidence):
            _invalid()
    elif result.status == "no_result":
        if (result.value is not None or result.reason_code not in _NO_RESULTS
            or result.reason_code == "JUDGE_LOW_CONFIDENCE" and (result.confidence is None
                or result.confidence >= definition.output_policy.min_confidence)):
            _invalid()
    elif result.status == "failed":
        if result.value is not None or result.reason_code not in _FAILURES or result.confidence is not None or result.evidence_step_ids:
            _invalid()
    else:
        _invalid()
    if ((outcome.input_digest, outcome.definition_digest) != (request.input_digest, definition.definition_digest)
        and not (result.status == "failed" and outcome.input_digest is None and outcome.definition_digest is None)):
        _invalid()
    usage = outcome.usage
    if usage is not None and (type(usage) is not JudgeUsage or type(usage.input_tokens) is not int
        or type(usage.output_tokens) is not int or not 0 <= usage.input_tokens <= definition.max_total_input_tokens
        or not 0 <= usage.output_tokens <= definition.max_output_tokens):
        _invalid()
    evidence = {"reason_code": result.reason_code, "evidence_step_ids": sorted(result.evidence_step_ids),
        "evidence_ref": request.evidence_ref.model_dump(mode="json"), "input_digest": request.input_digest,
        "criteria_digest": request.criteria_digest, "judge_definition_digest": definition.definition_digest,
        "confidence_json": canonical_dumps(result.confidence) if result.confidence is not None else None,
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens} if usage is not None else None}
    if result.status == "failed":
        row = failed_result(binding, error_code=result.reason_code, error_message=result.reason_code,
            duration_ms=outcome.duration_ms, manifest_schema_version=schema)
        return replace(row, evidence=evidence)
    return build_result(binding, result.value, evidence=evidence, duration_ms=outcome.duration_ms,
        manifest_schema_version=schema, raw_value_present=result.status == "succeeded")


async def prepare_judge_evaluation(binding, *, request, provider, evaluation_generation, owner, manifest_schema_version):
    """实际异步桥接：来源 seal → 持久预算 Adapter → 重验响应 → 签发消费凭证。"""
    from .judge_budget import BudgetedJudgeProvider

    definition = _verified_binding(binding)
    if (type(provider) is not BudgetedJudgeProvider or manifest_schema_version != "1.3"
        or type(evaluation_generation) is not int or evaluation_generation < 0):
        _invalid()
    validate_prepared_judge_request(request, definition)
    outcome = await provider.evaluate(request, definition, binding_id=binding.binding_id,
        evaluation_generation=evaluation_generation, owner=owner)
    row = _typed_outcome(binding, request, definition, outcome, manifest_schema_version)
    result = PreparedJudgeEvaluation(binding.binding_digest, request.scope, request.evidence_ref, request.input_digest,
        request.criteria_digest, definition.definition_digest, evaluation_generation, row)
    return replace(result, _seal=_seal(result))


def execute_prepared_judge_result(*, binding=None, context=None, trajectory_contract=None, prepared_result=None,
    evaluation_generation=None, **_):
    """同步实现的可校验制品；直接/缺输入调用只会失败，不会访问网络。"""
    from ..evaluator_results import ResultProvenance, TypedEvaluationResult, failed_result
    from .schema import TrajectoryEvidenceRef, TrajectoryScope

    if binding is None:
        _invalid("JUDGE_PREPARED_RESULT_UNAVAILABLE")
    schema = "1.3"
    def failure(code):
        return failed_result(binding, error_code=code, error_message=code, manifest_schema_version=schema)
    if prepared_result is None:
        return failure("JUDGE_PREPARED_RESULT_UNAVAILABLE")
    try:
        value = prepared_result
        definition = _definition(binding.params)
        if (type(value) is not PreparedJudgeEvaluation or type(value.scope) is not TrajectoryScope
            or type(value.evidence_ref) is not TrajectoryEvidenceRef or type(value.result) is not TypedEvaluationResult
            or type(value._seal) is not str or len(value._seal) != 64
            or not hmac.compare_digest(value._seal, _seal(value)) or value.binding_digest != binding.binding_digest
            or type(evaluation_generation) is not int or evaluation_generation != value.evaluation_generation
            or value.result.provenance != ResultProvenance.from_binding(binding, manifest_schema_version=schema)
            or value.result.evaluator_id != binding.evaluator_id or value.result.result_type != "categorical"):
            _invalid()
        approved = adjudicate_judge_criteria(context.expected_output, policy=definition.criteria_policy,
            scope=value.scope, limits=trajectory_contract.limits)
        rebuilt = build_judge_request(context, definition, contract=trajectory_contract, expected_scope=value.scope,
            approved_criteria=approved)
        if (rebuilt.input_digest != value.input_digest or rebuilt.criteria_digest != value.criteria_digest
            or rebuilt.evidence_ref != value.evidence_ref or rebuilt.definition_digest != value.judge_definition_digest):
            _invalid()
        return value.result
    except Exception:  # noqa: BLE001 - 封套字段的任意异常也只能形成受控 UNKNOWN
        return failure("JUDGE_PREPARED_RESULT_INVALID")


def adapt_prepared_judge_result(raw, **inputs):
    """统一入口再次验证消费凭证，执行器替换不能绕过准备结果的固定身份。"""
    from ..evaluator_results import TypedEvaluationResult, failed_result

    expected = execute_prepared_judge_result(**inputs)
    if type(raw) is not TypedEvaluationResult or raw != expected:
        return failed_result(inputs["binding"], error_code="JUDGE_PREPARED_RESULT_INVALID",
            error_message="JUDGE_PREPARED_RESULT_INVALID", manifest_schema_version="1.3")
    return expected


class TrajectoryJudgeExecutor(BuiltinPythonExecutor):
    executor_type = "trajectory_judge"

    def validate(self, binding, implementation):
        code = super().validate(binding, implementation)
        if code:
            return code
        try:
            frozen_params, frozen_input = freeze_judge_config(implementation, binding.params)
            from ..evaluator_binding import freeze_binding
            expected = freeze_binding(self.registry.definition(binding.evaluator_id), implementation,
                runner_identity=binding.runner, composed_of=binding.composed_of, params=frozen_params)
            if binding.critical:
                return "CRITICAL_JUDGE_SINGLE_SOURCE"
            if (binding.binding_id != expected.binding_id or binding.input_contract != frozen_input
                or binding.output_contract != expected.output_contract or binding.result_type != "categorical"
                or binding.scope != "item" or binding.params != frozen_params or binding.category_values != ("PASS", "FAIL")
                or binding.normalization_rule is not None or binding.contract_status != expected.contract_status
                or binding.release_eligible != expected.release_eligible or binding.eligibility_reasons != expected.eligibility_reasons):
                return "JUDGE_BINDING_INVALID"
        except EvaluatorBindingError as error:
            return error.code
        return None

    def execute(self, implementation, *, output, expected_output, **inputs):
        return execute_prepared_judge_result(**inputs)
