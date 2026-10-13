from copy import deepcopy

import pytest
from app.trajectory import graph
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import TrajectoryContract
from app.trajectory.normalizer import normalize_observations
from test_trajectory_contract import version
from test_trajectory_normalized_batches import batch_inputs


def node(row, identity, parent, **changes):
    value = deepcopy(row)
    value.update(id=identity, parentObservationId=parent, **changes)
    return value


def assemble(rows, contract, scope):
    batch = normalize_observations(rows, contract, scope=scope)
    return graph.assemble_root_graph(scope, batch, contract)


def ids(result):
    return [record.observation_id for record in result.records if record.observation.step is not None]


def semantic(result):
    return canonical_dumps(result.model_dump(exclude={"deduped"}))


def test_frozen_root_closure_keeps_real_parent_depth_without_creating_causality():
    rows, contract, scope = batch_inputs()
    rows += [node(rows[1], "child", "request")]
    result = assemble(rows, contract, scope)
    assert result.scope == scope and result.trajectory_contract_digest == contract.digest
    assert result.source_profile_id == contract.source_profile.profile_id
    assert result.source_profile_version == contract.source_profile.version
    assert result.depth_by_id == {"root": 0, "request": 1, "child": 2}
    assert ids(result) == ["root", "request", "child"]
    assert all(record.observation.step.causal_predecessor_ids == [] for record in result.records)
    assert not result.truncated and result.deduped == 0


def test_sibling_item_and_older_attempt_tree_diagnostics_are_excluded():
    rows, contract, scope = batch_inputs()
    expected = assemble(rows, contract, scope)
    sibling = node(rows[0], "old-root", "outer", metadata={})
    sibling_bad = node(rows[1], "old-tool", "old-root", metadata={})
    foreign = node(rows[1], "foreign", "foreign-root", traceId="f" * 32)
    result = assemble(rows + [sibling, sibling_bad, foreign], contract, scope)
    assert semantic(result) == semantic(expected)


def test_identical_duplicates_and_arrival_order_do_not_change_graph_semantics():
    rows, contract, scope = batch_inputs()
    expected = assemble(rows, contract, scope)
    result = assemble([rows[1], rows[0], deepcopy(rows[1]), deepcopy(rows[0])], contract, scope)
    assert result.deduped == 2 and semantic(result) == semantic(expected)


def test_conflicting_sibling_parents_are_isolated_when_every_variant_is_proven_outside():
    rows, contract, scope = batch_inputs()
    expected = assemble(rows, contract, scope)
    siblings = [node(rows[0], "brother-a", "outer"), node(rows[0], "brother-b", None),
                node(rows[1], "bad-sibling", "brother-a"), node(rows[1], "bad-sibling", "brother-b")]
    result = assemble(rows + siblings, contract, scope)
    assert semantic(result) == semantic(expected)
    assert semantic(assemble(list(reversed(rows + siblings)), contract, scope)) == semantic(expected)


def test_conflicting_evidence_has_no_winner_but_independent_tool_remains():
    rows, contract, scope = batch_inputs()
    conflict = deepcopy(rows[1])
    conflict["input"] = '{"customer_id":"other-synthetic"}'
    rows += [conflict, node(rows[1], "independent", "root")]
    result = assemble(rows, contract, scope)
    assert set(ids(result)) == {"root", "independent"}
    assert "GRAPH_ID_CONFLICT" in result.warnings
    rejected = next(record for record in result.records if record.observation_id == "request")
    assert rejected.parent_known and rejected.parent_step_id == "root" and rejected.observation.step is None
    assert semantic(result) == semantic(assemble(list(reversed(rows)), contract, scope))


def test_conflicting_parent_does_not_choose_current_attempt_as_a_winner():
    rows, contract, scope = batch_inputs()
    result = assemble(rows + [node(rows[1], "request", "sibling-root")], contract, scope)
    assert ids(result) == ["root"]
    assert "GRAPH_ID_CONFLICT" in result.warnings
    assert "request" not in result.depth_by_id


