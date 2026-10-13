"""四类规则的纯测量合同；合成证据不计作真实来源资格。"""

from copy import deepcopy
from dataclasses import replace

import pytest
from app.evaluator_binding import EvaluatorBinding
from app.evaluator_results import project_legacy_scores
from app.trajectory.assembler import trajectory_digest
from app.trajectory.inputs import EvaluationContext
from app.trajectory.rules import evaluate_trajectory_rule, parse_rule_params
from app.trajectory.schema import StandardTrajectory, TrajectoryEvidenceRef
from test_trajectory_assembler import assemble, configured, prepared
from test_trajectory_causal_links import event

A, B = "banking.customer.lookup", "banking.balance.read"


def trajectory(scenario="serial"):
    rows, contract, scope = prepared()
    mapping = contract.tool_mapping.model_dump()
    mapping["entries"].append({"source_profile_id": "native-otel-v1", "source_profile_version": "fixture-1",
        "raw_name": "ledger_read", "tool_identity": B})
    contract = configured(contract, tool_mapping=mapping)
    root, template = rows[0], rows[1]
    schedule = [(A, "REQUESTED", "req-a", ()), (A, "EXECUTED", "exec-a", (1,)),
                (B, "REQUESTED", "req-b", (2,)), (B, "EXECUTED", "exec-b", (3,))]
    if scenario == "b-a-b":
        schedule = [(B, "REQUESTED", "req-b0", ()), (B, "EXECUTED", "exec-b0", (1,)),
            (A, "REQUESTED", "req-a", (2,)), (A, "EXECUTED", "exec-a", (3,)),
            (B, "REQUESTED", "req-b", (4,)), (B, "EXECUTED", "exec-b", (5,))]
    elif scenario == "zero":
        schedule = []
    elif scenario == "parallel":
        schedule[2] = B, "REQUESTED", "req-b", ()
    events = []
    for index, (tool, phase, identity, predecessors) in enumerate(schedule, 1):
        call = "1" * 32 if tool == A else "2" * 32 if identity.endswith("0") else "3" * 32
        row = event(template, identity, index, phase, call, predecessors)
        row["name"] = "crm_lookup" if tool == A else "ledger_read"
        row["metadata"]["attributes.gen_ai.tool.name"] = row["name"]
        events.append(row)
    terminal = event(template, "terminal", len(events) + 1, "TERMINAL", "4" * 32)
    terminal.update(type="SPAN", name="workflow.terminal", output=None)
    terminal["metadata"].update({"attributes.workflow.event_count": len(events) + 1,
        "attributes.workflow.event_ids": [f"{index:032x}" for index in range(1, len(events) + 2)],
        "attributes.workflow.final_sequence": len(events) + 1})
    return assemble([root, *events, terminal], contract, scope)


def reseal(value, change):
    payload = deepcopy(value.model_dump())
    change(payload)
    payload["provenance"]["trajectory_digest"] = trajectory_digest(StandardTrajectory.model_validate(payload))
    return StandardTrajectory.model_validate(payload)


def context(value, *, output=None):
    ref = TrajectoryEvidenceRef(artifact_id="synthetic-artifact", trajectory_digest=value.provenance.trajectory_digest,
        trajectory_contract_digest=value.provenance.trajectory_contract_digest, schema_version=value.schema_version)
    return EvaluationContext(output or {"tool_calls": [{"name": B}]}, {}, {}, value, ref)


def binding(params):
    params = parse_rule_params(params)
    capability = "tool_requests" if getattr(params, "phase", None) == "REQUESTED" else "tool_executions"
    required = ["ordering", "tool_executions", "tool_requests"] if params.rule == "tool_order" else [capability]
    return EvaluatorBinding.from_payload({"id": "trajectory." + params.rule, "version": "1.0.0",
        "binding_id": "synthetic-binding", "scope": "item", "result_type": "boolean",
        "definition_digest": "sha256:" + "a" * 64, "params": params.model_dump(mode="json"),
        "input_contract": {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "tool-rules-1.0", "rule_id": "trajectory." + params.rule,
            "required_capabilities": required}, "output_contract": {"type": "boolean"}})


def run(value, params, **kwargs):
    return evaluate_trajectory_rule(context(value), params, binding=binding(params), manifest_schema_version="1.3", **kwargs)


def assert_measured(result, expected, reason=None):
    assert result.status == "succeeded" and result.value is expected and result.result_type == "boolean"
    assert result.normalized_value is None and project_legacy_scores([result]) == {}
    if reason:
        assert result.evidence["reason_code"] == reason


