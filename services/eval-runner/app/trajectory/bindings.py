"""轨迹规则的注册、冻结配置和既有 TypedResult 适配；不访问来源服务。"""

import hashlib
import importlib
import inspect
from dataclasses import replace

from . import rules

RULES = {
    "require_tool": ("必需工具", rules.RequireToolParams),
    "forbid_tool": ("禁止工具", rules.ForbidToolParams),
    "max_tool_calls": ("工具调用上限", rules.MaxToolCallsParams),
    "tool_order": ("逐次工具先决条件", rules.ToolOrderParams),
    "tool_argument_match": ("工具参数约束", rules.ToolArgumentMatchParams),
    "required_step_sequence": ("可信因果子序列", rules.RequiredStepSequenceParams),
    "max_same_tool_repeats": ("同身份连续调用上限", rules.MaxSameToolRepeatsParams),
    "require_human_escalation": ("可信人工介入", rules.RequireHumanEscalationParams),
}


def is_tool_rule(value):
    return (getattr(value, "evaluator_id", "") in {"trajectory." + rule for rule in RULES}
        and getattr(value, "input_contract", {}).get("input_kind") == "trajectory")


def requires_human_policy(binding):
    return is_tool_rule(binding) and "human_events" in binding.input_contract.get("required_capabilities", [])


def input_contract(params):
    return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
        "algorithm_version": rules.algorithm_version(params.rule), "rule_id": "trajectory." + params.rule,
        "required_capabilities": rules._capabilities(params)}


def declared_contract(rule):
    if rule == "require_human_escalation":
        return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "human-events-1.0", "rule_id": "trajectory." + rule,
            "required_capabilities": ["human_events"]}
    if rule == "max_same_tool_repeats":
        return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "tool-repeats-1.0", "rule_id": "trajectory." + rule,
            "required_capabilities": ["tool_executions", "tool_arguments", "ordering"]}
    if rule == "required_step_sequence":
        return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "step-sequence-1.0", "rule_id": "trajectory." + rule,
            "required_capabilities": ["ordering", "tool_executions"]}
    if rule == "tool_argument_match":
        return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "tool-arguments-1.0", "rule_id": "trajectory." + rule,
            "required_capabilities": ["tool_requests", "tool_arguments"]}
    return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
        "algorithm_version": rules.ALGORITHM_VERSION, "rule_id": "trajectory." + rule,
        "required_capabilities": ["ordering", "tool_executions", "tool_requests"] if rule == "tool_order" else ["tool_executions"]}


def register_tool_rules(registry):
    from ..evaluators import EvaluatorDefinition, EvaluatorVersion

    for rule, (name, model) in RULES.items():
        identity = "trajectory." + rule
        registry.register_definition(EvaluatorDefinition(id=identity, name=name, display_description="基于冻结 Observation 轨迹的布尔规则",
            definition_source="ARGUS_BUILTIN", execution_owner="ARGUS", default_version="1.0.0", default_selected=False))
        registry.register_version(EvaluatorVersion(evaluator_id=identity, version="1.0.0", result_type="boolean", scope="item",
            threshold=1.0, direction="higher_is_better", critical=False, executor_type="builtin_python",
            implementation_ref=f"builtin:{identity}@1.0.0", input_contract=declared_contract(rule),
            output_contract={"type": "boolean"}, param_schema=model.model_json_schema(), fn=rules.evaluate_trajectory_rule))


def freeze_rule_config(version, payload):
    from ..evaluator_binding import EvaluatorBindingError

    try:
        parsed = rules.parse_rule_params(payload)
        if (version.evaluator_id != "trajectory." + parsed.rule or version.fn is not rules.evaluate_trajectory_rule
            or version.result_type != "boolean" or version.scope != "item"
            or version.input_contract != declared_contract(parsed.rule)
            or version.param_schema != RULES[parsed.rule][1].model_json_schema()):
            raise ValueError
        return parsed.model_dump(mode="json"), input_contract(parsed)
    except (ValueError, TypeError) as error:
        safe = {"TRAJECTORY_JSONPATH_INVALID", "TRAJECTORY_JSONPATH_SEGMENT_LIMIT", "TRAJECTORY_SENSITIVE_MATCH_FIELD",
            "TRAJECTORY_RULE_OPERATOR_UNSUPPORTED", "TRAJECTORY_RULE_LITERAL_INVALID"}
        code = str(error) if str(error) in safe else "TRAJECTORY_RULE_PARAMS_INVALID"
        raise EvaluatorBindingError(code, code=code) from None