def test_conflicted_bridge_with_consistent_parent_keeps_independent_descendant():
    rows, contract, scope = batch_inputs()
    conflict = deepcopy(rows[1])
    conflict["input"] = '{"customer_id":"other-synthetic"}'
    result = assemble(rows + [conflict, node(rows[1], "child", "request")], contract, scope)
    assert ids(result) == ["root", "child"]
    assert result.depth_by_id["child"] == 2 and "GRAPH_ID_CONFLICT" in result.warnings


def test_rejected_scoped_parent_retains_lineage_and_descendant_evidence():
    rows, contract, scope = batch_inputs()
    child = node(rows[1], "child", "request")
    rows[1]["metadata"].pop("attributes.workflow.phase")
    result = assemble(rows + [child], contract, scope)
    assert ids(result) == ["root", "child"]
    assert result.depth_by_id == {"root": 0, "request": 1, "child": 2}
    assert "STEP_SEMANTICS_UNAVAILABLE" in result.warnings


@pytest.mark.parametrize("kind,reason", [("missing", "GRAPH_PARENT_MISSING"), ("unknown", "GRAPH_PARENT_UNAVAILABLE"),
                                         ("cycle", "GRAPH_PARENT_CYCLE")])
def test_unknown_components_are_reported_without_erasing_independent_evidence(kind, reason):
    rows, contract, scope = batch_inputs()
    extra = node(rows[1], "orphan", "absent")
    if kind == "unknown":
        extra.pop("parentObservationId")
    elif kind == "cycle":
        extra["parentObservationId"] = "cycle"
        rows += [node(rows[1], "cycle", "orphan")]
    result = assemble(rows + [extra], contract, scope)
    assert ids(result) == ["root", "request"] and reason in result.warnings


def test_no_id_is_not_fabricated_or_silently_treated_as_outside():
    rows, contract, scope = batch_inputs()
    extra = node(rows[1], 123, "root")
    result = assemble(rows + [extra], contract, scope)
    assert ids(result) == ["root", "request"]
    assert "GRAPH_IDENTITY_UNAVAILABLE" in result.warnings


def test_cross_trace_child_is_not_pulled_in_by_parent_id():
    rows, contract, scope = batch_inputs()
    result = assemble(rows + [node(rows[1], "foreign", "root", traceId="f" * 32)], contract, scope)
    assert ids(result) == ["root", "request"] and "GRAPH_TRACE_SCOPE_MISMATCH" in result.warnings


@pytest.mark.parametrize("trace", [None, "invalid-trace", "0" * 32])
def test_unknown_trace_with_unprovable_parent_is_not_declared_outside(trace):
    rows, contract, scope = batch_inputs()
    extra = node(rows[1], "unknown", "absent")
    if trace is None:
        extra.pop("traceId")
    else:
        extra["traceId"] = trace
    result = assemble(rows + [extra], contract, scope)
    assert ids(result) == ["root", "request"]
    assert "GRAPH_TRACE_SCOPE_UNAVAILABLE" in result.warnings


def test_conflicting_trace_for_same_observation_id_cannot_be_cherry_picked():
    rows, contract, scope = batch_inputs()
    result = assemble(rows + [node(rows[1], "request", "root", traceId="f" * 32)], contract, scope)
    assert ids(result) == ["root"] and "GRAPH_ID_CONFLICT" in result.warnings


@pytest.mark.parametrize("kind,reason", [("missing", "TRAJECTORY_ROOT_UNAVAILABLE"),
                                         ("conflict", "TRAJECTORY_ROOT_CONFLICT"),
                                         ("cycle", "TRAJECTORY_ROOT_PARENT_CYCLE")])
def test_untrustworthy_root_fails_explicitly_instead_of_returning_fake_graph(kind, reason):
    rows, contract, scope = batch_inputs()
    if kind == "missing":
        rows.pop(0)
    elif kind == "conflict":
        rows += [node(rows[0], "root", "other")]
    else:
        rows[0]["parentObservationId"] = "request"
    with pytest.raises(ValueError, match=reason):
        assemble(rows, contract, scope)


