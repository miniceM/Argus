from copy import deepcopy

import pytest
from app.trajectory import normalizer
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import TrajectoryContract
from app.trajectory.normalizer import NormalizedObservations
from pydantic import BaseModel
from test_trajectory_contract import version
from test_trajectory_normalizer import inputs


def batch_inputs(**limits):
    row, contract, scope = inputs()
    root = deepcopy(row)
    root.update(
        id="root",
        name="remote-agent-http",
        parentObservationId="outer",
        metadata={},
        input=None,
        output='{"intent":"balance"}',
    )
    if limits:
        payload = contract.model_dump()
        payload["limits"].update(limits)
        contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    return [root, row], contract, scope


def normalize(rows, contract, scope):
    return normalizer.normalize_observations(rows, contract, scope=scope)


def test_batch_binds_exact_scope_contract_and_source_without_raw_content():
    rows, contract, scope = batch_inputs()
    result = normalize(rows, contract, scope)
    assert result.scope == scope and result.trajectory_contract_digest == contract.digest
    assert result.source_profile_id == contract.source_profile.profile_id
    assert result.source_profile_version == contract.source_profile.version
    assert result.warnings == [] and not result.truncated
    assert len(result.records) == 2
    assert [r.observation_id for r in result.records] == ["root", "request"]
    assert result.records[1].observation.step.arguments_redacted == {"customer_id": "synthetic-001"}
    assert "synthetic-secret" not in canonical_dumps(result) and "attributes.workflow" not in canonical_dumps(result)
    assert "raw_row" not in result.model_dump()
    rows[1]["metadata"].clear()
    assert result.records[1].observation.event_id == "c" * 32


def test_batch_keeps_safe_lineage_of_rejected_business_row_for_scope_attribution():
    rows, contract, scope = batch_inputs()
    del rows[1]["metadata"]["attributes.workflow.phase"]
    result = normalize(rows, contract, scope)
    record = result.records[1]
    assert record.observation_id == "request" and record.trace_id == scope.trace_id
    assert record.parent_step_id == "root" and record.parent_known
    assert record.observation.step is None and record.observation.warnings == ["STEP_SEMANTICS_UNAVAILABLE"]
    assert result.warnings == []  # scope 内外的行诊断由 Assembler 按真实父链归属。


def test_batch_does_not_infer_missing_parent_or_identity():
    rows, contract, scope = batch_inputs()
    del rows[1]["parentObservationId"]
    result = normalize(rows, contract, scope)
    record = result.records[1]
    assert record.observation_id == "request" and record.parent_step_id is None and not record.parent_known
    assert record.observation.step is None
    rows[1]["id"] = 42
    assert normalize(rows, contract, scope).records[1].observation_id is None


def test_batch_keeps_foreign_trace_only_as_rejected_safe_identity():
    rows, contract, scope = batch_inputs()
    rows[1]["traceId"] = "f" * 32
    record = normalize(rows, contract, scope).records[1]
    assert record.trace_id == "f" * 32 and record.observation.step is None
    assert record.observation.warnings == ["SOURCE_TRACE_SCOPE_MISMATCH"]


def test_batch_alias_conflict_does_not_choose_one_observation_id():
    rows, contract, scope = batch_inputs()
    payload = contract.model_dump()
    profile = next(p for p in payload["source_profile_set"] if p["profile_id"] == "native-otel-v1")
    profile["fields"]["observation_id"].append(["observationId"])
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    rows[1]["observationId"] = "conflicting-id"
    record = normalize(rows, contract, scope).records[1]
    assert record.observation_id is None and record.observation.step is None
    assert record.observation.warnings == ["SOURCE_FIELD_ALIAS_CONFLICT"]


def test_batch_does_not_spend_step_limit_on_arrival_order_or_duplicates():
    rows, contract, scope = batch_inputs(max_steps=1)
    rows = [deepcopy(rows[1]) for _ in range(20)] + [rows[0]]
    result = normalize(rows, contract, scope)
    assert len(result.records) == 21 and not result.truncated
    assert all(record.observation.step is not None for record in result.records)
    assert all(record.observation.step.index == 0 for record in result.records)
    assert result.records[-1].observation.step.step_id == "root"


def test_batch_record_count_uses_frozen_page_budget():
    rows, contract, scope = batch_inputs(page_size=1, max_pages=2)
    result = normalize(rows + [deepcopy(rows[1])], contract, scope)
    assert len(result.records) == 2 and result.truncated
    assert result.warnings == ["SOURCE_ROW_LIMIT"]


