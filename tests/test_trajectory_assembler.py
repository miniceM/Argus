from copy import deepcopy
from decimal import Decimal

import pytest
from app.trajectory import assembler
from app.trajectory.codec import canonical_dumps, canonical_loads
from app.trajectory.contract import TrajectoryContract
from app.trajectory.normalizer import NormalizedObservations, ObservationConvergence, normalize_observations
from app.trajectory.schema import StandardTrajectory
from test_trajectory_causal_links import event, inputs
from test_trajectory_contract import version


def prepared():
    rows, contract, scope = inputs()
    terminal = event(rows[1], "terminal", 5, "TERMINAL", "3" * 32)
    terminal.update(type="SPAN", name="workflow.terminal", output=None)
    terminal["metadata"].update({"attributes.workflow.event_count": 5,
                                 "attributes.workflow.event_ids": [f"{n:032x}" for n in range(1, 6)],
                                 "attributes.workflow.final_sequence": 5})
    return rows + [terminal], contract, scope


def configured(contract, **changes):
    payload = contract.model_dump()
    for key, value in changes.items():
        if key == "limits":
            payload[key].update(value)
        elif key == "capabilities":
            for profile in payload["source_profile_set"]:
                profile[key].update(value)
        else:
            payload[key] = value
    return TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))


def batch(rows, contract, scope, *, quiet=Decimal(2), paging=True, proved=True):
    normalized = normalize_observations(rows, contract, scope=scope)
    if proved:
        proof = ObservationConvergence(normalized_digest=normalized.semantic_digest,
                                       paging_complete=paging, quiet_window_seconds=quiet)
        normalized = NormalizedObservations.model_validate(normalized.model_dump() | {"convergence": proof})
    return normalized


def assemble(rows, contract, scope, **proof):
    return assembler.assemble_trajectory(scope, batch(rows, contract, scope, **proof), contract)


def by_id(trajectory):
    return {step.step_id: step for step in trajectory.steps}


def test_frozen_inventory_convergence_and_relevant_roles_form_complete_trajectory():
    rows, contract, scope = prepared()
    result = assemble(rows, contract, scope)
    assert isinstance(result, StandardTrajectory) and result.scope == scope
    assert result.completeness.status == "OBSERVED_COMPLETE"
    assert result.completeness.capabilities == {
        "tool_executions": "COMPLETE", "tool_requests": "COMPLETE", "tool_arguments": "COMPLETE",
        "human_events": "NONE", "ordering": "COMPLETE", "timing": "PARTIAL"}
    assert [step.index for step in result.steps] == list(range(6))
    assert by_id(result)["exec-a"].causal_predecessor_ids == ["req-a"]
    assert by_id(result)["req-b"].causal_predecessor_ids == ["exec-a"]
    assert by_id(result)["exec-a"].arguments_redacted == {"customer_id": "synthetic-001"}
    assert result.input_redacted is None and result.output_redacted == {"intent": "balance"}
    terminal = by_id(result)["terminal"]
    assert terminal.phase == "EVENT" and terminal.causal_predecessor_ids == []
    assert terminal.result_summary["terminal_inventory"]["event_count"] == 5
    assert "synthetic-secret" not in canonical_dumps(result)
    assert result.provenance.trajectory_contract_digest == contract.digest
    assert assembler.trajectory_digest(result) == result.provenance.trajectory_digest


@pytest.mark.parametrize("change,reason", [("missing-terminal", "TERMINAL_INVENTORY_MISSING"),
                                           ("missing-event", "TERMINAL_INVENTORY_INCOMPLETE"),
                                           ("wrong-set", "TERMINAL_INVENTORY_INCOMPLETE"),
                                           ("duplicate-id", "TERMINAL_INVENTORY_CONFLICT"),
                                           ("duplicate-sequence", "TERMINAL_INVENTORY_CONFLICT"),
                                           ("terminal-not-final", "TERMINAL_INVENTORY_INCOMPLETE"),
                                           ("extra-terminal", "TERMINAL_INVENTORY_CONFLICT"),
                                           ("invalid-count", "TERMINAL_INVENTORY_INVALID")])
