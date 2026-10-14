"""冻结身份与可信连续因果证明，不把合成轨迹作为正式来源资格。"""

from decimal import Decimal

import pytest
from app.evaluator_binding import EvaluatorBinding
from app.trajectory.rules import evaluate_trajectory_rule, parse_rule_params
from test_trajectory_arguments import governed_contract
from test_trajectory_assembler import assemble, configured, prepared
from test_trajectory_causal_links import event
from test_trajectory_manifest import context as _launch_context
from test_trajectory_rules import A, B, assert_measured, assert_unknown, context, partial, reseal

launch_context = _launch_context
PARAMS = {"rule": "max_same_tool_repeats", "scope": "ROOT_SUBTREE", "workflow_scope_id": None,
    "tool_identity": A, "phase": "EXECUTED", "identity_paths": ["$.amount", "$.customer_id"], "max_repeats": 1}


def repeated(tools=(A, A), amounts=(5, 5), *, with_contract=False):
    rows, _, scope = prepared()
    contract = governed_contract()
    policy = contract.redaction_policy.model_dump()
    policy["argument_paths"] = ["$.customer_id", "$.amount"]
    contract = configured(contract, redaction_policy=policy)
    root, template = rows[0], rows[1]
    events = []
    for index, tool in enumerate(tools):
        for phase in ("REQUESTED", "EXECUTED"):
            sequence = len(events) + 1
            identity = f"{phase.lower()}-{index}"
            row = event(template, identity, sequence, phase, f"{index + 1:032x}", (sequence - 1,) if sequence > 1 else ())
            row["name"] = "crm_lookup" if tool == A else "ledger_read"
            row["metadata"]["attributes.gen_ai.tool.name"] = row["name"]
            if phase == "REQUESTED":
                row["input"] = {"customer_id": "synthetic-001", "amount": amounts[index]}
            events.append(row)
    sequence = len(events) + 1
    terminal = event(template, "terminal", sequence, "TERMINAL", "f" * 32, (sequence - 1,) if events else ())
    terminal.update(type="SPAN", name="workflow.terminal", output=None)
    terminal["metadata"].update({"attributes.workflow.event_count": sequence,
        "attributes.workflow.event_ids": [f"{i:032x}" for i in range(1, sequence + 1)], "attributes.workflow.final_sequence": sequence})
    value = assemble([root, *events, terminal], contract, scope)
    return (value, contract) if with_contract else value


def binding(params):
    return EvaluatorBinding.from_payload({"id": "trajectory.max_same_tool_repeats", "version": "1.0.0",
        "binding_id": "synthetic-repeats", "scope": "item", "result_type": "boolean",
        "definition_digest": "sha256:" + "a" * 64, "params": params,
        "input_contract": {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "tool-repeats-1.0", "rule_id": "trajectory.max_same_tool_repeats",
            "required_capabilities": ["tool_requests" if params["phase"] == "REQUESTED" else "tool_executions", "tool_arguments", "ordering"]},
        "output_contract": {"type": "boolean"}})


def run(value, **changes):
    params = PARAMS | changes
    return evaluate_trajectory_rule(context(value), params, binding=binding(params), manifest_schema_version="1.3")


@pytest.mark.parametrize("phase", ["REQUESTED", "EXECUTED"])
def test_same_phase_consecutive_logical_calls_exceed_limit_without_double_counting(phase):
    result = run(repeated(), phase=phase)
    assert_measured(result, False, "TOOL_REPEAT_LIMIT_EXCEEDED")
    assert result.evidence["observed_repeat_count"] == 2
    assert len(result.evidence["evidence_step_ids"]) == 2
    assert "synthetic-001" not in str(result.evidence) and "arguments" not in str(result.evidence)
    assert_measured(run(repeated(), phase=phase, max_repeats=2), True)


def test_different_tool_or_frozen_argument_value_breaks_a_repeat():
    assert_measured(run(repeated((A, B, A), (5, 5, 5))), True)
    assert_measured(run(repeated(amounts=(5, 6))), True)
    assert_measured(run(repeated(amounts=(5, Decimal("5.000")))), False)
    assert_measured(run(repeated(amounts=(True, 1))), True)


def test_one_matching_call_does_not_need_foreign_tool_adjacency_to_bound_repeats():
    def mutate(payload):
        next(step for step in payload["steps"] if step["step_id"] == "requested-1")["causal_predecessor_ids"] = []
    value = reseal(repeated((B, A), (5, 5)), mutate)
    assert_measured(run(value), True)
    assert_measured(run(value, max_repeats=0), False)


@pytest.mark.parametrize("state", ["REDACTED", "INVALID", "MISSING", None])
def test_unavailable_identity_is_unknown_without_splitting_calls_into_distinct_buckets(state):
    def mutate(payload):
        step = next(s for s in payload["steps"] if s["step_id"] == "executed-1")
        step["argument_visibility"] = {"$.amount": state} if state else {}
    assert_unknown(run(reseal(repeated(), mutate)))