def assert_unknown(result):
    assert result.status == "no_result" and result.value is None and project_legacy_scores([result]) == {}


def partial(value, **caps):
    return reseal(value, lambda p: p["completeness"].update(status="PARTIAL",
        capabilities=p["completeness"]["capabilities"] | caps))


def test_lied_output_cannot_create_a_tool_missing_from_complete_observations():
    value = trajectory("zero")
    assert value.completeness.capabilities["tool_executions"] == "COMPLETE"
    result = run(value, {"rule": "require_tool", "tool_identity": B})
    assert_measured(result, False, "TOOL_REQUIRED_MISSING")
    assert result.evidence["evidence_step_ids"] == []


@pytest.mark.parametrize("phase", ["EXECUTED", "REQUESTED"])
def test_require_uses_the_frozen_phase_and_real_success_for_executions(phase):
    value = trajectory()
    assert_measured(run(value, {"rule": "require_tool", "tool_identity": A, "phase": phase}), True)
    failed = reseal(value, lambda p: next(s for s in p["steps"] if s["step_id"] == "exec-a").update(status="FAILED"))
    assert_measured(run(failed, {"rule": "require_tool", "tool_identity": A, "phase": phase}), phase == "REQUESTED")
    unknown = reseal(value, lambda p: next(s for s in p["steps"] if s["step_id"] == "exec-a").update(status="UNKNOWN"))
    if phase == "EXECUTED":
        assert_unknown(run(unknown, {"rule": "require_tool", "tool_identity": A}))


@pytest.mark.parametrize("rule,kwargs", [("require_tool", {}), ("forbid_tool", {}), ("max_tool_calls", {"max_count": 1})])
def test_no_tools_with_incomplete_coverage_never_proves_absence_or_compliance(rule, kwargs):
    value = partial(trajectory("zero"), tool_executions="PARTIAL")
    assert_unknown(run(value, {"rule": rule, "tool_identity": A, **kwargs}))


def test_blocked_action_is_a_request_without_becoming_an_execution():
    value = reseal(trajectory(), lambda p: next(s for s in p["steps"] if s["step_id"] == "exec-b").update(phase="BLOCKED"))
    assert_measured(run(value, {"rule": "forbid_tool", "tool_identity": B}), True)
    assert_measured(run(value, {"rule": "forbid_tool", "tool_identity": B, "phase": "REQUESTED"}), False)
    assert_measured(run(value, {"rule": "max_tool_calls", "tool_identity": B, "max_count": 0}), True)


def test_partial_known_forbidden_execution_is_preserved_and_contains_only_safe_references():
    value = partial(trajectory(), tool_executions="PARTIAL")
    result = run(value, {"rule": "forbid_tool", "tool_identity": B})
    assert_measured(result, False, "TOOL_FORBIDDEN_OBSERVED")
    assert result.evidence["evidence_step_ids"] == ["exec-b"]
    assert set(result.evidence) == {"reason_code", "evidence_step_ids", "evidence_ref"}
    assert result.evidence["evidence_ref"] == context(value).trajectory_evidence_ref.model_dump(mode="json")
    assert "arguments" not in str(result.evidence) and "synthetic-001" not in str(result.evidence)


@pytest.mark.parametrize("phase", ["REQUESTED", "EXECUTED"])
@pytest.mark.parametrize("limit,expected", [(0, False), (1, False), (2, True), (3, True)])
def test_max_count_is_distinct_per_phase_and_not_a_numeric_measurement(phase, limit, expected):
    result = run(trajectory("b-a-b"), {"rule": "max_tool_calls", "tool_identity": B, "phase": phase, "max_count": limit})
    assert_measured(result, expected)
    assert result.evidence["observed_count"] == 2


def test_known_excess_survives_partial_coverage_but_below_limit_is_unknown():
    value = partial(trajectory("b-a-b"), tool_executions="PARTIAL")
    assert_measured(run(value, {"rule": "max_tool_calls", "tool_identity": B, "max_count": 1}), False)
    assert_unknown(run(value, {"rule": "max_tool_calls", "tool_identity": B, "max_count": 2}))


ORDER = {"rule": "tool_order", "before_tool_identity": A, "after_tool_identity": B}


