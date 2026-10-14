"""可信因果子序列：顺序来自边，显示排序与 wall-clock 不构成证明。"""

from dataclasses import replace

import pytest
from app.evaluator_binding import EvaluatorBinding
from app.trajectory.rules import evaluate_trajectory_rule
from test_trajectory_manifest import context as _launch_context
from test_trajectory_rules import A, B, assert_measured, assert_unknown, context, partial, reseal, trajectory

launch_context = _launch_context


def descriptor(tool, phase="EXECUTED"):
    return {"type": "TOOL", "tool_identity": tool, "phase": phase}


PARAMS = {"rule": "required_step_sequence", "scope": "ROOT_SUBTREE", "workflow_scope_id": None,
    "steps": [descriptor(A), descriptor(B)]}


def bound(params):
    capabilities = ["ordering"] + sorted({"tool_requests" if step["phase"] == "REQUESTED" else "tool_executions"
        for step in params["steps"] if step["type"] == "TOOL"})
    return EvaluatorBinding.from_payload({"id": "trajectory.required_step_sequence", "version": "1.0.0",
        "binding_id": "synthetic-sequence", "scope": "item", "result_type": "boolean",
        "definition_digest": "sha256:" + "a" * 64, "params": params,
        "input_contract": {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "step-sequence-1.0", "rule_id": "trajectory.required_step_sequence",
            "required_capabilities": capabilities}, "output_contract": {"type": "boolean"}})


def run(value, **changes):
    params = PARAMS | changes
    return evaluate_trajectory_rule(context(value), params, binding=bound(params), manifest_schema_version="1.3")


def test_same_workflow_causal_subsequence_passes_without_display_order():
    value = reseal(trajectory(), lambda p: p["steps"].reverse())
    result = run(value)
    assert_measured(result, True, "STEP_SEQUENCE_OBSERVED")
    assert result.evidence["evidence_step_ids"] == ["exec-a", "exec-b"]
    assert set(result.evidence) == {"reason_code", "evidence_step_ids", "evidence_ref"}


def test_transitive_edges_allow_intervening_steps_and_repeated_descriptors():
    result = run(trajectory("b-a-b"), steps=[descriptor(B), descriptor(A), descriptor(B)])
    assert_measured(result, True)
    assert result.evidence["evidence_step_ids"] == ["exec-a", "exec-b", "exec-b0"]


def test_one_step_cannot_satisfy_two_occurrences():
    assert_measured(run(trajectory(), steps=[descriptor(A), descriptor(A)]), False, "STEP_SEQUENCE_ABSENT")


def test_complete_reversed_sequence_is_false_and_parallel_is_unknown():
    assert_measured(run(trajectory(), steps=[descriptor(B), descriptor(A)]), False)
    assert_unknown(run(trajectory("parallel")))


def test_complete_absent_descriptor_is_false_partial_absence_is_unknown():
    assert_measured(run(trajectory("zero")), False)
    assert_unknown(run(partial(trajectory("zero"), tool_executions="PARTIAL")))


def test_observed_sequence_remains_a_proof_under_partial_coverage():
    assert_measured(run(partial(trajectory(), tool_executions="PARTIAL", ordering="PARTIAL")), True)
    assert_unknown(run(partial(trajectory(), ordering="NONE")))


@pytest.mark.parametrize("mutation", ["scope", "missing_time", "missing_edge", "cycle"])
def test_untrusted_or_unscoped_order_never_passes(mutation):
    def change(payload):
        steps = {step["step_id"]: step for step in payload["steps"]}
        if mutation == "scope":
            steps["exec-b"]["workflow_scope_id"] = None
        elif mutation == "missing_time":
            steps["exec-a"]["ended_at"] = None
        elif mutation == "missing_edge":
            steps["req-b"]["causal_predecessor_ids"] = []
        else:
            steps["req-a"]["causal_predecessor_ids"] = ["exec-b"]
    assert_unknown(run(reseal(trajectory(), change)))


def test_other_workflow_cannot_complete_the_sequence():
    def change(payload):
        for step in payload["steps"]:
            if step["tool_identity"] == B:
                step["workflow_scope_id"] = "other-workflow"
                step["causal_predecessor_ids"] = []
    assert_measured(run(reseal(trajectory(), change)), False)


