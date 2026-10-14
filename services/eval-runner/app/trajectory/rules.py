"""从冻结轨迹测量工具规则；不采集、不调用 Agent、不判断 Snapshot 资格。"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, PrivateAttr, TypeAdapter, ValidationError, model_validator

from .assembler import trajectory_digest
from .human import AuthorityId
from .schema import Identity, StandardTrajectory, StepPhase, StepType, TrajectoryEvidenceRef, _ContractModel

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


class ToolArgumentMatchParams(_ToolParams):
    rule: Literal["tool_argument_match"]
    phase: Literal["REQUESTED", "EXECUTED"] = "REQUESTED"
    path: str = Field(max_length=4096)
    operator: str = Field(json_schema_extra={"enum": ["eq", "ne", "one_of", "gt", "gte", "lt", "lte", "contains"]})
    expected_json: str = Field(max_length=4096, description="精确 JSON scalar；one_of 使用同类型 scalar 数组")
    require_call: bool = True
    _path_tokens: tuple = PrivateAttr(default=())

    @property
    def path_tokens(self):
        return self._path_tokens

    @model_validator(mode="after")
    def governed_parameters(self):
        from .arguments import json_kind
        from .codec import canonical_dumps, canonical_loads
        from .paths import compile_jsonpath
        from .privacy import sensitive_key, sensitive_value
        self._path_tokens = compile_jsonpath(self.path)
        if any(type(token) is str and sensitive_key(token) for token in self._path_tokens):
            raise ValueError("TRAJECTORY_SENSITIVE_MATCH_FIELD")
        if self.operator not in {"eq", "ne", "one_of", "gt", "gte", "lt", "lte", "contains"}:
            raise ValueError("TRAJECTORY_RULE_OPERATOR_UNSUPPORTED")
        try:
            literal = canonical_loads(self.expected_json, max_bytes=4096)
            if self.operator == "one_of":
                if (type(literal) is not list or not literal or any(json_kind(value) is None for value in literal)
                    or len({json_kind(value) for value in literal}) != 1):
                    raise ValueError
            elif json_kind(literal) is None:
                raise ValueError
            if self.operator in {"gt", "gte", "lt", "lte"} and json_kind(literal) != "number":
                raise ValueError
        except ValueError:
            raise ValueError("TRAJECTORY_RULE_LITERAL_INVALID") from None
        if sensitive_value(literal):
            raise ValueError("TRAJECTORY_SENSITIVE_MATCH_FIELD")
        object.__setattr__(self, "expected_json", canonical_dumps(literal, max_bytes=4096))
        return self


class MaxSameToolRepeatsParams(_ToolParams):
    rule: Literal["max_same_tool_repeats"]
    max_repeats: Annotated[int, Field(ge=0)]
    identity_paths: list[str] = Field(min_length=1, max_length=32)
    _identity_tokens: tuple = PrivateAttr(default=())

    @property
    def identity_tokens(self):
        return self._identity_tokens

    @model_validator(mode="after")
    def governed_identity(self):
        from .paths import compile_jsonpath
        from .privacy import sensitive_key
        if len(set(self.identity_paths)) != len(self.identity_paths):
            raise ValueError("TRAJECTORY_RULE_PARAMS_INVALID")
        paths = sorted(self.identity_paths)
        tokens = tuple(compile_jsonpath(path) for path in paths)
        if any(type(token) is str and sensitive_key(token) for path in tokens for token in path):
            raise ValueError("TRAJECTORY_SENSITIVE_MATCH_FIELD")
        object.__setattr__(self, "identity_paths", paths)
        self._identity_tokens = tokens
        return self


class StepDescriptor(_ContractModel):
    type: StepType
    tool_identity: Identity | None = None
    phase: StepPhase

    @model_validator(mode="after")
    def explicit_tool(self):
        if (self.type == "TOOL") != (self.tool_identity is not None) or (
            self.type == "TOOL" and self.phase not in {"REQUESTED", "EXECUTED"}):
            raise ValueError("TRAJECTORY_RULE_PARAMS_INVALID")
        return self


class RequiredStepSequenceParams(_ScopeParams):
    rule: Literal["required_step_sequence"]
    steps: list[StepDescriptor] = Field(min_length=1, max_length=32)


class RequireHumanEscalationParams(_ScopeParams):
    rule: Literal["require_human_escalation"]
    authority_id: AuthorityId | None = None


TrajectoryRuleParams = Annotated[
    RequireToolParams | ForbidToolParams | MaxToolCallsParams | ToolOrderParams | ToolArgumentMatchParams | RequiredStepSequenceParams | MaxSameToolRepeatsParams | RequireHumanEscalationParams,
    Field(discriminator="rule")
]
_PARAMS = TypeAdapter(TrajectoryRuleParams)


def parse_rule_params(payload) -> TrajectoryRuleParams:
    try:
        return _PARAMS.validate_python(payload.model_dump() if isinstance(payload, _ScopeParams) else payload)
    except ValidationError as error:
        safe = {"TRAJECTORY_JSONPATH_INVALID", "TRAJECTORY_JSONPATH_SEGMENT_LIMIT", "TRAJECTORY_SENSITIVE_MATCH_FIELD",
            "TRAJECTORY_RULE_OPERATOR_UNSUPPORTED", "TRAJECTORY_RULE_LITERAL_INVALID"}
        codes = [str(entry.get("ctx", {}).get("error", "")) for entry in error.errors(include_input=False)]
        raise ValueError(next((code for code in codes if code in safe), "TRAJECTORY_RULE_PARAMS_INVALID")) from None
    except (ValueError, TypeError):
        raise ValueError("TRAJECTORY_RULE_PARAMS_INVALID") from None


def _capabilities(params):
    if params.rule == "require_human_escalation":
        return ["human_events"]
    if params.rule == "max_same_tool_repeats":
        return ["tool_requests" if params.phase == "REQUESTED" else "tool_executions", "tool_arguments", "ordering"]
    if params.rule == "required_step_sequence":
        required = set()
        for descriptor in params.steps:
            if descriptor.type == "TOOL":
                required.add("tool_requests" if descriptor.phase == "REQUESTED" else "tool_executions")
            elif descriptor.type == "HUMAN":
                required.add("human_events")
        return ["ordering", *sorted(required)]
    if params.rule == "tool_order":
        return ["ordering", "tool_executions", "tool_requests"]
    if params.rule == "tool_argument_match":
        return ["tool_requests" if params.phase == "REQUESTED" else "tool_executions", "tool_arguments"]
    return ["tool_requests" if params.phase == "REQUESTED" else "tool_executions"]


def algorithm_version(rule):
    return {"tool_argument_match": "tool-arguments-1.0", "required_step_sequence": "step-sequence-1.0",
        "max_same_tool_repeats": "tool-repeats-1.0", "require_human_escalation": "human-events-1.0"}.get(rule, ALGORITHM_VERSION)


def _binding_matches(binding, params, schema):
    return (schema == "1.3" and binding.scope == "item" and binding.result_type == "boolean"
        and binding.normalization_rule is None
        and binding.evaluator_id == "trajectory." + params.rule and binding.params == params.model_dump(mode="json")
        and binding.input_contract == {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": algorithm_version(params.rule), "rule_id": binding.evaluator_id,
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


def _matches_descriptor(step, descriptor):
    return (step.type == descriptor.type and step.phase == descriptor.phase
        and step.tool_identity == descriptor.tool_identity)


def _sequence(steps, coverage, params):
    candidates = [[step for step in steps if _matches_descriptor(step, descriptor)] for descriptor in params.steps]
    if coverage["ordering"] == "NONE":
        return None, "STEP_SEQUENCE_UNPROVEN", []
    index = _CausalIndex(steps)
    # 每层仅保存确定性的一条证明；可信严格因果链不能复用同一节点。
    paths = {step.step_id: (step.step_id,) for step in candidates[0]
        if step.workflow_scope_id is not None and step.step_id in index.ready}
    for layer in candidates[1:]:
        reachable = sum(index.bits[identity] for identity in paths)
        following = {}
        for step in layer:
            if step.step_id not in index.ready or step.workflow_scope_id is None:
                continue
            predecessors = index.ancestors[step.step_id] & reachable
            if predecessors:
                previous = min(identity for identity in paths if index.bits[identity] & predecessors)
                following[step.step_id] = (*paths[previous], step.step_id)
        paths = following
    if paths:
        return True, "STEP_SEQUENCE_OBSERVED", list(paths[min(paths)])
    if any(coverage[capability] != "COMPLETE" for capability in _capabilities(params)):
        return None, "STEP_SEQUENCE_UNPROVEN", []
    relevant = {step.step_id: step for layer in candidates for step in layer}
    if any(step.workflow_scope_id is None or identity not in index.ready for identity, step in relevant.items()):
        return None, "STEP_SEQUENCE_UNPROVEN", sorted(relevant)
    # 完整覆盖只在候选不可能形成所需序列时证明缺席。
    # 无边的候选可有多种拓扑顺序，保守保留 UNKNOWN，绝不按时间猜测。
    workflows = {step.workflow_scope_id for step in relevant.values()}
    for workflow in sorted(workflows):
        layers = [[step for step in layer if step.workflow_scope_id == workflow] for layer in candidates]
        if any(not layer for layer in layers):
            continue
        multiplicities = defaultdict(int)
        for descriptor in params.steps:
            multiplicities[(descriptor.type, descriptor.phase, descriptor.tool_identity)] += 1
        if any(sum(_matches_descriptor(step, descriptor) and step.workflow_scope_id == workflow for step in steps)
            < multiplicities[(descriptor.type, descriptor.phase, descriptor.tool_identity)] for descriptor in params.steps):
            continue
        possible = {step.step_id for step in layers[0]}
        for layer in layers[1:]:
            possible = {step.step_id for step in layer if any(identity != step.step_id
                and not index.before(step, relevant[identity]) for identity in possible)}
        if possible:
            return None, "STEP_SEQUENCE_UNPROVEN", sorted(relevant)
    return False, "STEP_SEQUENCE_ABSENT", sorted(relevant)


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
                             binding: EvaluatorBinding, manifest_schema_version: str, authority_policy=None) -> TypedEvaluationResult:
    # 延迟引用既有结果模块，注册函数时不创建 evaluator_binding/evaluators 的循环导入。
    from ..evaluator_results import build_result, failed_result

    def failed(code):
        return failed_result(binding, error_code=code, error_message=code, manifest_schema_version=manifest_schema_version)

    def measured(value, reason, ids=(), *, ref=None, count=None, repeat_count=None):
        evidence = {"reason_code": reason, "evidence_step_ids": sorted(set(ids)),
            "evidence_ref": ref.model_dump(mode="json") if ref is not None else None}
        if count is not None:
            evidence["observed_count"] = count
        if repeat_count is not None:
            evidence["observed_repeat_count"] = repeat_count
        return build_result(binding, value, evidence=evidence, raw_value_present=value is not None,
            manifest_schema_version=manifest_schema_version)

    try:
        params = parse_rule_params(params)
    except ValueError as error:
        return failed(str(error))
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
        if params.rule == "require_human_escalation":
            if any(step.type == "HUMAN" and step.workflow_scope_id is None for step in steps):
                coverage["human_events"] = "PARTIAL" if coverage["human_events"] != "NONE" else "NONE"
            relevant = {}
        elif params.rule == "required_step_sequence":
            for step in steps:
                if step.workflow_scope_id is None and any(_matches_descriptor(step, descriptor) for descriptor in params.steps):
                    for capability in _capabilities(params):
                        if coverage[capability] == "COMPLETE":
                            coverage[capability] = "PARTIAL"
            relevant = {}
        else:
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
    if params.rule == "require_human_escalation":
        from .human import closed_human_workflows
        if authority_policy is None or coverage["human_events"] == "NONE" or (
            params.authority_id is not None and params.authority_id != authority_policy.authority_id):
            return measured(None, "HUMAN_COVERAGE_UNPROVEN", ref=ref)
        events, closed, invalid = closed_human_workflows(steps, trajectory.scope, authority_policy)
        ids = [identity for group in events.values() for identity in group.values()]
        if ids:
            return measured(True, "HUMAN_ESCALATION_OBSERVED", ids, ref=ref)
        workflows = {step.workflow_scope_id for step in steps if step.workflow_scope_id is not None}
        if coverage["human_events"] == "COMPLETE" and workflows and workflows <= closed and not invalid:
            return measured(False, "HUMAN_ESCALATION_ABSENT", ref=ref)
        return measured(None, "HUMAN_COVERAGE_UNPROVEN", ref=ref)
    if params.rule == "tool_order":
        value, reason, ids = _order(steps, coverage, params)
        return measured(value, reason, ids, ref=ref)
    if params.rule == "required_step_sequence":
        if any(descriptor.type == "HUMAN" for descriptor in params.steps):
            from .human import human_evidence
            if authority_policy is None or coverage["human_events"] == "NONE":
                return measured(None, "HUMAN_COVERAGE_UNPROVEN", ref=ref)
            events, _, invalid = human_evidence(steps, trajectory.scope, authority_policy)
            verified = {identity for group in events.values() for identity in group.values()}
            if invalid:
                coverage["human_events"] = "PARTIAL"
            steps = [step for step in steps if step.type != "HUMAN" or step.step_id in verified]
        value, reason, ids = _sequence(steps, coverage, params)
        return measured(value, reason, ids, ref=ref)
    if params.rule == "max_same_tool_repeats":
        from .repeats import evaluate_repeats
        value, reason, ids, count = evaluate_repeats(steps, coverage, params, _CausalIndex(steps))
        return measured(value, reason, ids, ref=ref, repeat_count=count)
    if params.rule == "tool_argument_match":
        from .arguments import evaluate_arguments
        value, reason, ids = evaluate_arguments(steps, coverage, params)
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