def test_every_after_checks_the_first_b_instead_of_accepting_the_later_valid_pair():
    result = run(trajectory("b-a-b"), ORDER)
    assert_measured(result, False, "PREREQUISITE_UNSATISFIED")
    assert result.evidence["evidence_step_ids"] == ["req-b0"]
    assert_measured(run(trajectory(), ORDER), True, "ALL_B_HAVE_SUCCESSFUL_CAUSAL_PREDECESSOR")


def test_successful_order_carries_the_observed_proof_and_is_independent_of_display_order():
    value = trajectory()
    result = run(value, ORDER)
    assert result.evidence["evidence_step_ids"] == ["exec-a", "req-b"]
    shuffled = reseal(value, lambda p: p["steps"].reverse())
    assert run(shuffled, ORDER).value is True
    assert run(shuffled, ORDER).evidence["evidence_step_ids"] == result.evidence["evidence_step_ids"]


def test_successful_a_from_another_workflow_cannot_satisfy_b():
    def mutate(payload):
        for step in payload["steps"]:
            if step["tool_identity"] == B:
                step["workflow_scope_id"] = "separate-workflow"
        next(s for s in payload["steps"] if s["step_id"] == "req-b")["causal_predecessor_ids"] = []
    assert_measured(run(reseal(trajectory(), mutate), ORDER), False)


@pytest.mark.parametrize("change", [{"scope": "WORKFLOW"}, {"workflow_scope_id": "wf"},
    {"mode": "ANY_PAIR"}, {"prerequisite_status": "FAILED"}, {"unexpected": "synthetic-secret"}])
def test_rule_scope_order_mode_and_extra_parameters_are_strict(change):
    result = evaluate_trajectory_rule(context(trajectory()), ORDER | change, binding=binding(ORDER), manifest_schema_version="1.3")
    assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_PARAMS_INVALID"
    assert "synthetic-secret" not in result.error_message


def test_partial_coverage_keeps_a_directly_observed_successful_required_tool():
    value = partial(trajectory(), tool_executions="PARTIAL")
    assert_measured(run(value, {"rule": "require_tool", "tool_identity": A}), True)


def test_time_parent_and_display_index_without_causal_edges_never_imply_order():
    value = trajectory("parallel")
    assert value.completeness.capabilities["ordering"] == "COMPLETE"
    assert_unknown(run(value, ORDER))


@pytest.mark.parametrize("change", ["missing_time", "overlap", "clock_reversal", "scope", "cycle", "missing_ref", "failed_a"])
def test_order_with_unproven_prerequisite_never_passes(change):
    def mutate(payload):
        steps = {s["step_id"]: s for s in payload["steps"]}
        if change == "missing_time":
            steps["exec-a"]["ended_at"] = None
        elif change == "overlap":
            steps["req-b"]["started_at"] = steps["exec-a"]["started_at"]
        elif change == "clock_reversal":
            steps["exec-a"]["ended_at"] = steps["req-b"]["ended_at"]
        elif change == "scope":
            steps["req-b"]["workflow_scope_id"] = None
        elif change == "cycle":
            steps["req-a"]["causal_predecessor_ids"] = ["exec-b"]
        elif change == "missing_ref":
            steps["req-b"]["causal_predecessor_ids"] = ["not-collected"]
        else:
            steps["exec-a"]["status"] = "FAILED"
    result = run(reseal(trajectory(), mutate), ORDER)
    if change == "failed_a":
        assert_measured(result, False)
    else:
        assert_unknown(result)


def test_partial_only_late_a_does_not_fabricate_absence_of_an_earlier_a():
    value = partial(trajectory("b-a-b"), tool_executions="PARTIAL", tool_requests="PARTIAL", ordering="PARTIAL")
    assert_unknown(run(value, ORDER))


def test_first_unknown_does_not_hide_a_later_locally_proven_violation():
    def mutate(payload):
        # First B has a missing clock; the later B has no successful A.
        for step in payload["steps"]:
            if step["step_id"] == "req-b0":
                step["ended_at"] = None
            if step["tool_identity"] == A and step["phase"] == "EXECUTED":
                step["status"] = "FAILED"
        # Later B is independent of the uncertain component.
        next(s for s in payload["steps"] if s["step_id"] == "req-b")["causal_predecessor_ids"] = []
        payload["completeness"]["status"] = "PARTIAL"
        payload["completeness"]["capabilities"]["ordering"] = "PARTIAL"
    result = run(reseal(trajectory("b-a-b"), mutate), ORDER)
    assert_measured(result, False)
    assert result.evidence["evidence_step_ids"] == ["req-b"]


