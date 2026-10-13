from copy import deepcopy

import pytest
from app.trajectory import causal
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import TrajectoryContract
from app.trajectory.normalizer import normalize_observations
from test_trajectory_contract import version
from test_trajectory_normalized_batches import batch_inputs


def event(template, identity, sequence, phase, call, predecessors=()):
    row = deepcopy(template)
    row.update(id=identity, startTime=f"2026-10-13T04:00:{sequence:02d}Z",
               endTime=f"2026-10-13T04:00:{sequence:02d}.500000Z")
    row["metadata"].update({"attributes.workflow.event_id": f"{sequence:032x}",
                            "attributes.workflow.sequence": sequence,
                            "attributes.workflow.phase": phase, "attributes.workflow.call_id": call,
                            "attributes.workflow.predecessor_event_ids": [f"{index:032x}" for index in predecessors]})
    if phase != "REQUESTED":
        row["input"] = None
        row["metadata"].pop("attributes.workflow.arguments_visibility")
    return row


def inputs():
    rows, contract, scope = batch_inputs()
    rows[0].update(startTime="2026-10-13T04:00:00Z", endTime="2026-10-13T04:00:10Z")
    template = rows.pop()
    rows += [event(template, "req-a", 1, "REQUESTED", "1" * 32),
             event(template, "exec-a", 2, "EXECUTED", "1" * 32, [1]),
             event(template, "req-b", 3, "REQUESTED", "2" * 32, [2]),
             event(template, "exec-b", 4, "EXECUTED", "2" * 32, [3])]
    return rows, contract, scope


def validate(rows, contract, scope):
    return causal.validate_causal_links(scope, normalize_observations(rows, contract, scope=scope), contract)


def test_explicit_event_dependencies_resolve_to_scoped_observation_ids_and_exact_pairs():
    rows, contract, scope = inputs()
    result = validate(rows, contract, scope)
    assert result.predecessors_by_id == {"root": [], "req-a": [], "exec-a": ["req-a"],
                                         "req-b": ["exec-a"], "exec-b": ["req-b"]}
    assert result.request_by_execution_id == {"exec-a": "req-a", "exec-b": "req-b"}
    assert result.invalid_ordering_ids == [] and result.warnings_by_id == {}
    assert result.root_graph.scope == scope
    assert canonical_dumps(result) == canonical_dumps(validate(list(reversed(rows)), contract, scope))
    execution = next(record for record in result.root_graph.records if record.observation_id == "exec-a")
    assert execution.observation.step.arguments_redacted is None  # 参数传递由后续投影处理。


def test_parallel_siblings_and_monotonic_sequence_never_generate_missing_links():
    rows, contract, scope = inputs()
    rows[3]["metadata"]["attributes.workflow.predecessor_event_ids"] = []
    result = validate(rows, contract, scope)
    assert result.predecessors_by_id["req-b"] == []
    assert result.predecessors_by_id["exec-b"] == ["req-b"]
    assert result.request_by_execution_id == {"exec-a": "req-a", "exec-b": "req-b"}


@pytest.mark.parametrize("change,reason", [("missing", "CAUSAL_REFERENCE_UNAVAILABLE"),
                                          ("workflow", "CAUSAL_REFERENCE_UNAVAILABLE"),
                                          ("outside", "CAUSAL_REFERENCE_UNAVAILABLE"),
                                          ("self", "CAUSAL_CYCLE"),
                                          ("time", "CAUSAL_TIME_CONFLICT"),
                                          ("unknown-time", "CAUSAL_TIME_UNAVAILABLE"),
                                          ("sequence", "CAUSAL_SEQUENCE_CONFLICT")])
def test_untrusted_edge_does_not_pull_or_guess_a_predecessor(change, reason):
    rows, contract, scope = inputs()
    if change == "missing":
        rows[3]["metadata"]["attributes.workflow.predecessor_event_ids"] = ["e" * 32]
    elif change == "workflow":
        rows[2]["metadata"]["attributes.workflow.scope_id"] = "e" * 32
    elif change == "outside":
        rows[2]["parentObservationId"] = None
    elif change == "self":
        rows[3]["metadata"]["attributes.workflow.predecessor_event_ids"] = [f"{3:032x}"]
    elif change == "time":
        rows[2]["endTime"] = "2026-10-13T04:00:03.500000Z"
    elif change == "unknown-time":
        rows[2]["endTime"] = None
    else:
        rows[2]["metadata"]["attributes.workflow.sequence"] = 5
    result = validate(rows, contract, scope)
    assert result.predecessors_by_id["req-b"] == []
    assert reason in result.warnings_by_id["req-b"] and "req-b" in result.invalid_ordering_ids


