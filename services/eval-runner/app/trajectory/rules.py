"""从冻结轨迹测量工具规则；不采集、不调用 Agent、不判断 Snapshot 资格。"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from .assembler import trajectory_digest
from .schema import Identity, StandardTrajectory, TrajectoryEvidenceRef, _ContractModel

if TYPE_CHECKING:
    from ..evaluator_binding import EvaluatorBinding
    from ..evaluator_results import TypedEvaluationResult
    from .inputs import EvaluationContext

ALGORITHM_VERSION = "tool-rules-1.0"


class _ScopeParams(_ContractModel):
    scope: Literal["ROOT_SUBTREE", "WORKFLOW"] = "ROOT_SUBTREE"
    workflow_scope_id: Identity | None = None

    @model_validator(mode="after")
    def explicit_workflow(self):
        if (self.scope == "WORKFLOW") != (self.workflow_scope_id is not None):
            raise ValueError("TRAJECTORY_RULE_SCOPE_INVALID")
        return self


class _ToolParams(_ScopeParams):
    tool_identity: Identity
    phase: Literal["REQUESTED", "EXECUTED"] = "EXECUTED"


class RequireToolParams(_ToolParams):
    rule: Literal["require_tool"]


class ForbidToolParams(_ToolParams):
    rule: Literal["forbid_tool"]


class MaxToolCallsParams(_ToolParams):
    rule: Literal["max_tool_calls"]
    max_count: Annotated[int, Field(ge=0)]


class ToolOrderParams(_ScopeParams):
    rule: Literal["tool_order"]
    before_tool_identity: Identity
    after_tool_identity: Identity
    mode: Literal["EVERY_AFTER"] = "EVERY_AFTER"
    prerequisite_status: Literal["SUCCEEDED"] = "SUCCEEDED"


TrajectoryRuleParams = Annotated[
    RequireToolParams | ForbidToolParams | MaxToolCallsParams | ToolOrderParams, Field(discriminator="rule")
]
_PARAMS = TypeAdapter(TrajectoryRuleParams)


def parse_rule_params(payload) -> TrajectoryRuleParams:
    try:
        return _PARAMS.validate_python(payload.model_dump() if isinstance(payload, _ScopeParams) else payload)
    except (ValueError, TypeError):
        raise ValueError("TRAJECTORY_RULE_PARAMS_INVALID") from None


def _capabilities(params):
    if params.rule == "tool_order":
        return ["ordering", "tool_executions", "tool_requests"]
    return ["tool_requests" if params.phase == "REQUESTED" else "tool_executions"]


def _binding_matches(binding, params, schema):
    return (schema == "1.3" and binding.scope == "item" and binding.result_type == "boolean"
        and binding.normalization_rule is None
        and binding.evaluator_id == "trajectory." + params.rule and binding.params == params.model_dump(mode="json")
        and binding.input_contract == {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": ALGORITHM_VERSION, "rule_id": binding.evaluator_id,
            "required_capabilities": _capabilities(params)})


def _input(context):
    # 深层容器可变，重新校验及重算摘要，不把消费方修改视为已冻结的制品。
    trajectory = StandardTrajectory.model_validate(context.trajectory.model_dump())
    ref = TrajectoryEvidenceRef.model_validate(context.trajectory_evidence_ref.model_dump())
    if (ref.schema_version != trajectory.schema_version or ref.trajectory_digest != trajectory.provenance.trajectory_digest
        or ref.trajectory_contract_digest != trajectory.provenance.trajectory_contract_digest
        or trajectory_digest(trajectory) != ref.trajectory_digest):
        raise ValueError("TRAJECTORY_RULE_INPUT_INVALID")
    return trajectory, ref


class _CausalIndex:
    """最多 1000 个节点的拓扑 bitset 闭包；不递归，不从 parent/index/time 造边。"""

    def __init__(self, steps):
        nodes = {step.step_id: step for step in steps}
        self.bits = {identity: 1 << index for index, identity in enumerate(sorted(nodes))}
        self.ancestors = dict.fromkeys(nodes, 0)
        self.ready = set()
        successors, incoming, invalid = defaultdict(list), dict.fromkeys(nodes, 0), set()
        for step in steps:
            if step.started_at is None or step.ended_at is None or step.ended_at < step.started_at:
                invalid.add(step.step_id)
            for identity in step.causal_predecessor_ids:
                before = nodes.get(identity)
                if (before is None or step.workflow_scope_id is None or before.workflow_scope_id != step.workflow_scope_id
                    or before.ended_at is None or step.started_at is None or before.ended_at > step.started_at):
                    invalid.add(step.step_id)
                    continue
                successors[identity].append(step.step_id)
                incoming[step.step_id] += 1
        queue = deque(sorted(identity for identity, count in incoming.items() if count == 0 and identity not in invalid))
        while queue:
            identity = queue.popleft()
            self.ready.add(identity)
            for after in successors[identity]:
                self.ancestors[after] |= self.ancestors[identity] | self.bits[identity]
                incoming[after] -= 1
                if incoming[after] == 0 and after not in invalid:
                    queue.append(after)

    def before(self, first, after):
        return (first.step_id in self.ready and after.step_id in self.ready
            and bool(self.ancestors[after.step_id] & self.bits[first.step_id]))


def _order(steps, coverage, params):
    requests = [s for s in steps if s.type == "TOOL" and s.phase == "REQUESTED" and s.tool_identity == params.after_tool_identity]
    actions = [s for s in steps if s.type == "TOOL" and s.phase == "EXECUTED" and s.tool_identity == params.before_tool_identity]
    if not requests:
        return (True, "ALL_B_HAVE_SUCCESSFUL_CAUSAL_PREDECESSOR", []) if coverage["tool_requests"] == "COMPLETE" else (
            None, "CAUSAL_ORDER_UNPROVEN", [])
    index = _CausalIndex(steps)
    violations, unknowns, proofs = [], [], []
    # B 请求全集覆盖用于整规则 PASS；这笔已知 B 的 A 缺席只由执行范围完整性证明。
    closed = coverage["tool_executions"] == "COMPLETE" and coverage["tool_requests"] != "NONE"
    for request in requests:
        candidates = [a for a in actions if request.workflow_scope_id is not None and a.workflow_scope_id == request.workflow_scope_id]
        if request.workflow_scope_id is None or request.step_id not in index.ready or coverage["ordering"] == "NONE":
            unknowns.append(request.step_id)
            continue
        successful = [a for a in candidates if a.status == params.prerequisite_status]
        proven = [a for a in successful if index.before(a, request) and a.ended_at <= request.started_at]
        if proven:
            proofs.extend([min(a.step_id for a in proven), request.step_id])
            continue
        # 无成功 A 是完整范围的负证据；有 A 时必须证明所有成功 A 均因果晚于这笔 B。
        # 并行/仅 wall-clock 较晚不构成负证据，缺覆盖可能遗漏更早 A。
        if (closed and not any(a.status == "UNKNOWN" for a in candidates)
            and all(index.before(request, a) and request.ended_at <= a.started_at for a in successful)):
            violations.append(request.step_id)
        else:
            unknowns.append(request.step_id)
    if violations:
        return False, "PREREQUISITE_UNSATISFIED", violations
    if unknowns or coverage["tool_requests"] != "COMPLETE":
        return None, "CAUSAL_ORDER_UNPROVEN", unknowns
    return True, "ALL_B_HAVE_SUCCESSFUL_CAUSAL_PREDECESSOR", proofs


def evaluate_trajectory_rule(context: EvaluationContext | None, params: TrajectoryRuleParams, *,
                             binding: EvaluatorBinding, manifest_schema_version: str) -> TypedEvaluationResult:
    # 延迟引用既有结果模块，注册函数时不创建 evaluator_binding/evaluators 的循环导入。
    from ..evaluator_results import build_result, failed_result

    def failed(code):
        return failed_result(binding, error_code=code, error_message=code, manifest_schema_version=manifest_schema_version)

    def measured(value, reason, ids=(), *, ref=None, count=None):
        evidence = {"reason_code": reason, "evidence_step_ids": sorted(set(ids)),
            "evidence_ref": ref.model_dump(mode="json") if ref is not None else None}
        if count is not None:
            evidence["observed_count"] = count
        return build_result(binding, value, evidence=evidence, raw_value_present=value is not None,
            manifest_schema_version=manifest_schema_version)

    try:
        params = parse_rule_params(params)
    except ValueError:
        return failed("TRAJECTORY_RULE_PARAMS_INVALID")
    if not _binding_matches(binding, params, manifest_schema_version):
        return failed("TRAJECTORY_RULE_BINDING_MISMATCH")
    if context is None or context.trajectory is None and context.trajectory_evidence_ref is None:
        return measured(None, "TRAJECTORY_RULE_INPUT_UNAVAILABLE")
    try:
        trajectory, ref = _input(context)
    except (ValueError, TypeError, AttributeError):
        return failed("TRAJECTORY_RULE_INPUT_INVALID")
    steps = trajectory.steps
    coverage = dict(trajectory.completeness.capabilities)
    if params.scope == "WORKFLOW":
        # 全局 COMPLETE 不等于所选 workflow 的 membership 已闭合。
        # 无归属的相关工具事件可能属于该范围，只保留已知 Step 数量的下界。
        relevant = {"EXECUTED": params.before_tool_identity, "REQUESTED": params.after_tool_identity} if params.rule == "tool_order" else {
            params.phase: params.tool_identity}
        for step in steps:
            if (step.type == "TOOL" and step.workflow_scope_id is None and step.phase in relevant
                and step.tool_identity in (None, relevant[step.phase])):
                capability = "tool_requests" if step.phase == "REQUESTED" else "tool_executions"
                if coverage[capability] == "COMPLETE":
                    coverage[capability] = "PARTIAL"
        steps = [step for step in steps if step.workflow_scope_id == params.workflow_scope_id]
        if not steps:
            return measured(None, "WORKFLOW_SCOPE_UNAVAILABLE", ref=ref)
    if params.rule == "tool_order":
        value, reason, ids = _order(steps, coverage, params)
        return measured(value, reason, ids, ref=ref)
    matching = [s for s in steps if s.type == "TOOL" and s.phase == params.phase and s.tool_identity == params.tool_identity]
    ids = [step.step_id for step in matching]
    complete = coverage[_capabilities(params)[0]] == "COMPLETE"
    if params.rule == "forbid_tool":
        return measured(False if matching else True if complete else None,
            "TOOL_FORBIDDEN_OBSERVED" if matching else "TOOL_FORBIDDEN_ABSENT" if complete else "TOOL_COVERAGE_INCOMPLETE", ids, ref=ref)
    if params.rule == "max_tool_calls":
        count = len(matching)
        return measured(False if count > params.max_count else True if complete else None,
            "TOOL_CALL_LIMIT_EXCEEDED" if count > params.max_count else "TOOL_CALL_LIMIT_SATISFIED" if complete else "TOOL_COVERAGE_INCOMPLETE",
            ids, ref=ref, count=count)
    accepted = [s for s in matching if params.phase == "REQUESTED" or s.status == "SUCCEEDED"]
    if accepted:
        return measured(True, "TOOL_REQUIRED_OBSERVED", [s.step_id for s in accepted], ref=ref)
    unknown = not complete or any(s.status == "UNKNOWN" for s in matching)
    return measured(None if unknown else False, "TOOL_COVERAGE_INCOMPLETE" if unknown else "TOOL_REQUIRED_MISSING", ref=ref)