def test_no_b_is_vacuously_true_only_with_complete_request_coverage():
    value = trajectory("zero")
    assert_measured(run(value, ORDER), True)
    assert_unknown(run(partial(value, tool_requests="PARTIAL"), ORDER))


@pytest.mark.parametrize("rule", ["require_tool", "forbid_tool", "max_tool_calls", "tool_order"])
def test_http_only_and_missing_input_remain_unknown(rule):
    params = ORDER if rule == "tool_order" else {"rule": rule, "tool_identity": A, **({"max_count": 1} if rule == "max_tool_calls" else {})}
    base = trajectory()
    root_only = reseal(base, lambda p: p.update(steps=[p["steps"][0]], completeness={"status": "ROOT_ONLY",
        "capabilities": {key: "NONE" for key in p["completeness"]["capabilities"]}, "warnings": []}))
    assert_unknown(run(root_only, params))
    assert_unknown(evaluate_trajectory_rule(None, params, binding=binding(params), manifest_schema_version="1.3"))


@pytest.mark.parametrize("failure", ["digest", "contract", "schema", "mutable_trajectory"])
def test_reference_and_owned_input_identity_are_checked_before_measurement(failure):
    ctx = context(trajectory())
    ref = ctx.trajectory_evidence_ref.model_dump()
    if failure == "digest":
        ref["trajectory_digest"] = "sha256:" + "0" * 64
    elif failure == "contract":
        ref["trajectory_contract_digest"] = "sha256:" + "0" * 64
    elif failure == "schema":
        ref["schema_version"] = "2.0"
    else:
        ctx.trajectory.steps.pop()
    ref = ctx.trajectory_evidence_ref.model_copy(update=ref)
    result = evaluate_trajectory_rule(replace(ctx, trajectory_evidence_ref=ref), ORDER,
        binding=binding(ORDER), manifest_schema_version="1.3")
    assert result.status == "failed" and result.value is None
    assert result.error_code == "TRAJECTORY_RULE_INPUT_INVALID"


@pytest.mark.parametrize("bad", [True, -1, "1", 1.1, None])
def test_max_count_rejects_coercion_and_invalid_business_limits_without_leaking_values(bad):
    valid = {"rule": "max_tool_calls", "tool_identity": A, "max_count": 1}
    result = evaluate_trajectory_rule(context(trajectory()), valid | {"max_count": bad}, binding=binding(valid),
        manifest_schema_version="1.3")
    assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_PARAMS_INVALID" and result.value is None


@pytest.mark.parametrize("field,value", [("input_kind", "output"), ("rule_id", "trajectory.forbid_tool"),
    ("algorithm_version", "unregistered"), ("trajectory_schema_version", "2.0"), ("required_capabilities", [])])
def test_binding_cannot_relabel_another_algorithm_schema_or_input(field, value):
    bound = binding(ORDER)
    bound = replace(bound, input_contract=bound.input_contract | {field: value})
    result = evaluate_trajectory_rule(context(trajectory()), ORDER, binding=bound, manifest_schema_version="1.3")
    assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_BINDING_MISMATCH"


def test_parameter_mismatch_and_old_manifest_do_not_execute_the_new_measurement():
    params = {"rule": "max_tool_calls", "tool_identity": A, "max_count": 1}
    for changed, schema in ((params | {"max_count": 0}, "1.3"), (params, "1.2")):
        result = evaluate_trajectory_rule(context(trajectory()), changed, binding=binding(params), manifest_schema_version=schema)
        assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_BINDING_MISMATCH"


def test_measurement_does_not_modify_the_owned_trajectory_or_frozen_binding():
    value = trajectory()
    bound = binding(ORDER)
    before = value.model_dump(), deepcopy(bound.to_payload())
    result = evaluate_trajectory_rule(context(value), ORDER, binding=bound, manifest_schema_version="1.3")
    assert_measured(result, True)
    assert (value.model_dump(), bound.to_payload()) == before


def test_explicit_workflow_selection_never_treats_an_unknown_workflow_as_an_empty_one():
    value = trajectory()
    workflow = next(s.workflow_scope_id for s in value.steps if s.step_id == "req-a")
    params = {"rule": "max_tool_calls", "tool_identity": B, "max_count": 1, "scope": "WORKFLOW", "workflow_scope_id": workflow}
    assert_measured(run(value, params), True)
    assert_unknown(run(value, params | {"workflow_scope_id": "not-observed"}))