def test_batch_counts_canonical_source_utf8_and_stops_before_cumulative_overflow():
    rows, contract, scope = batch_inputs()
    rows[0]["output"] = '{"intent":"余额"}'
    first_bytes = len(canonical_dumps(rows[0]).encode("utf-8"))
    rows, contract, scope = batch_inputs(max_source_bytes=first_bytes)
    rows[0]["output"] = '{"intent":"余额"}'
    result = normalize(rows, contract, scope)
    assert len(result.records) == 1 and result.truncated
    assert result.source_bytes == first_bytes and not result.source_bytes_complete
    assert result.warnings == ["SOURCE_BYTE_LIMIT"]


def test_batch_exact_source_capacity_boundary_and_tuple_input():
    rows, contract, scope = batch_inputs()
    total = sum(len(canonical_dumps(row).encode("utf-8")) for row in rows)
    rows, contract, scope = batch_inputs(max_source_bytes=total)
    result = normalize(tuple(rows), contract, scope)
    assert result.source_bytes == total and len(result.records) == 2 and not result.truncated


def test_batch_rejects_external_sdk_object_without_calling_its_serializer():
    class External(BaseModel):
        secret: str

        def model_dump(self, *args, **kwargs):
            pytest.fail("external serializer must never run")

    rows, contract, scope = batch_inputs()
    rows[1]["metadata"]["unused-sdk-object"] = External(secret="synthetic-secret")
    result = normalize(rows, contract, scope)
    assert result.records[1].observation_id == "request"
    assert result.records[1].observation.step is None
    assert result.records[1].observation.warnings == ["SOURCE_FIELD_INVALID"]
    assert not result.source_bytes_complete
    assert "synthetic-secret" not in canonical_dumps(result)


@pytest.mark.parametrize("rows", [None, {}, iter([])])
def test_batch_rejects_unbounded_or_untyped_collection_without_consuming_it(rows):
    _, contract, scope = batch_inputs()
    with pytest.raises(ValueError, match="SOURCE_COLLECTION_INVALID"):
        normalize(rows, contract, scope)


def test_batch_records_are_independent_and_configuration_is_owned():
    rows, contract, scope = batch_inputs()
    result = normalize(rows + [deepcopy(rows[1])], contract, scope)
    contract.redaction_policy.argument_paths.clear()
    result.records[1].observation.step.argument_visibility.clear()
    assert result.records[2].observation.step.argument_visibility == {"$.customer_id": "AVAILABLE"}


def test_batch_revalidates_scope_and_requires_frozen_source_binding():
    rows, contract, scope = batch_inputs()
    with pytest.raises(ValueError):
        normalize(rows, contract, scope.model_copy(update={"dispatch_generation": 0}))
    with pytest.raises(ValueError, match="TRAJECTORY_SOURCE_PROFILE_UNBOUND"):
        normalize(rows, TrajectoryContract.model_validate(contract.model_dump()), scope)


@pytest.mark.parametrize("limits", [{"max_numeric_digits": 2}, {"max_json_depth": 1}, {"max_string_bytes": 40}])
def test_optional_source_field_limits_do_not_erase_independent_observed_tool_identity(limits):
    rows, contract, scope = batch_inputs(**limits)
    rows[1]["metadata"]["attributes.workflow.duration_ms"] = 123
    rows[1]["metadata"]["unused-telemetry"] = "x" * 80
    row_result = normalizer.normalize_observation(rows[1], contract, scope, index=0)
    assert row_result.step is not None and row_result.step.tool_identity == "banking.customer.lookup"
    result = normalize(rows, contract, scope).records[1].observation
    assert result.step is not None and result.step.tool_identity == row_result.step.tool_identity
    assert canonical_dumps(result) == canonical_dumps(row_result)


def test_batch_rejects_a_retained_step_with_foreign_trace_identity():
    rows, contract, scope = batch_inputs()
    payload = normalize(rows, contract, scope).model_dump()
    payload["records"][1]["trace_id"] = "f" * 32
    with pytest.raises(ValueError, match="TRAJECTORY_NORMALIZED_SCOPE_MISMATCH"):
        NormalizedObservations.model_validate(payload)


@pytest.mark.parametrize("change", [{"parent_known": False}, {"parent_step_id": "other"}, {"observation_id": "other"}])
def test_reconstructed_batch_checks_retained_step_against_lineage(change):
    rows, contract, scope = batch_inputs()
    payload = normalize(rows, contract, scope).model_dump()
    payload["records"][1].update(change)
    with pytest.raises(ValueError, match="TRAJECTORY_NORMALIZED_IDENTITY_MISMATCH"):
        NormalizedObservations.model_validate(payload)