def test_duplicate_event_identity_has_no_winner_and_independent_event_edge_survives():
    rows, contract, scope = inputs()
    extra = event(rows[1], "dup", 5, "REQUESTED", "3" * 32)
    extra["metadata"]["attributes.workflow.event_id"] = f"{1:032x}"
    result = validate(rows + [extra], contract, scope)
    assert result.predecessors_by_id["exec-a"] == []
    assert result.predecessors_by_id["req-b"] == ["exec-a"]
    assert result.predecessors_by_id["exec-b"] == ["req-b"]
    assert "CAUSAL_EVENT_CONFLICT" in result.warnings_by_id["exec-a"]
    assert "exec-a" not in result.request_by_execution_id
    assert canonical_dumps(result) == canonical_dumps(validate(list(reversed(rows + [extra])), contract, scope))


def test_duplicate_sequence_cannot_be_repaired_by_sorting_or_event_identity():
    rows, contract, scope = inputs()
    rows[2]["metadata"]["attributes.workflow.sequence"] = 3
    result = validate(rows, contract, scope)
    assert result.predecessors_by_id["exec-a"] == [] and result.predecessors_by_id["req-b"] == []
    assert "CAUSAL_SEQUENCE_CONFLICT" in result.warnings_by_id["exec-a"]


def test_raw_cycle_is_rejected_as_a_whole_before_choosing_sequence_consistent_edges():
    rows, contract, scope = inputs()
    rows[1]["metadata"]["attributes.workflow.predecessor_event_ids"] = [f"{4:032x}"]
    result = validate(rows, contract, scope)
    assert all(result.predecessors_by_id[identity] == [] for identity in ["req-a", "exec-a", "req-b", "exec-b"])
    assert all("CAUSAL_CYCLE" in result.warnings_by_id[identity]
               for identity in ["req-a", "exec-a", "req-b", "exec-b"])
    assert result.request_by_execution_id == {}


@pytest.mark.parametrize("change", ["call", "tool", "extra-request", "extra-execution", "missing-link"])
def test_pair_requires_unambiguous_same_workflow_call_tool_and_valid_request_link(change):
    rows, contract, scope = inputs()
    if change == "call":
        rows[2]["metadata"]["attributes.workflow.call_id"] = "3" * 32
    elif change == "tool":
        rows[2]["metadata"]["attributes.gen_ai.tool.name"] = "unknown-tool"
    elif change == "extra-request":
        rows += [event(rows[1], "extra", 5, "REQUESTED", "1" * 32)]
    elif change == "extra-execution":
        rows += [event(rows[1], "extra", 5, "EXECUTED", "1" * 32, [1])]
    else:
        rows[2]["metadata"]["attributes.workflow.predecessor_event_ids"] = []
    result = validate(rows, contract, scope)
    assert "exec-a" not in result.request_by_execution_id
    assert result.request_by_execution_id.get("exec-b") == "req-b"
    assert result.root_graph.records[0].observation_id == "root"


def test_blocked_action_can_bind_request_but_does_not_become_executed():
    rows, contract, scope = inputs()
    rows[2]["metadata"]["attributes.workflow.phase"] = "BLOCKED"
    result = validate(rows, contract, scope)
    assert result.request_by_execution_id["exec-a"] == "req-a"
    record = next(record for record in result.root_graph.records if record.observation_id == "exec-a")
    assert record.observation.step.phase == "BLOCKED"


def test_source_none_or_incomplete_event_markers_cannot_attest_ordering():
    rows, contract, scope = inputs()
    payload = contract.model_dump()
    payload["source_profile_set"][0]["capabilities"]["ordering"] = "NONE"
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    result = validate(rows, contract, scope)
    assert all(not predecessors for predecessors in result.predecessors_by_id.values())
    assert result.request_by_execution_id == {}
    assert "CAUSAL_SOURCE_UNAVAILABLE" in result.warnings_by_id["exec-a"]


