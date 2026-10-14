from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import TrajectoryContract
from app.trajectory.normalizer import normalize_observation
from app.trajectory.schema import TrajectoryScope
from test_trajectory_contract import version
from trajectory_contract_fixtures import contract_payload

TRACE = "a" * 32
WORKFLOW = "b" * 32
EVENT = "c" * 32
CALL = "d" * 32


def inputs(profile_id="native-otel-v1"):
    payload = contract_payload()
    for profile in payload["source_profile_set"]:
        profile["fields"]["tool_raw_name"] = [["metadata", "attributes.gen_ai.tool.name"]]
        profile["safe_names"].update({"workflow.tool.requested": "workflow.tool.requested"})
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version(profile_id))
    scope = TrajectoryScope(
        launch_id="launch",
        item_execution_id="item",
        dataset_item_id="case",
        attempt_id="attempt",
        dispatch_generation=1,
        trace_id=TRACE,
        root_observation_id="root",
    )
    row = {
        "id": "request",
        "traceId": TRACE,
        "parentObservationId": "root",
        "type": "TOOL",
        "name": "workflow.tool.requested",
        "startTime": "2026-10-13T12:00:00.000001+08:00",
        "endTime": "2026-10-13T12:00:00.000002+08:00",
        "input": '{"customer_id":"synthetic-001","token":"synthetic-secret"}',
        "output": '{"balance":9007199254740993.00000000000000001,"Authorization":"synthetic-secret"}',
        "metadata": {
            "attributes.workflow.source": "business-instrumentation-v1",
            "attributes.workflow.phase": "REQUESTED",
            "attributes.workflow.status": "SUCCEEDED",
            "attributes.gen_ai.tool.name": "crm_lookup" if profile_id == "native-otel-v1" else "account_crm_lookup",
            "attributes.workflow.scope_id": WORKFLOW,
            "attributes.workflow.event_id": EVENT,
            "attributes.workflow.call_id": CALL,
            "attributes.workflow.sequence": 1,
            "attributes.workflow.arguments_visibility": "AVAILABLE",
            "attributes.workflow.predecessor_event_ids": "[]",
            "attributes.workflow.duration_ms": Decimal("1.001"),
        },
    }
    return row, contract, scope


@pytest.mark.parametrize("profile_id", ["native-otel-v1", "pydantic-ai-v1"])
def test_explicit_fields_produce_same_semantics_and_minimal_exact_evidence(profile_id):
    row, contract, scope = inputs(profile_id)
    result = normalize_observation(row, contract, scope, index=1)
    step = result.step
    assert step.tool_identity == "banking.customer.lookup"
    assert step.phase == "REQUESTED" and step.status == "SUCCEEDED"
    assert step.started_at == datetime(2026, 10, 13, 4, 0, 0, 1, tzinfo=UTC)
    assert step.duration_ms == Decimal("1.001")
    assert step.arguments_redacted == {"customer_id": "synthetic-001"}
    assert step.argument_visibility == {"$.customer_id": "AVAILABLE"}
    assert step.result_summary == {"balance": Decimal("9007199254740993.00000000000000001")}
    assert step.causal_predecessor_ids == []
    assert result.event_id == EVENT and result.call_id == CALL and result.sequence == 1
    encoded = canonical_dumps(result)
    assert "synthetic-secret" not in encoded and "metadata" not in encoded
    assert "9007199254740993.00000000000000001" in encoded
    row["input"] = "changed"
    assert step.arguments_redacted == {"customer_id": "synthetic-001"}


@pytest.mark.parametrize("field,value", [("id", None), ("id", 1), ("traceId", "f" * 32), ("parentObservationId", 3)])
def test_bad_identity_or_other_trace_cannot_form_step(field, value):
    row, contract, scope = inputs()
    row[field] = value
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step is None and result.warnings
    assert all(value == "NONE" for value in result.capabilities.values())


def test_missing_parent_is_not_inferred_as_root():
    row, contract, scope = inputs()
    del row["parentObservationId"]
    assert normalize_observation(row, contract, scope, index=1).step is None