def test_sort_uses_time_null_depth_id_and_never_invents_sibling_order():
    rows, contract, scope = batch_inputs()
    rows += [node(rows[1], "early", "root", startTime="2026-01-01T00:00:00Z"),
             node(rows[1], "null", "root", startTime=None)]
    result = assemble(list(reversed(rows)), contract, scope)
    assert ids(result)[0] == "early" and ids(result)[-1] == "null"
    assert "GRAPH_CLOCK_SKEW" in result.warnings
    assert all(record.observation.step.causal_predecessor_ids == [] for record in result.records)


def test_step_limit_reserves_late_root_after_dedup_and_is_arrival_independent():
    rows, contract, scope = batch_inputs(max_steps=1)
    rows[0]["startTime"] = "2027-01-01T00:00:00Z"
    result = assemble([rows[1]] * 20 + [rows[0]], contract, scope)
    assert ids(result) == ["root"] and result.deduped == 19 and result.truncated
    assert "GRAPH_STEP_LIMIT" in result.warnings
    assert semantic(result) == semantic(assemble([rows[0]] + [rows[1]] * 20, contract, scope))


@pytest.mark.parametrize("field,value", [("attempt_id", "other"), ("item_execution_id", "other"),
                                         ("launch_id", "other"), ("dispatch_generation", 2)])
def test_consumer_rechecks_full_scope_not_only_trace_id(field, value):
    rows, contract, scope = batch_inputs()
    batch = normalize_observations(rows, contract, scope=scope)
    with pytest.raises(ValueError, match="TRAJECTORY_GRAPH_SCOPE_MISMATCH"):
        graph.assemble_root_graph(scope.model_copy(update={field: value}), batch, contract)


def test_consumer_rechecks_contract_profile_and_limits():
    rows, contract, scope = batch_inputs(page_size=2, max_pages=1, max_source_bytes=4096)
    batch = normalize_observations(rows, contract, scope=scope)
    for updates, reason in [({"source_profile_version": "other"}, "TRAJECTORY_GRAPH_SOURCE_MISMATCH"),
                            ({"source_bytes": 4097}, "TRAJECTORY_GRAPH_SOURCE_LIMIT"),
                            ({"records": batch.records + [batch.records[1]]}, "TRAJECTORY_GRAPH_SOURCE_LIMIT")]:
        with pytest.raises(ValueError, match=reason):
            graph.assemble_root_graph(scope, batch.model_copy(update=updates), contract)
    payload = contract.model_dump()
    payload["redaction_policy"]["version"] = "different"
    changed = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    with pytest.raises(ValueError, match="TRAJECTORY_GRAPH_CONTRACT_MISMATCH"):
        graph.assemble_root_graph(scope, batch, changed)


def test_input_truncation_survives_root_closure():
    rows, contract, scope = batch_inputs(page_size=1, max_pages=1)
    result = assemble(rows, contract, scope)
    assert result.truncated and "SOURCE_ROW_LIMIT" in result.warnings


def test_iterative_parent_walk_handles_maximum_deep_chain_without_recursion():
    rows, contract, scope = batch_inputs(max_steps=1000)
    template = rows.pop()
    for index in range(999):
        rows += [node(template, f"n{index:04}", "root" if index == 0 else f"n{index-1:04}")]
    result = assemble(list(reversed(rows)), contract, scope)
    assert len(ids(result)) == 1000 and result.depth_by_id["n0998"] == 999
    assert not result.truncated


def test_graph_owns_records_and_does_not_store_original_source_json():
    rows, contract, scope = batch_inputs()
    batch = normalize_observations(rows, contract, scope=scope)
    result = graph.assemble_root_graph(scope, batch, contract)
    batch.records[1].observation.step.argument_visibility.clear()
    assert result.records[1].observation.step.argument_visibility == {"$.customer_id": "AVAILABLE"}
    assert "synthetic-secret" not in canonical_dumps(result) and "attributes.workflow" not in canonical_dumps(result)