def test_equal_sequence_in_different_workflow_is_not_a_conflict_or_cross_workflow_link():
    rows, contract, scope = inputs()
    other = event(rows[1], "other-workflow", 1, "REQUESTED", "1" * 32)
    other["metadata"]["attributes.workflow.scope_id"] = "e" * 32
    result = validate(rows + [other], contract, scope)
    assert result.warnings_by_id == {} and result.request_by_execution_id["exec-a"] == "req-a"


def test_call_pair_with_redacted_request_only_proves_identity_does_not_restore_parameters():
    rows, contract, scope = inputs()
    rows[1]["metadata"].update({"attributes.workflow.arguments_visibility": "REDACTED",
                                "attributes.workflow.hidden_paths": ["$.customer_id"]})
    rows[1]["input"] = None
    result = validate(rows, contract, scope)
    assert result.request_by_execution_id["exec-a"] == "req-a"
    request = next(record for record in result.root_graph.records if record.observation_id == "req-a")
    assert request.observation.step.argument_visibility == {"$.customer_id": "REDACTED"}
    assert "synthetic-secret" not in canonical_dumps(result)


def test_pair_result_is_owned_and_full_scope_and_contract_are_still_validated():
    rows, contract, scope = inputs()
    batch = normalize_observations(rows, contract, scope=scope)
    result = causal.validate_causal_links(scope, batch, contract)
    batch.records[1].observation.causal_event_ids.append("e" * 32)
    assert result.predecessors_by_id["req-a"] == []
    with pytest.raises(ValueError, match="TRAJECTORY_GRAPH_SCOPE_MISMATCH"):
        causal.validate_causal_links(scope.model_copy(update={"attempt_id": "other"}), batch, contract)


def test_predecessor_list_order_and_duplicate_identical_source_rows_do_not_change_links():
    rows, contract, scope = inputs()
    rows[4]["metadata"]["attributes.workflow.predecessor_event_ids"] = [f"{2:032x}", f"{3:032x}"]
    expected = validate(rows, contract, scope)
    rows[4]["metadata"]["attributes.workflow.predecessor_event_ids"].reverse()
    result = validate(list(reversed(rows)), contract, scope)
    assert canonical_dumps(result) == canonical_dumps(expected)
    result = validate(rows + [deepcopy(rows[2])], contract, scope)
    assert result.predecessors_by_id == expected.predecessors_by_id
    assert result.request_by_execution_id == expected.request_by_execution_id


def test_iterative_cycle_check_handles_thousand_nodes_and_isolates_exact_component():
    edges = {f"n{index}": {f"n{index-1}"} if index else set() for index in range(1000)}
    assert causal._cycle_nodes(edges) == set()
    edges["n997"].add("n999")
    assert causal._cycle_nodes(edges) == {"n997", "n998", "n999"}


def test_partial_source_retains_valid_explicit_edges_without_upgrading_source_coverage():
    rows, contract, scope = inputs()
    payload = contract.model_dump()
    payload["source_profile_set"][0]["capabilities"]["ordering"] = "PARTIAL"
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    result = validate(rows, contract, scope)
    assert result.predecessors_by_id["req-b"] == ["exec-a"]
    assert all(record.observation.capabilities["ordering"] == "PARTIAL" for record in result.root_graph.records
               if record.observation_id != "root")


def test_missing_per_observation_ordering_coverage_cannot_be_attested_from_profile_alone():
    rows, contract, scope = inputs()
    batch = normalize_observations(rows, contract, scope=scope)
    batch.records[1].observation.capabilities.pop("ordering")
    result = causal.validate_causal_links(scope, batch, contract)
    assert "CAUSAL_SOURCE_UNAVAILABLE" in result.warnings_by_id["req-a"]
    assert result.predecessors_by_id["exec-a"] == [] and "exec-a" not in result.request_by_execution_id


