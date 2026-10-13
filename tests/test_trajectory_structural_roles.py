from copy import deepcopy

import pytest
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import TrajectoryContract
from app.trajectory.normalizer import ObservationNormalizer
from pydantic import ValidationError
from test_trajectory_contract import version
from test_trajectory_normalizer import inputs
from trajectory_contract_fixtures import contract_payload


def role_payload(**changes):
    return {
        "provider_type": "TOOL",
        "raw_name": "account_crm_lookup",
        "step_type": "CUSTOM",
        "safe_name": "framework-tool-container",
        "metadata_equals": {"scope.name": "pydantic-ai", "scope.version": "2.55.0"},
    } | changes


def structural_inputs():
    row, contract, scope = inputs("pydantic-ai-v1")
    payload = contract.model_dump()
    profile = next(profile for profile in payload["source_profile_set"] if profile["profile_id"] == "pydantic-ai-v1")
    profile["structural_roles"] = [role_payload()]
    profile["version"] = "structural-fixture-1"
    for entry in payload["tool_mapping"]["entries"]:
        if entry["source_profile_id"] == profile["profile_id"]:
            entry["source_profile_version"] = profile["version"]
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("pydantic-ai-v1"))
    row.update(
        name="account_crm_lookup",
        input="synthetic-secret",
        output="synthetic-secret",
        metadata={"scope.name": "pydantic-ai", "scope.version": "2.55.0"},
    )
    return row, contract, scope


def test_old_profile_serialization_and_contract_digest_are_byte_compatible():
    contract = TrajectoryContract.model_validate(contract_payload())
    assert "structural_roles" not in canonical_dumps(contract)
    assert contract.digest == "sha256:3ee4cf5a05e650ab855fea213528dffbdfb259405a1ca89452a7f9f8233208b2"


def test_explicit_structural_role_retains_parent_but_never_tool_evidence_or_content():
    row, contract, scope = structural_inputs()
    result = ObservationNormalizer(contract).normalize(row, scope, index=1)
    assert result.structural is True
    assert result.step.observation_id == row["id"] and result.step.parent_step_id == "root"
    assert result.step.type == "CUSTOM" and result.step.phase == "EVENT" and result.step.status == "UNKNOWN"
    assert result.step.raw_name == "framework-tool-container" and result.step.tool_identity is None
    assert result.step.arguments_redacted is None and result.step.argument_visibility == {}
    assert result.step.result_summary is None and result.step.duration_ms is None
    assert result.step.workflow_scope_id is None and result.causal_event_ids == []
    assert all(value == "NONE" for value in result.capabilities.values())
    assert result.warnings == [] and "synthetic-secret" not in canonical_dumps(result)


@pytest.mark.parametrize("field,value", [("name", "unapproved"), ("type", "SPAN")])
def test_structural_role_requires_exact_physical_identity(field, value):
    row, contract, scope = structural_inputs()
    row[field] = value
    result = ObservationNormalizer(contract).normalize(row, scope, index=1)
    assert result.step is None and not result.structural


@pytest.mark.parametrize("field,value", [("scope.name", "other"), ("scope.version", "2.56.0")])
def test_structural_role_requires_approved_source_scope_and_version(field, value):
    row, contract, scope = structural_inputs()
    row["metadata"][field] = value
    assert ObservationNormalizer(contract).normalize(row, scope, index=1).step is None


@pytest.mark.parametrize("field", ["phase", "status", "event_id", "sequence", "call_id", "event_count"])
def test_business_semantic_markers_never_fall_back_to_structural_role(field):
    row, contract, scope = structural_inputs()
    row["metadata"]["attributes.workflow." + field] = None
    result = ObservationNormalizer(contract).normalize(row, scope, index=1)
    assert result.step is None and not result.structural


def test_structural_roles_are_canonical_sorted_and_change_full_contract_digest():
    row, contract, scope = structural_inputs()
    payload = contract.model_dump()
    profile = next(profile for profile in payload["source_profile_set"] if profile["profile_id"] == "pydantic-ai-v1")
    profile["structural_roles"].append(role_payload(provider_type="AGENT", raw_name="agent run", step_type="AGENT"))
    first = TrajectoryContract.model_validate(payload)
    profile["structural_roles"].reverse()
    second = TrajectoryContract.model_validate(payload)
    assert first.digest == second.digest and first.digest != contract.digest


@pytest.mark.parametrize(
    "changes",
    [
        {"step_type": "TOOL"},
        {"step_type": "HUMAN"},
        {"metadata_equals": {}},
        {"metadata_equals": {"http.request.headers.authorization": "forbidden"}},
    ],
)
def test_invalid_structural_role_cannot_certify_tool_human_or_secret_source(changes):
    _row, contract, _scope = inputs()
    payload = contract.model_dump()
    payload["source_profile_set"][0]["structural_roles"] = [role_payload(**changes)]
    with pytest.raises(ValidationError):
        TrajectoryContract.model_validate(payload)


def test_duplicate_role_identity_is_rejected_even_when_predicates_differ():
    _row, contract, _scope = structural_inputs()
    payload = contract.model_dump()
    profile = next(profile for profile in payload["source_profile_set"] if profile["profile_id"] == "pydantic-ai-v1")
    profile["structural_roles"].append(role_payload(metadata_equals={"scope.name": "other"}))
    with pytest.raises(ValidationError):
        TrajectoryContract.model_validate(payload)


def test_context_owns_structural_role_predicates_and_foreign_trace_is_still_rejected():
    row, contract, scope = structural_inputs()
    context = ObservationNormalizer(contract)
    profile = next(profile for profile in contract.source_profile_set if profile.profile_id == "pydantic-ai-v1")
    profile.structural_roles[0].metadata_equals["scope.name"] = "changed"
    assert context.normalize(row, scope, index=1).structural
    foreign = deepcopy(row)
    foreign["traceId"] = "f" * 32
    assert context.normalize(foreign, scope, index=1).step is None


def test_approved_business_source_missing_semantics_cannot_be_reclassified_as_structure():
    row, contract, scope = structural_inputs()
    row["metadata"]["attributes.workflow.source"] = "business-instrumentation-v1"
    result = ObservationNormalizer(contract).normalize(row, scope, index=1)
    assert result.step is None and not result.structural
    assert result.warnings == ["STEP_SEMANTICS_UNAVAILABLE"]


@pytest.mark.parametrize("field", ["phase", "status", "call_id", "causal_event_ids"])
def test_structural_profile_cannot_omit_semantic_marker_detection(field):
    row, contract, scope = structural_inputs()
    payload = contract.model_dump()
    profile = next(profile for profile in payload["source_profile_set"] if profile["profile_id"] == "pydantic-ai-v1")
    del profile["fields"][field]
    row["metadata"]["attributes.workflow." + field] = None
    with pytest.raises(ValidationError, match="TRAJECTORY_STRUCTURAL_GUARD_FIELDS_INCOMPLETE"):
        unchecked = TrajectoryContract.model_validate(payload).bind_source_version(version("pydantic-ai-v1"))
        ObservationNormalizer(unchecked).normalize(row, scope, index=1)
