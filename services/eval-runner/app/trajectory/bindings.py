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
}


def is_tool_rule(value):
    return (getattr(value, "evaluator_id", "") in {"trajectory." + rule for rule in RULES}
        and getattr(value, "input_contract", {}).get("input_kind") == "trajectory")


def input_contract(params):
    return {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
        "algorithm_version": rules.ALGORITHM_VERSION, "rule_id": "trajectory." + params.rule,
        "required_capabilities": rules._capabilities(params)}


def declared_contract(rule):
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
    except (ValueError, TypeError):
        raise EvaluatorBindingError("TRAJECTORY_RULE_PARAMS_INVALID", code="TRAJECTORY_RULE_PARAMS_INVALID") from None


def implementation_dependencies(fn):
    if fn is not rules.evaluate_trajectory_rule:
        return ()
    # dispatcher 外的参数模型、因果算法、digest 和结果 helper 也属于可执行身份。
    modules = ("app.trajectory.bindings", "app.trajectory.rules", "app.trajectory.schema", "app.trajectory.codec",
        "app.trajectory.assembler", "app.trajectory.paths", "app.evaluator_results")
    return tuple("sha256:" + hashlib.sha256(inspect.getsource(importlib.import_module(name)).encode()).hexdigest() for name in modules)


_REASONS = {
    "TRAJECTORY_RULE_INPUT_UNAVAILABLE", "WORKFLOW_SCOPE_UNAVAILABLE", "CAUSAL_ORDER_UNPROVEN",
    "PREREQUISITE_UNSATISFIED", "ALL_B_HAVE_SUCCESSFUL_CAUSAL_PREDECESSOR", "TOOL_FORBIDDEN_OBSERVED",
    "TOOL_FORBIDDEN_ABSENT", "TOOL_COVERAGE_INCOMPLETE", "TOOL_CALL_LIMIT_EXCEEDED", "TOOL_CALL_LIMIT_SATISFIED",
    "TOOL_REQUIRED_OBSERVED", "TOOL_REQUIRED_MISSING",
}
_ERRORS = {"TRAJECTORY_RULE_INPUT_INVALID", "TRAJECTORY_RULE_PARAMS_INVALID", "TRAJECTORY_RULE_BINDING_MISMATCH"}


def execute_rule(fn, *, context, binding, schema, contract_digest):
    from ..evaluator_results import failed_result

    trajectory = getattr(context, "trajectory", None)
    if trajectory is not None and trajectory.provenance.trajectory_contract_digest != contract_digest:
        return failed_result(binding, error_code="TRAJECTORY_RULE_INPUT_INVALID", error_message="TRAJECTORY_RULE_INPUT_INVALID",
            manifest_schema_version=schema)
    return fn(context, binding.params, binding=binding, manifest_schema_version=schema)


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
    return replace(raw, duration_ms=duration_ms)