def test_quiet_window_cannot_cover_an_inventory_gap(change, reason):
    rows, contract, scope = prepared()
    metadata = rows[-1]["metadata"]
    if change == "missing-terminal":
        rows.pop()
    elif change == "missing-event":
        rows.pop(3)
    elif change == "wrong-set":
        metadata["attributes.workflow.event_ids"][-1] = "f" * 32
    elif change == "duplicate-id":
        rows[2]["metadata"]["attributes.workflow.event_id"] = f"{1:032x}"
    elif change == "duplicate-sequence":
        rows[2]["metadata"]["attributes.workflow.sequence"] = 1
    elif change == "terminal-not-final":
        metadata["attributes.workflow.sequence"] = 6
    elif change == "extra-terminal":
        rows.append(deepcopy(rows[-1]) | {"id": "terminal-2"})
    else:
        metadata["attributes.workflow.event_count"] = True
    result = assemble(rows, contract, scope)
    assert result.completeness.status == "PARTIAL" and reason in result.completeness.warnings
    assert result.completeness.capabilities["tool_executions"] != "COMPLETE"
    assert "exec-b" in by_id(result)  # 无关可信执行未被删除。


@pytest.mark.parametrize("proof,reason", [({"proved": False}, "ACQUISITION_CONVERGENCE_UNPROVEN"),
                                         ({"paging": False}, "ACQUISITION_PAGING_INCOMPLETE"),
                                         ({"quiet": Decimal("1.999")}, "ACQUISITION_QUIET_UNPROVEN")])
def test_only_a_proof_bound_to_final_batch_can_enable_absence_coverage(proof, reason):
    rows, contract, scope = prepared()
    result = assemble(rows, contract, scope, **proof)
    assert result.completeness.status == "PARTIAL" and reason in result.completeness.warnings
    assert result.completeness.capabilities["tool_executions"] == "PARTIAL"


def test_changed_rows_or_scope_cannot_reuse_a_convergence_proof():
    rows, contract, scope = prepared()
    normalized = batch(rows, contract, scope)
    for change in ("row", "scope", "contract", "truncated", "bytes"):
        data = normalized.model_dump()
        if change == "row":
            data["records"][1]["observation"]["step"]["duration_ms"] = Decimal(9)
        elif change == "scope":
            data["scope"]["attempt_id"] = "other"
        elif change == "contract":
            data["trajectory_contract_digest"] = "sha256:" + "0" * 64
        elif change == "truncated":
            data["truncated"] = True
        else:
            data["source_bytes_complete"] = False
        with pytest.raises(ValueError, match="CONVERGENCE_BATCH_MISMATCH"):
            NormalizedObservations.model_validate(data)


def test_profile_partial_none_and_missing_required_timing_cannot_be_promoted():
    rows, contract, scope = prepared()
    for coverage in ("PARTIAL", "NONE"):
        changed = configured(contract, capabilities={"tool_executions": coverage})
        result = assemble(rows, changed, scope)
        assert result.completeness.status == "PARTIAL"
        assert result.completeness.capabilities["tool_executions"] == coverage
    changed = configured(contract, required_capabilities=["tool_executions", "timing"])
    assert assemble(rows, changed, scope).completeness.status == "PARTIAL"
    rows[2]["metadata"].pop("attributes.workflow.duration_ms")
    result = assemble(rows, contract, scope)
    assert result.completeness.capabilities["timing"] == "PARTIAL"
    assert by_id(result)["exec-a"].duration_ms is None


@pytest.mark.parametrize("visibility", ["MISSING", "REDACTED", "UNCOLLECTED", "INVALID"])
def test_verified_pair_preserves_parameter_visibility_and_never_restores_hidden_values(visibility):
    rows, contract, scope = prepared()
    rows[1]["metadata"]["attributes.workflow.arguments_visibility"] = visibility
    rows[1]["input"] = {"other": 1} if visibility == "MISSING" else None if visibility != "INVALID" else "broken"
    if visibility == "REDACTED":
        rows[1]["metadata"]["attributes.workflow.hidden_paths"] = ["$.customer_id"]
    result = assemble(rows, contract, scope)
    step = by_id(result)["exec-a"]
    assert step.argument_visibility == ({} if visibility == "UNCOLLECTED" else {"$.customer_id": visibility})
    if visibility != "MISSING":
        assert result.completeness.capabilities["tool_arguments"] != "COMPLETE"
    assert "synthetic-secret" not in canonical_dumps(result)


@pytest.mark.parametrize("visibility", ["REDACTED", "INVALID"])
def test_execution_intrinsic_parameter_uncertainty_is_not_overwritten_by_available_request(visibility):
    rows, contract, scope = prepared()
    rows[2]["metadata"]["attributes.workflow.arguments_visibility"] = visibility
    rows[2]["input"] = None if visibility == "REDACTED" else "broken"
    result = assemble(rows, contract, scope)
    assert by_id(result)["exec-a"].argument_visibility == {"$.customer_id": visibility}
    assert result.completeness.capabilities["tool_arguments"] == "PARTIAL"