def test_conflicting_aliases_drop_record_instead_of_selecting_a_winner():
    row, contract, scope = inputs()
    payload = contract.model_dump()
    payload["source_profile_set"][0]["fields"]["raw_name"].append(["legacyName"])
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    row["legacyName"] = "different"
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step is None and "SOURCE_FIELD_ALIAS_CONFLICT" in result.warnings
    row["legacyName"] = row["name"]
    assert normalize_observation(row, contract, scope, index=1).step is not None


@pytest.mark.parametrize("field", ["attributes.workflow.phase", "attributes.workflow.status"])
def test_tool_type_and_successful_http_do_not_supply_missing_business_semantics(field):
    row, contract, scope = inputs()
    del row["metadata"][field]
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step is None and "STEP_SEMANTICS_UNAVAILABLE" in result.warnings


@pytest.mark.parametrize("name", ["crm_lookup", "workflow.tool.requested"])
def test_unapproved_source_predicate_does_not_certify_business_event(name):
    row, contract, scope = inputs()
    row["name"] = name
    row["metadata"]["attributes.workflow.source"] = "untrusted"
    assert normalize_observation(row, contract, scope, index=1).step is None


def test_unknown_tool_identity_and_unapproved_free_text_name_remain_safe():
    row, contract, scope = inputs()
    row["name"] = "synthetic-secret"
    row["metadata"]["attributes.gen_ai.tool.name"] = "unknown-tool"
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.tool_identity is None
    assert result.step.raw_name == "unapproved-name"
    assert "TOOL_MAPPING_UNRESOLVED" in result.warnings
    assert "synthetic-secret" not in canonical_dumps(result)


@pytest.mark.parametrize("duration", [None, -1, True, 1.5, "1.5", Decimal("NaN")])
def test_invalid_explicit_duration_only_degrades_timing(duration):
    row, contract, scope = inputs()
    row["metadata"]["attributes.workflow.duration_ms"] = duration
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.duration_ms is None
    assert result.step.tool_identity == "banking.customer.lookup"
    assert result.capabilities["timing"] == "NONE"


def test_time_errors_never_insert_current_time_or_derive_duration():
    row, contract, scope = inputs()
    row["startTime"] = "2026-10-13T12:00:00"
    row["endTime"] = "invalid"
    del row["metadata"]["attributes.workflow.duration_ms"]
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.started_at is None and result.step.ended_at is None
    assert result.step.duration_ms is None and result.capabilities["ordering"] == "PARTIAL"


@pytest.mark.parametrize(
    "input_value,visibility",
    [(None, {}), ("{broken", {"$.customer_id": "INVALID"}), ("{}", {"$.customer_id": "MISSING"})],
)
def test_uncollected_invalid_and_observed_missing_arguments_are_distinct(input_value, visibility):
    row, contract, scope = inputs()
    row["input"] = input_value
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.argument_visibility == visibility
    if input_value is None:
        assert result.capabilities["tool_arguments"] == "NONE"


def test_hidden_argument_is_not_a_literal_mask_or_missing_truth():
    row, contract, scope = inputs()
    row["metadata"]["attributes.workflow.arguments_visibility"] = "REDACTED"
    row["metadata"]["attributes.workflow.hidden_paths"] = '["$.customer_id"]'
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.arguments_redacted == {}
    assert result.step.argument_visibility == {"$.customer_id": "REDACTED"}
    assert "synthetic-001" not in canonical_dumps(result)


def test_explicit_predecessor_is_retained_as_event_ref_without_parent_or_index_guess():
    row, contract, scope = inputs()
    row["metadata"]["attributes.workflow.predecessor_event_ids"] = '["' + "e" * 32 + '"]'
    result = normalize_observation(row, contract, scope, index=99)
    assert result.causal_event_ids == ["e" * 32]
    assert result.step.causal_predecessor_ids == []
    assert result.step.parent_step_id == "root"