@pytest.mark.parametrize("rule", ["require_tool", "forbid_tool", "max_tool_calls", "tool_order"])
def test_selected_workflow_cannot_exclude_relevant_events_with_unknown_ownership(rule):
    value = trajectory()
    workflow = next(s.workflow_scope_id for s in value.steps if s.step_id == "req-a")
    identity = "req-b" if rule == "tool_order" else "exec-b"
    changed = reseal(value, lambda p: next(s for s in p["steps"] if s["step_id"] == identity).update(workflow_scope_id=None))
    params = ORDER if rule == "tool_order" else {"rule": rule, "tool_identity": B,
        **({"max_count": 0} if rule == "max_tool_calls" else {})}
    assert_unknown(run(changed, params | {"scope": "WORKFLOW", "workflow_scope_id": workflow}))


def test_ambiguous_prerequisite_workflow_prevents_a_false_absence_claim():
    value = trajectory()
    workflow = next(s.workflow_scope_id for s in value.steps if s.step_id == "req-b")
    def mutate(payload):
        next(s for s in payload["steps"] if s["step_id"] == "exec-a")["workflow_scope_id"] = None
        next(s for s in payload["steps"] if s["step_id"] == "req-b")["causal_predecessor_ids"] = []
    assert_unknown(run(reseal(value, mutate), ORDER | {"scope": "WORKFLOW", "workflow_scope_id": workflow}))


def test_ambiguous_later_b_does_not_erase_a_known_first_b_violation():
    value = trajectory("b-a-b")
    workflow = next(s.workflow_scope_id for s in value.steps if s.step_id == "req-a")
    changed = reseal(value, lambda p: next(s for s in p["steps"] if s["step_id"] == "req-b").update(workflow_scope_id=None))
    result = run(changed, ORDER | {"scope": "WORKFLOW", "workflow_scope_id": workflow})
    assert_measured(result, False)
    assert result.evidence["evidence_step_ids"] == ["req-b0"]


def test_known_b_with_partial_request_coverage_retains_complete_prerequisite_violation():
    value = partial(trajectory("b-a-b"), tool_requests="PARTIAL")
    assert_measured(run(value, ORDER), False)
    assert_unknown(run(partial(value, tool_requests="NONE"), ORDER))


def test_workflow_ambiguity_keeps_known_measurements_without_claiming_a_count_upper_bound():
    value = trajectory("b-a-b")
    workflow = next(s.workflow_scope_id for s in value.steps if s.step_id == "req-a")
    changed = reseal(value, lambda p: next(s for s in p["steps"] if s["step_id"] == "exec-b0").update(workflow_scope_id=None))
    selected = {"tool_identity": B, "scope": "WORKFLOW", "workflow_scope_id": workflow}
    assert_measured(run(changed, selected | {"rule": "require_tool"}), True)
    assert_measured(run(changed, selected | {"rule": "forbid_tool"}), False)
    assert_measured(run(changed, selected | {"rule": "max_tool_calls", "max_count": 0}), False)
    assert_unknown(run(changed, selected | {"rule": "max_tool_calls", "max_count": 1}))


def test_boolean_numeric_normalization_is_rejected_before_trajectory_measurement():
    bound = replace(binding(ORDER), normalization_rule={"kind": "boolean", "true_value": 1.0, "false_value": 0.0})
    result = evaluate_trajectory_rule(context(trajectory()), ORDER, binding=bound, manifest_schema_version="1.3")
    assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_BINDING_MISMATCH"
    assert result.value is None and result.normalized_value is None and project_legacy_scores([result]) == {}


def test_long_causal_chain_uses_bounded_iterative_traversal():
    def mutate(payload):
        steps = {s["step_id"]: s for s in payload["steps"]}
        middle, previous = [], "exec-a"
        for index in range(994):
            step = deepcopy(steps["terminal"])
            step.update(step_id=f"middle-{index}", observation_id=f"middle-{index}", result_summary=None,
                started_at=steps["exec-a"]["ended_at"], ended_at=steps["exec-a"]["ended_at"],
                causal_predecessor_ids=[previous])
            previous = step["step_id"]
            middle.append(step)
        steps["req-b"]["causal_predecessor_ids"] = [previous]
        payload["steps"] = list(steps.values()) + middle
        for index, step in enumerate(payload["steps"]):
            step["index"] = index
    value = reseal(trajectory(), mutate)
    assert len(value.steps) == 1000
    assert_measured(run(value, ORDER), True)