def test_invalid_pair_does_not_copy_arguments_or_erase_independent_execution():
    rows, contract, scope = prepared()
    rows[2]["metadata"]["attributes.workflow.call_id"] = "4" * 32
    result = assemble(rows, contract, scope)
    assert by_id(result)["exec-a"].arguments_redacted is None
    assert by_id(result)["exec-b"].arguments_redacted == {"customer_id": "synthetic-001"}
    assert result.completeness.capabilities["tool_arguments"] != "COMPLETE"


def test_parallel_explicit_relations_survive_without_display_order_becoming_causal():
    rows, contract, scope = prepared()
    rows[3]["metadata"]["attributes.workflow.predecessor_event_ids"] = []
    result = assemble(rows, contract, scope)
    assert result.completeness.status == "OBSERVED_COMPLETE"
    assert by_id(result)["req-b"].causal_predecessor_ids == []
    assert by_id(result)["exec-b"].causal_predecessor_ids == ["req-b"]
    rows[2]["metadata"]["attributes.workflow.phase"] = "BLOCKED"
    assert by_id(assemble(rows, contract, scope))["exec-a"].phase == "BLOCKED"


def test_arrival_order_duplicates_and_operational_quiet_elapsed_do_not_change_semantic_digest():
    rows, contract, scope = prepared()
    first = assemble(rows, contract, scope)
    assert canonical_dumps(first) == canonical_dumps(assemble(list(reversed(rows)), contract, scope))
    assert canonical_dumps(first) == canonical_dumps(assemble(rows + [deepcopy(rows[1])], contract, scope))
    assert first.provenance.trajectory_digest == assemble(rows, contract, scope, quiet=Decimal(3)).provenance.trajectory_digest
    exact = canonical_loads(canonical_dumps(first))
    exact["steps"] = [{**step, "started_at": first.steps[i].started_at, "ended_at": first.steps[i].ended_at}
                      for i, step in enumerate(exact["steps"])]
    assert assembler.trajectory_digest(StandardTrajectory.model_validate(exact)) == first.provenance.trajectory_digest


def test_evidence_duration_and_contract_changes_change_digest():
    rows, contract, scope = prepared()
    first = assemble(rows, contract, scope)
    rows[2]["metadata"]["attributes.workflow.duration_ms"] = Decimal("1.00000000000000000001")
    assert assemble(rows, contract, scope).provenance.trajectory_digest != first.provenance.trajectory_digest
    rows[2]["metadata"].pop("attributes.workflow.duration_ms")
    assert by_id(assemble(rows, contract, scope))["exec-a"].duration_ms is None
    other = configured(contract, assembler_version="fixture-2")
    assert assemble(rows, other, scope).provenance.trajectory_digest != first.provenance.trajectory_digest


def test_step_and_artifact_limits_reserve_root_and_remove_dangling_display_references():
    rows, contract, scope = prepared()
    changed = configured(contract, limits={"max_steps": 3})
    limited = assemble(rows, changed, scope)
    assert len(limited.steps) == 3 and "root" in by_id(limited)
    assert limited.completeness.status == "PARTIAL"
    assert all(set(step.causal_predecessor_ids) <= by_id(limited).keys() for step in limited.steps)
    first = assemble(rows, contract, scope)
    cap = len(canonical_dumps(first).encode()) - 400
    changed = configured(contract, limits={"max_artifact_bytes": cap})
    limited = assemble(rows, changed, scope)
    assert len(canonical_dumps(limited).encode()) <= cap and "root" in by_id(limited)
    assert limited.completeness.status == "PARTIAL" and "TRAJECTORY_ARTIFACT_LIMIT" in limited.completeness.warnings
    assert len(limited.steps) < len(first.steps)
    with pytest.raises(ValueError, match="ARTIFACT_ROOT_LIMIT"):
        assemble(rows, configured(contract, limits={"max_artifact_bytes": 1}), scope)


def test_root_only_does_not_gain_tool_capabilities_from_inventory_or_proof():
    rows, contract, scope = prepared()
    result = assemble(rows[:1], contract, scope)
    assert result.completeness.status == "ROOT_ONLY"
    assert all(value == "NONE" for value in result.completeness.capabilities.values())


def test_missing_parent_and_conflict_rejected_component_lower_coverage_without_erasing_good_tools():
    rows, contract, scope = prepared()
    extra = deepcopy(rows[1]) | {"id": "orphan", "parentObservationId": "not-collected"}
    result = assemble(rows + [extra], contract, scope)
    assert result.completeness.status == "PARTIAL"
    assert result.completeness.capabilities["tool_executions"] == "PARTIAL"
    assert "GRAPH_PARENT_MISSING" in result.completeness.warnings and "exec-b" in by_id(result)