@pytest.mark.parametrize("change", ["parallel", "gap", "missing_inventory", "missing_time", "scope"])
def test_unproven_adjacency_is_unknown(change):
    def mutate(payload):
        steps = {step["step_id"]: step for step in payload["steps"]}
        if change == "parallel":
            steps["requested-1"]["causal_predecessor_ids"] = []
        elif change == "gap":
            inventory = steps["terminal"]["result_summary"]["terminal_inventory"]
            inventory["observed_events"] = [event for event in inventory["observed_events"] if event["step_id"] != "requested-1"]
        elif change == "missing_inventory":
            steps["terminal"]["result_summary"] = None
        elif change == "missing_time":
            steps["executed-0"]["ended_at"] = None
        else:
            steps["executed-1"]["workflow_scope_id"] = None
    assert_unknown(run(reseal(repeated(), mutate)))


def test_partial_coverage_retains_independently_proven_excess():
    value = partial(repeated(), tool_executions="PARTIAL", ordering="PARTIAL")
    assert_measured(run(value), False)
    assert_unknown(run(value, max_repeats=2))


def test_incomplete_inventory_preserves_local_excess_without_certifying_compliance():
    def mutate(payload):
        inventory = next(s for s in payload["steps"] if s["step_id"] == "terminal")["result_summary"]["terminal_inventory"]
        inventory["observed_events"] = [event for event in inventory["observed_events"] if event["step_id"] != "requested-0"]
    value = reseal(repeated(), mutate)
    assert_measured(run(value), False)
    assert_unknown(run(value, max_repeats=2))


def test_structured_identity_outside_governed_leaf_types_is_unknown():
    def mutate(payload):
        for step in payload["steps"]:
            if step["type"] == "TOOL":
                step["arguments_redacted"]["amount"] = {"unapproved": 5}
    assert_unknown(run(reseal(repeated(), mutate)))


def test_later_unknown_identity_does_not_erase_an_earlier_proven_excess():
    def mutate(payload):
        step = next(s for s in payload["steps"] if s["step_id"] == "executed-2")
        step["argument_visibility"] = {"$.amount": "REDACTED"}
    result = run(reseal(repeated((A, A, A), (5, 5, 5)), mutate))
    assert_measured(result, False)
    assert result.evidence["observed_repeat_count"] == 2


def test_later_missing_inventory_member_does_not_erase_an_earlier_proven_excess():
    def mutate(payload):
        inventory = next(s for s in payload["steps"] if s["step_id"] == "terminal")["result_summary"]["terminal_inventory"]
        inventory["observed_events"] = [event for event in inventory["observed_events"] if event["step_id"] != "executed-2"]
    result = run(reseal(repeated((A, A, A), (5, 5, 5)), mutate))
    assert_measured(result, False)
    assert result.evidence["observed_repeat_count"] == 2


def test_zero_limit_rejects_one_observed_call_and_complete_empty_calls_pass():
    assert_measured(run(repeated((A,), (5,)), max_repeats=0), False)
    assert_measured(run(repeated((), ()), max_repeats=0), True)
    assert_unknown(run(partial(repeated((), ()), tool_executions="PARTIAL")))


@pytest.mark.parametrize("paths,code", [([], "TRAJECTORY_RULE_PARAMS_INVALID"),
    (["$.amount"] * 2, "TRAJECTORY_RULE_PARAMS_INVALID"), (["$..amount"], "TRAJECTORY_JSONPATH_INVALID"),
    (["$.authorization"], "TRAJECTORY_SENSITIVE_MATCH_FIELD")])
def test_identity_paths_are_explicit_static_unique_and_non_sensitive(paths, code):
    with pytest.raises(ValueError, match=code):
        parse_rule_params(PARAMS | {"identity_paths": paths})


@pytest.mark.parametrize("limit", [True, -1, "2", 1.5])
def test_limit_does_not_coerce(limit):
    with pytest.raises(ValueError, match="TRAJECTORY_RULE_PARAMS_INVALID"):
        parse_rule_params(PARAMS | {"max_repeats": limit})


def test_frozen_dispatcher_preserves_boolean_repeat_result(launch_context):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_manifest import create
    value, contract = repeated(with_contract=True)
    launch = create(launch_context, contract=contract, evaluator_selections=[{
        "id": "trajectory.max_same_tool_repeats", "version": "1.0.0", "params": PARAMS}])
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=context(value))
    typed = result.typed_results[0]
    assert_measured(typed, False)
    assert set(typed.evidence) == {"reason_code", "evidence_step_ids", "evidence_ref", "observed_repeat_count"}
    assert result.required_evidence_status != "COMPLETE"


@pytest.mark.parametrize("mode", ["unapproved_path", "legacy_content"])
def test_formal_binding_rejects_unapproved_identity_policy(launch_context, mode):
    from app.evaluator_binding import EvaluatorBindingError
    from test_trajectory_manifest import create
    _, contract = repeated(with_contract=True)
    if mode == "legacy_content":
        policy = contract.redaction_policy.model_dump() | {"content_policy_version": None, "string_policies": None}
        contract = configured(contract, redaction_policy=policy)
    params = PARAMS | ({"identity_paths": ["$.unapproved"]} if mode == "unapproved_path" else {})
    from app.trajectory.manifest import TrajectoryManifestError
    error = TrajectoryManifestError if mode == "legacy_content" else EvaluatorBindingError
    code = "CONTENT_POLICY_REQUIRED" if mode == "legacy_content" else "TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED"
    with pytest.raises(error, match=code):
        create(launch_context, contract=contract, evaluator_selections=[{
            "id": "trajectory.max_same_tool_repeats", "version": "1.0.0", "params": params}])