def validate_rule_contract(binding, contract):
    """冻结和执行入口共用；只读冻结合同，不从当前配置补充允许路径。"""
    from ..evaluator_binding import EvaluatorBindingError
    from .codec import canonical_loads
    from .privacy import strings_allowed

    if requires_human_policy(binding):
        policy = contract.source_profile.human_authority if contract is not None else None
        try:
            params = rules.parse_rule_params(binding.params)
        except ValueError:
            raise EvaluatorBindingError("TRAJECTORY_RULE_PARAMS_INVALID", code="TRAJECTORY_RULE_PARAMS_INVALID") from None
        if policy is not None and getattr(params, "authority_id", None) is not None and params.authority_id != policy.authority_id:
            raise EvaluatorBindingError("TRAJECTORY_HUMAN_AUTHORITY_MISMATCH", code="TRAJECTORY_HUMAN_AUTHORITY_MISMATCH")
        return
    if binding.evaluator_id not in {"trajectory.tool_argument_match", "trajectory.max_same_tool_repeats"}:
        return
    try:
        params = rules.parse_rule_params(binding.params)
    except (ValueError, TypeError) as error:
        code = str(error) if str(error) in _ERRORS else "TRAJECTORY_RULE_PARAMS_INVALID"
        raise EvaluatorBindingError(code, code=code) from None
    paths = params.identity_paths if params.rule == "max_same_tool_repeats" else [params.path]
    if (contract is None or contract.redaction_policy.content_policy_version != "pii-v1"
        or not set(paths) <= set(contract.redaction_policy.argument_paths)):
        raise EvaluatorBindingError("TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED", code="TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED")
    if params.rule == "tool_argument_match" and not strings_allowed(
        canonical_loads(params.expected_json), (contract.redaction_policy.string_policies or {}).get(params.path)):
        raise EvaluatorBindingError("TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED", code="TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED")


def implementation_dependencies(fn):
    if fn is not rules.evaluate_trajectory_rule:
        return ()
    # dispatcher 外的参数模型、因果算法、digest 和结果 helper 也属于可执行身份。
    modules = ("app.trajectory.bindings", "app.trajectory.rules", "app.trajectory.schema", "app.trajectory.codec",
        "app.trajectory.assembler", "app.trajectory.paths", "app.trajectory.arguments", "app.trajectory.privacy",
        "app.trajectory.redaction", "app.trajectory.repeats", "app.trajectory.human", "app.evaluator_results")
    return tuple("sha256:" + hashlib.sha256(inspect.getsource(importlib.import_module(name)).encode()).hexdigest() for name in modules)


_REASONS = {
    "TRAJECTORY_RULE_INPUT_UNAVAILABLE", "WORKFLOW_SCOPE_UNAVAILABLE", "CAUSAL_ORDER_UNPROVEN",
    "PREREQUISITE_UNSATISFIED", "ALL_B_HAVE_SUCCESSFUL_CAUSAL_PREDECESSOR", "TOOL_FORBIDDEN_OBSERVED",
    "TOOL_FORBIDDEN_ABSENT", "TOOL_COVERAGE_INCOMPLETE", "TOOL_CALL_LIMIT_EXCEEDED", "TOOL_CALL_LIMIT_SATISFIED",
    "TOOL_REQUIRED_OBSERVED", "TOOL_REQUIRED_MISSING",
    "ARGUMENT_CALL_ABSENT", "ARGUMENT_COVERAGE_INCOMPLETE", "ARGUMENT_CONSTRAINT_VIOLATED", "ARGUMENT_CONSTRAINT_SATISFIED",
    "STEP_SEQUENCE_OBSERVED", "STEP_SEQUENCE_ABSENT", "STEP_SEQUENCE_UNPROVEN",
    "TOOL_REPEAT_LIMIT_EXCEEDED", "TOOL_REPEAT_LIMIT_SATISFIED", "TOOL_REPEAT_UNPROVEN",
    "HUMAN_ESCALATION_OBSERVED", "HUMAN_ESCALATION_ABSENT", "HUMAN_COVERAGE_UNPROVEN",
}
_ERRORS = {"TRAJECTORY_RULE_INPUT_INVALID", "TRAJECTORY_RULE_PARAMS_INVALID", "TRAJECTORY_RULE_BINDING_MISMATCH",
    "TRAJECTORY_JSONPATH_INVALID", "TRAJECTORY_JSONPATH_SEGMENT_LIMIT", "TRAJECTORY_SENSITIVE_MATCH_FIELD",
    "TRAJECTORY_RULE_OPERATOR_UNSUPPORTED", "TRAJECTORY_RULE_LITERAL_INVALID"}