def test_terminal_inventory_is_safe_and_available_for_later_closure_validation():
    row, contract, scope = inputs()
    row["metadata"].update(
        {
            "attributes.workflow.phase": "TERMINAL",
            "attributes.workflow.event_count": 1,
            "attributes.workflow.event_ids": '["' + EVENT + '"]',
            "attributes.workflow.final_sequence": 1,
        }
    )
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.phase == "EVENT"
    assert result.terminal_event_count == 1 and result.terminal_event_ids == [EVENT]
    assert result.terminal_final_sequence == 1
    assert result.step.result_summary["terminal_inventory"] == {
        "event_count": 1,
        "event_ids": [EVENT],
        "final_sequence": 1,
    }


def test_expected_http_root_has_fixed_safe_semantics_but_does_not_prove_business_tools():
    row, contract, scope = inputs()
    row.update(id="root", parentObservationId="outer", type="SPAN", name="remote-agent-http", metadata={})
    result = normalize_observation(row, contract, scope, index=0)
    assert result.step.type == "CUSTOM" and result.step.phase == "EXECUTED"
    assert result.step.status == "UNKNOWN" and result.step.raw_name == "remote-agent-http"
    assert result.step.parent_step_id == "outer"
    assert result.capabilities["tool_executions"] == "NONE"
    wrong = deepcopy(row)
    wrong["name"] = "crm_lookup"
    assert normalize_observation(wrong, contract, scope, index=0).step is None


def test_http_root_uses_input_output_policy_instead_of_tool_parameter_policy():
    row, contract, scope = inputs()
    row.update(
        id="root",
        type="SPAN",
        name="remote-agent-http",
        metadata={},
        output='{"intent":"balance","balance":3,"token":"synthetic-secret"}',
    )
    result = normalize_observation(row, contract, scope, index=0)
    assert result.step.arguments_redacted == {}
    assert result.step.argument_visibility == {}
    assert result.step.result_summary == {"intent": "balance"}
    assert "synthetic-001" not in canonical_dumps(result)


def test_duration_respects_reduced_numeric_budget_without_dropping_tool_identity():
    row, contract, scope = inputs()
    payload = contract.model_dump()
    payload["limits"]["max_numeric_digits"] = 2
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    row["metadata"]["attributes.workflow.duration_ms"] = 123
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.tool_identity == "banking.customer.lookup"
    assert result.step.duration_ms is None and result.capabilities["timing"] == "NONE"


def test_source_predicate_never_invokes_arbitrary_object_comparison():
    class UnsafeSdkObject:
        def __ne__(self, _other):
            raise AssertionError("object comparison must not execute")

    row, contract, scope = inputs()
    row["metadata"]["attributes.workflow.source"] = UnsafeSdkObject()
    assert normalize_observation(row, contract, scope, index=1).step is None


def test_reduced_string_budget_applies_to_safe_names_and_workflow_references():
    row, contract, scope = inputs()
    payload = contract.model_dump()
    payload["limits"]["max_string_bytes"] = 3
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step is None


@pytest.mark.parametrize("marker", [{}, []])
def test_invalid_source_visibility_cannot_interrupt_batch_normalization(marker):
    row, contract, scope = inputs()
    row["metadata"]["attributes.workflow.arguments_visibility"] = marker
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.argument_visibility == {"$.customer_id": "INVALID"}
    assert result.capabilities["tool_arguments"] == "PARTIAL"


def test_invalid_output_is_diagnosable_without_persisting_raw_output():
    row, contract, scope = inputs()
    row["output"] = "{broken-synthetic-secret"
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.result_summary == {}
    assert "OUTPUT_SUMMARY_UNAVAILABLE" in result.warnings
    assert "synthetic-secret" not in canonical_dumps(result)


def test_execution_without_input_or_visibility_marker_is_uncollected_instead_of_invalid_fields():
    row, contract, scope = inputs()
    row["metadata"]["attributes.workflow.phase"] = "EXECUTED"
    del row["metadata"]["attributes.workflow.arguments_visibility"]
    row["input"] = None
    result = normalize_observation(row, contract, scope, index=1)
    assert result.step.phase == "EXECUTED"
    assert result.step.arguments_redacted is None and result.step.argument_visibility == {}
    assert result.capabilities["tool_arguments"] == "NONE"
