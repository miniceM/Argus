from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from app.trajectory import normalizer
from app.trajectory.contract import TrajectoryContract, TrajectoryContractError
from pydantic import ValidationError
from test_trajectory_contract import version
from test_trajectory_normalizer import inputs


def test_reusable_normalizer_validates_full_contract_once_for_many_rows(monkeypatch):
    row, contract, scope = inputs()
    original = TrajectoryContract.model_validate
    validations = []

    def validate(cls, value, **kwargs):
        validations.append(1)
        return original(value, **kwargs)

    monkeypatch.setattr(TrajectoryContract, "model_validate", classmethod(validate))
    context = normalizer.ObservationNormalizer(contract)
    results = [context.normalize(row, scope, index=index) for index in range(1000)]
    assert len(validations) == 1
    assert all(result.step.tool_identity == "banking.customer.lookup" for result in results)
    assert [result.step.index for result in results] == list(range(1000))


def test_context_owns_mapping_profile_and_policy_before_caller_mutation():
    row, contract, scope = inputs()
    context = normalizer.ObservationNormalizer(contract)
    contract.tool_mapping.entries.clear()
    contract.source_profile_set[0].metadata_equals["attributes.workflow.source"] = "untrusted"
    contract.redaction_policy.argument_paths.append("$")
    result = context.normalize(row, scope, index=1)
    assert result.step.tool_identity == "banking.customer.lookup"
    assert result.step.argument_visibility == {"$.customer_id": "AVAILABLE"}
    with pytest.raises(ValidationError):
        normalizer.normalize_observation(row, contract, scope, index=1)


def test_reusable_context_preserves_per_row_scope_isolation_and_owned_outputs():
    row, contract, scope = inputs()
    context = normalizer.ObservationNormalizer(contract)
    first = context.normalize(row, scope, index=1)
    foreign = deepcopy(row)
    foreign["traceId"] = "f" * 32
    assert context.normalize(foreign, scope, index=2).step is None
    row["input"] = '{"customer_id":"second"}'
    second = context.normalize(row, scope, index=3)
    assert first.step.arguments_redacted == {"customer_id": "synthetic-001"}
    assert second.step.arguments_redacted == {"customer_id": "second"}


@pytest.mark.parametrize("index", [-1, True, 2, 1000])
def test_context_retains_frozen_step_index_budget(index):
    row, contract, scope = inputs()
    payload = contract.model_dump()
    payload["limits"]["max_steps"] = 2
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    result = normalizer.ObservationNormalizer(contract).normalize(row, scope, index=index)
    assert result.step is None and result.warnings == ["SOURCE_FIELD_INVALID"]


def test_context_requires_bound_approved_source_at_creation():
    _row, contract, _scope = inputs()
    unbound = TrajectoryContract.model_validate(contract.model_dump())
    with pytest.raises(TrajectoryContractError, match="TRAJECTORY_SOURCE_PROFILE_UNBOUND"):
        normalizer.ObservationNormalizer(unbound)


def test_context_rejects_mutated_invalid_configuration_at_creation():
    _row, contract, _scope = inputs()
    contract.redaction_policy.argument_paths.append("$")
    with pytest.raises(ValidationError):
        normalizer.ObservationNormalizer(contract)


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
def test_exact_datetime_aliases_remain_supported_without_accepting_sdk_objects(field):
    row, contract, scope = inputs()
    source_key = "startTime" if field == "started_at" else "endTime"
    first = normalizer.normalize_observation(row, contract, scope, index=1)
    timestamp = getattr(first.step, field)
    payload = contract.model_dump()
    payload["source_profile_set"][0]["fields"][field].append(["legacyTimestamp"])
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    row[source_key] = row["legacyTimestamp"] = timestamp
    result = normalizer.normalize_observation(row, contract, scope, index=1)
    assert getattr(result.step, field) == timestamp


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize(
    "timestamp",
    [datetime(2026, 10, 13), datetime.min.replace(tzinfo=timezone(timedelta(hours=1))), "invalid-time"],
)
def test_invalid_time_alias_only_degrades_time_without_dropping_tool_fact(field, timestamp):
    row, contract, scope = inputs()
    payload = contract.model_dump()
    payload["source_profile_set"][0]["fields"][field].append(["legacyTimestamp"])
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    row["startTime" if field == "started_at" else "endTime"] = row["legacyTimestamp"] = timestamp
    result = normalizer.normalize_observation(row, contract, scope, index=1)
    assert result.step is not None and result.step.tool_identity == "banking.customer.lookup"
    assert getattr(result.step, field) is None and result.capabilities["ordering"] == "PARTIAL"
    assert result.step.duration_ms is not None
    assert result.capabilities["timing"] == contract.source_profile.capabilities["timing"]
    assert "STEP_TIME_INVALID" in result.warnings


@pytest.mark.parametrize("input_value", ["{broken", {"id": object()}])
def test_invalid_http_root_input_never_claims_tool_argument_coverage(input_value):
    row, contract, scope = inputs()
    row.update(id="root", type="SPAN", name="remote-agent-http", metadata={}, input=input_value)
    result = normalizer.ObservationNormalizer(contract).normalize(row, scope, index=0)
    assert result.step is not None and result.step.type == "CUSTOM"
    assert all(value == "NONE" for value in result.capabilities.values())


def test_valid_time_aliases_compare_normalized_instants_and_conflicts_still_reject():
    row, contract, scope = inputs()
    payload = contract.model_dump()
    payload["source_profile_set"][0]["fields"]["started_at"].append(["legacyTimestamp"])
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    row["legacyTimestamp"] = "2026-10-13T04:00:00.000001Z"
    assert normalizer.normalize_observation(row, contract, scope, index=1).step is not None
    row["legacyTimestamp"] = "2026-10-13T04:00:00.000002Z"
    result = normalizer.normalize_observation(row, contract, scope, index=1)
    assert result.step is None and result.warnings == ["SOURCE_FIELD_ALIAS_CONFLICT"]