def execute_rule(fn, *, context, binding, schema, contract_digest, trajectory_contract=None):
    from ..evaluator_results import failed_result

    trajectory = getattr(context, "trajectory", None)
    if trajectory is not None and trajectory.provenance.trajectory_contract_digest != contract_digest:
        return failed_result(binding, error_code="TRAJECTORY_RULE_INPUT_INVALID", error_message="TRAJECTORY_RULE_INPUT_INVALID",
            manifest_schema_version=schema)
    kwargs = {}
    if requires_human_policy(binding):
        if trajectory_contract is None or trajectory_contract.digest != contract_digest:
            return failed_result(binding, error_code="TRAJECTORY_RULE_INPUT_INVALID", error_message="TRAJECTORY_RULE_INPUT_INVALID",
                manifest_schema_version=schema)
        kwargs["authority_policy"] = trajectory_contract.source_profile.human_authority
    return fn(context, binding.params, binding=binding, manifest_schema_version=schema, **kwargs)


def adapt_result(raw, *, binding, context, schema, duration_ms):
    from ..evaluator_results import RESULT_ERROR_CODES, ResultProvenance, TypedEvaluationResult, failed_result

    def invalid():
        return failed_result(binding, error_code="TRAJECTORY_RULE_RESULT_INVALID", error_message="TRAJECTORY_RULE_RESULT_INVALID",
            duration_ms=duration_ms, manifest_schema_version=schema)

    if (not isinstance(raw, TypedEvaluationResult) or raw.provenance != ResultProvenance.from_binding(binding, manifest_schema_version=schema)
        or raw.evaluator_id != binding.evaluator_id or raw.result_type != "boolean" or raw.normalized_value is not None
        or raw.normalization_rule is not None or raw.comment is not None):
        return invalid()
    if raw.status == "failed":
        if raw.value is not None or raw.error_code not in _ERRORS or raw.error_message != raw.error_code or raw.evidence is not None:
            return invalid()
        return replace(raw, duration_ms=duration_ms)
    if raw.status not in ("succeeded", "no_result") or (raw.status == "succeeded" and type(raw.value) is not bool):
        return invalid()
    if raw.status == "no_result" and (raw.value is not None or raw.error_code != "EVALUATION_VALUE_MISSING"):
        return invalid()
    if (raw.status == "succeeded" and (raw.error_code is not None or raw.error_message is not None)
        or raw.status == "no_result" and raw.error_message != RESULT_ERROR_CODES["EVALUATION_VALUE_MISSING"]):
        return invalid()
    evidence = raw.evidence
    keys = {"reason_code", "evidence_step_ids", "evidence_ref"}
    if binding.params.get("rule") == "max_tool_calls":
        keys.add("observed_count")
    if binding.params.get("rule") == "max_same_tool_repeats":
        keys.add("observed_repeat_count")
    # 无输入/未知工作流早退没有计数；其余计数结果必须包含合法下界。
    if type(evidence) is not dict or set(evidence) not in (keys, {"reason_code", "evidence_step_ids", "evidence_ref"}):
        return invalid()
    ids = evidence.get("evidence_step_ids")
    if (evidence.get("reason_code") not in _REASONS or type(ids) is not list
        or any(type(i) is not str for i in ids) or ids != sorted(set(ids))):
        return invalid()
    ref = getattr(context, "trajectory_evidence_ref", None)
    trajectory = getattr(context, "trajectory", None)
    if evidence["evidence_ref"] != (ref.model_dump(mode="json") if ref is not None else None):
        return invalid()
    known_ids = {step.step_id for step in trajectory.steps} if trajectory is not None else set()
    if not set(ids) <= known_ids:
        return invalid()
    if "observed_count" in evidence and (type(evidence["observed_count"]) is not int or evidence["observed_count"] < 0):
        return invalid()
    if "observed_repeat_count" in evidence and (type(evidence["observed_repeat_count"]) is not int or evidence["observed_repeat_count"] < 0):
        return invalid()
    return replace(raw, duration_ms=duration_ms)