def test_unqualified_human_record_is_not_authority_or_a_trusted_predecessor():
    rows, contract, scope = inputs()
    batch = normalize_observations(rows, contract, scope=scope)
    observation = batch.records[1].observation
    batch.records[1] = batch.records[1].model_copy(update={
        "observation": observation.model_copy(update={"step": observation.step.model_copy(update={"type": "HUMAN"})})})
    result = causal.validate_causal_links(scope, batch, contract)
    assert "CAUSAL_HUMAN_UNAVAILABLE" in result.warnings_by_id["req-a"]
    assert result.predecessors_by_id["exec-a"] == []


def test_confirmed_terminal_inventory_without_links_is_not_a_tool_ordering_hole():
    rows, contract, scope = inputs()
    terminal = event(rows[1], "terminal", 5, "TERMINAL", "5" * 32)
    terminal.update(name="workflow.terminal", type="SPAN")
    terminal["metadata"].pop("attributes.workflow.predecessor_event_ids")
    terminal["metadata"].update({"attributes.workflow.event_count": 5,
                                 "attributes.workflow.event_ids": [f"{index:032x}" for index in range(1, 6)],
                                 "attributes.workflow.final_sequence": 5})
    result = validate(rows + [terminal], contract, scope)
    assert result.warnings_by_id == {} and result.invalid_ordering_ids == []
    assert result.predecessors_by_id["terminal"] == [] and len(result.request_by_execution_id) == 2


@pytest.mark.parametrize("field,reason", [("event_id", "CAUSAL_EVENT_CONFLICT"),
                                        ("sequence", "CAUSAL_SEQUENCE_CONFLICT")])
def test_terminal_identity_still_shares_business_event_namespace_without_being_an_order_node(field, reason):
    rows, contract, scope = inputs()
    terminal = event(rows[1], "terminal", 5, "TERMINAL", "5" * 32)
    terminal.update(name="workflow.terminal", type="SPAN")
    terminal["metadata"].pop("attributes.workflow.predecessor_event_ids")
    terminal["metadata"].update({"attributes.workflow.event_count": 5,
                                 "attributes.workflow.event_ids": [f"{index:032x}" for index in range(1, 6)],
                                 "attributes.workflow.final_sequence": 5,
                                 "attributes.workflow." + field: f"{1:032x}" if field == "event_id" else 1})
    result = validate(rows + [terminal], contract, scope)
    assert result.predecessors_by_id["exec-a"] == []
    assert reason in result.warnings_by_id["exec-a"]
    assert "exec-a" not in result.request_by_execution_id


@pytest.mark.parametrize("collision,reason", [("event", "CAUSAL_EVENT_CONFLICT"),
                                            ("sequence", "CAUSAL_SEQUENCE_CONFLICT"), ("call", "CAUSAL_CALL_CONFLICT")])
def test_confirmed_identity_of_ordering_unavailable_peer_cannot_be_ignored_to_pick_a_winner(collision, reason):
    rows, contract, scope = inputs()
    extra = event(rows[1], "extra", 5, "EXECUTED" if collision == "call" else "REQUESTED",
                  "1" * 32 if collision == "call" else "3" * 32)
    extra["metadata"].pop("attributes.workflow.predecessor_event_ids")
    if collision == "event":
        extra["metadata"]["attributes.workflow.event_id"] = f"{1:032x}"
    elif collision == "sequence":
        extra["metadata"]["attributes.workflow.sequence"] = 1
    result = validate(rows + [extra], contract, scope)
    assert reason in result.warnings_by_id["exec-a"]
    assert "exec-a" not in result.request_by_execution_id


def test_declared_invalid_terminal_inventory_does_not_become_a_regular_order_node():
    rows, contract, scope = inputs()
    terminal = event(rows[1], "terminal", 5, "TERMINAL", "5" * 32, [4])
    terminal.update(name="workflow.terminal", type="SPAN")
    terminal["metadata"].update({"attributes.workflow.event_count": 999,
                                 "attributes.workflow.event_ids": [f"{5:032x}"],
                                 "attributes.workflow.final_sequence": 5})
    later = event(rows[1], "later", 6, "REQUESTED", "6" * 32, [5])
    result = validate(rows + [terminal, later], contract, scope)
    assert result.predecessors_by_id["terminal"] == [] and result.predecessors_by_id["later"] == []
    assert "CAUSAL_REFERENCE_UNAVAILABLE" in result.warnings_by_id["later"]
    assert result.request_by_execution_id == {"exec-a": "req-a", "exec-b": "req-b"}