def test_inventory_proves_zero_tools_and_checks_each_workflow_independently():
    rows, contract, scope = prepared()
    terminal = rows[-1]
    terminal["metadata"].update({"attributes.workflow.sequence": 1, "attributes.workflow.event_count": 1,
        "attributes.workflow.event_ids": [f"{5:032x}"], "attributes.workflow.final_sequence": 1})
    zero = assemble([rows[0], terminal], contract, scope)
    assert zero.completeness.status == "OBSERVED_COMPLETE"
    assert zero.completeness.capabilities["tool_executions"] == "COMPLETE"
    second = deepcopy(terminal) | {"id": "second-workflow"}
    second["metadata"]["attributes.workflow.scope_id"] = "f" * 32
    both = assemble([rows[0], terminal, second], contract, scope)
    assert both.completeness.status == "OBSERVED_COMPLETE"
    second["metadata"]["attributes.workflow.event_ids"] = ["e" * 32]
    assert assemble([rows[0], terminal, second], contract, scope).completeness.status == "PARTIAL"


def test_structural_wrappers_with_none_capabilities_are_not_inventory_or_business_roles():
    rows, contract, scope = prepared()
    payload = contract.model_dump()
    profile = next(profile for profile in payload["source_profile_set"] if profile["profile_id"] == "native-otel-v1")
    profile["structural_roles"] = [{"provider_type": "SPAN", "raw_name": "business-wrapper", "step_type": "AGENT",
        "safe_name": "business-wrapper", "metadata_equals": {"source": "safe-fixture"}}]
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    wrapper = deepcopy(rows[0]) | {"id": "wrapper", "type": "SPAN", "name": "business-wrapper",
                                   "parentObservationId": "root", "metadata": {"source": "safe-fixture"}}
    for row in rows[1:]:
        row["parentObservationId"] = "wrapper"
    result = assemble(rows + [wrapper], contract, scope)
    assert result.completeness.status == "OBSERVED_COMPLETE"
    assert by_id(result)["wrapper"].tool_identity is None
    assert by_id(result)["exec-a"].parent_step_id == "wrapper"


def test_contradictory_collected_execution_arguments_are_not_replaced_by_the_request():
    rows, contract, scope = prepared()
    rows[2]["input"] = {"customer_id": "synthetic-different"}
    rows[2]["metadata"]["attributes.workflow.arguments_visibility"] = "AVAILABLE"
    result = assemble(rows, contract, scope)
    assert by_id(result)["exec-a"].arguments_redacted == {"customer_id": "synthetic-different"}
    assert result.completeness.capabilities["tool_arguments"] == "PARTIAL"
    assert "ARGUMENTS_PAIR_CONFLICT" in result.completeness.warnings


def test_result_is_owned_and_invalid_input_cannot_bypass_full_contract_checks():
    rows, contract, scope = prepared()
    normalized = batch(rows, contract, scope)
    result = assembler.assemble_trajectory(scope, normalized, contract)
    normalized.records[1].observation.step.arguments_redacted["customer_id"] = "mutated-after"
    assert by_id(result)["exec-a"].arguments_redacted == {"customer_id": "synthetic-001"}
    with pytest.raises(ValueError, match="CONVERGENCE_BATCH_MISMATCH"):
        assembler.assemble_trajectory(scope, normalized, contract)
    valid_batch = batch(rows, contract, scope)
    contract.required_capabilities.append("invalid")
    with pytest.raises(ValueError):
        assembler.assemble_trajectory(scope, valid_batch, contract)


def test_nonterminal_event_ordering_uncertainty_is_not_ignored_as_inventory_metadata():
    rows, contract, scope = prepared()
    progress = event(rows[1], "progress", 5, "TERMINAL", "4" * 32)
    progress.update(type="SPAN", name="workflow.terminal", startTime=None)
    terminal = event(rows[1], "terminal", 6, "TERMINAL", "3" * 32)
    terminal.update(type="SPAN", name="workflow.terminal")
    terminal["metadata"].update({"attributes.workflow.event_count": 6,
        "attributes.workflow.event_ids": [f"{n:032x}" for n in range(1, 7)], "attributes.workflow.final_sequence": 6})
    result = assemble(rows[:-1] + [progress, terminal], contract, scope)
    assert result.completeness.capabilities["ordering"] == "PARTIAL"
    assert result.completeness.status == "PARTIAL"


def test_truncation_to_a_root_display_is_partial_and_not_a_genuinely_root_only_source():
    rows, contract, scope = prepared()
    result = assemble(rows, configured(contract, limits={"max_steps": 1}), scope)
    assert [step.step_id for step in result.steps] == ["root"]
    assert result.completeness.status == "PARTIAL"
    assert "GRAPH_STEP_LIMIT" in result.completeness.warnings
    assert all(value == "NONE" for value in result.completeness.capabilities.values())