def test_unknown_workflow_membership_cannot_be_filtered_into_absence():
    value = trajectory()
    workflow = next(step.workflow_scope_id for step in value.steps if step.step_id == "exec-a")
    changed = reseal(value, lambda p: next(step for step in p["steps"] if step["step_id"] == "exec-b").update(workflow_scope_id=None))
    assert_unknown(run(changed, scope="WORKFLOW", workflow_scope_id=workflow))
    assert_unknown(run(value, scope="WORKFLOW", workflow_scope_id="missing"))


def test_phase_is_frozen_and_blocked_execution_does_not_satisfy_it():
    changed = reseal(trajectory(), lambda p: next(step for step in p["steps"] if step["step_id"] == "exec-b").update(phase="BLOCKED"))
    assert_measured(run(changed), False)
    assert_measured(run(changed, steps=[descriptor(A), descriptor(B, "REQUESTED")]), True)


@pytest.mark.parametrize("steps", [[], [descriptor(A)] * 33,
    [descriptor(A) | {"type": "LLM"}], [descriptor(A) | {"phase": "REGEX"}],
    [descriptor(A) | {"expression": "synthetic-secret"}], [descriptor(A) | {"tool_identity": None}]])
def test_descriptor_contract_is_bounded_and_finite(steps):
    params = PARAMS | {"steps": steps}
    result = evaluate_trajectory_rule(context(trajectory()), params, binding=bound(PARAMS), manifest_schema_version="1.3")
    assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_PARAMS_INVALID"
    assert "synthetic-secret" not in result.error_message


def test_algorithm_identity_cannot_be_relabelled():
    binding = bound(PARAMS)
    result = evaluate_trajectory_rule(context(trajectory()), PARAMS,
        binding=replace(binding, input_contract=binding.input_contract | {"algorithm_version": "tool-rules-1.0"}),
        manifest_schema_version="1.3")
    assert result.status == "failed" and result.error_code == "TRAJECTORY_RULE_BINDING_MISMATCH"


def test_registry_freezes_new_rule_as_opt_in_boolean():
    from app.evaluators import default_evaluator_registry as registry
    from app.trajectory.bindings import declared_contract, freeze_rule_config
    version = registry.version("trajectory.required_step_sequence", "1.0.0")
    assert version is not None
    assert registry.definition("trajectory.required_step_sequence").default_selected is False
    frozen, contract = freeze_rule_config(version, PARAMS)
    assert frozen == PARAMS and contract == bound(PARAMS).input_contract
    assert version.input_contract == declared_contract("required_step_sequence")


@pytest.mark.parametrize("scenario,expected", [("serial", True), ("zero", False), ("parallel", None)])
def test_frozen_dispatcher_preserves_sequence_results_and_cannot_claim_synthetic_qualification(launch_context, scenario, expected):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_manifest import create
    value, contract = trajectory(scenario, with_contract=True)
    launch = create(launch_context, contract=contract, evaluator_selections=[{
        "id": "trajectory.required_step_sequence", "version": "1.0.0", "params": PARAMS}])
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=context(value))
    typed = result.typed_results[0]
    assert typed.status == ("succeeded" if expected is not None else "no_result")
    assert typed.value is expected and typed.normalized_value is None
    assert set(typed.evidence) == {"reason_code", "evidence_step_ids", "evidence_ref"}
    assert result.required_evidence_status != "COMPLETE"


def test_sequence_config_changes_measurement_without_replacing_existing_results(launch_context):
    from test_trajectory_manifest import create
    _, contract = trajectory(with_contract=True)
    def freeze(params):
        return create(launch_context, contract=contract, evaluator_selections=[{
            "id": "trajectory.required_step_sequence", "version": "1.0.0", "params": params}]).manifest
    first = freeze(PARAMS)
    second = freeze(PARAMS | {"steps": list(reversed(PARAMS["steps"]))})
    assert first["measurement_digest"] != second["measurement_digest"]
    assert first["evaluators"][0]["binding_id"] != second["evaluators"][0]["binding_id"]
    assert first["trajectory"] == second["trajectory"]
