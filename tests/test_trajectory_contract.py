from dataclasses import replace
from decimal import Decimal

import pytest
from app.registry import AgentVersionSpec
from app.trajectory.codec import canonical_dumps, canonical_loads
from app.trajectory.contract import SourceProfile, TrajectoryContract, TrajectoryContractError, TrajectoryLimits
from pydantic import ValidationError
from trajectory_contract_fixtures import contract_payload, profile_payload


def version(profile_id):
    return AgentVersionSpec(
        agent_id="fixture-agent",
        version="v1",
        endpoint="http://localhost/invoke",
        method="POST",
        timeout_seconds=5,
        max_retries=0,
        rate_limit_per_minute=60,
        request_mapping={},
        trajectory_source_profile_id=profile_id,
    )


def test_two_declared_frameworks_share_full_measurement_contract():
    contract = TrajectoryContract.model_validate(contract_payload())
    native = contract.bind_source_version(version("native-otel-v1"))
    pydantic = contract.bind_source_version(version("pydantic-ai-v1"))
    assert native.source_profile.profile_id == "native-otel-v1"
    assert pydantic.source_profile.profile_id == "pydantic-ai-v1"
    assert native.digest == pydantic.digest == contract.digest
    assert canonical_dumps(native) == canonical_dumps(pydantic)
    assert "selected_source_profile" not in canonical_dumps(native)
    with pytest.raises(TrajectoryContractError, match="UNBOUND"):
        _ = contract.source_profile
    assert TrajectoryContract.model_validate(canonical_loads(canonical_dumps(native))).digest == native.digest


@pytest.mark.parametrize("profile_id", [None, "unknown-profile"])
def test_selection_fails_closed_for_missing_or_unapproved_version_declaration(profile_id):
    contract = TrajectoryContract.model_validate(contract_payload())
    with pytest.raises(TrajectoryContractError, match="NOT_APPROVED"):
        contract.bind_source_version(version(profile_id))


def test_unbound_config_rejects_trace_or_browser_selected_branch_and_bad_algorithm():
    for change in (
        {"selected_source_profile_id": "native-otel-v1"},
        {"profile": "native-otel-v1"},
        {"source_profile_selector_version": "infer-from-trace"},
        {"rule_scope_version": "timestamp-order"},
    ):
        with pytest.raises(ValidationError):
            TrajectoryContract.model_validate(contract_payload(**change))


def test_configuration_order_does_not_change_semantic_digest():
    original = contract_payload()
    first = TrajectoryContract.model_validate(original)
    original["source_profile_set"].reverse()
    original["tool_mapping"]["entries"].reverse()
    original["required_capabilities"] = ["ordering", "tool_executions", "ordering"]
    original["redaction_policy"]["argument_paths"] *= 2
    second = TrajectoryContract.model_validate(original)
    assert first.digest == second.digest


def test_mutating_any_result_related_config_changes_contract_identity():
    first = TrajectoryContract.model_validate(contract_payload())
    mutations = [
        lambda data: data["limits"].update(deadline_seconds=Decimal("30")),
        lambda data: data["source_profile_set"][0]["fields"]["raw_name"].append(["legacyName"]),
        lambda data: data["source_profile_set"][1].update(version="fixture-2"),
        lambda data: data["tool_mapping"]["entries"][0].update(tool_identity="banking.customer.other"),
        lambda data: data["redaction_policy"]["argument_paths"].append("$.account_id"),
        lambda data: data["required_capabilities"].append("tool_arguments"),
        lambda data: data.update(assembler_version="fixture-2"),
    ]
    for mutate in mutations:
        changed = contract_payload()
        mutate(changed)
        if changed["source_profile_set"][1]["version"] == "fixture-2":
            changed["tool_mapping"]["entries"][1]["source_profile_version"] = "fixture-2"
        assert TrajectoryContract.model_validate(changed).digest != first.digest


def test_unknown_mapping_profile_or_version_and_duplicate_identity_are_rejected():
    for field, value in (("source_profile_id", "outside-set"), ("source_profile_version", "unknown-version")):
        data = contract_payload()
        data["tool_mapping"]["entries"][0][field] = value
        with pytest.raises(ValidationError):
            TrajectoryContract.model_validate(data)
    data = contract_payload()
    data["source_profile_set"].append(profile_payload())
    with pytest.raises(ValidationError):
        TrajectoryContract.model_validate(data)
    data = contract_payload()
    data["tool_mapping"]["entries"].append(dict(data["tool_mapping"]["entries"][0], tool_identity="conflicting"))
    with pytest.raises(ValidationError):
        TrajectoryContract.model_validate(data)


def test_profile_capabilities_mappings_and_aliases_are_explicit_and_bounded():
    changes = [
        {"capabilities": {"tool_executions": "COMPLETE"}},
        {"provider_major_version": True},
        {"provider_major_version": "4"},
        {"fields": {"observation_id": [["id"]]}},
        {"type_map": {"TOOL": "HUMAN"}},
        {"completion_protocol": "root-only-v1"},
        {"human_authority_id": None, "capabilities": profile_payload()["capabilities"] | {"human_events": "COMPLETE"}},
        {"fields": profile_payload()["fields"] | {"phase": [[]]}},
        {"fields": profile_payload()["fields"] | {"phase": [["metadata"] * 33]}},
        {"fields": profile_payload()["fields"] | {"phase": [["metadata", "invalid space"]]}},
        {"phase_map": {"UNKNOWN": "EXECUTED_BY_NAME"}},
    ]
    for change in changes:
        with pytest.raises(ValidationError):
            SourceProfile.model_validate(profile_payload(**change))


@pytest.mark.parametrize(
    "path",
    [
        "$",
        "$.*",
        "$..customer_id",
        "$['token']",
        "$.Authorization",
        "$.headers",
        "$.access_token",
        "$.items[01]",
        "$.accessToken",
        "$.refreshToken",
        "$.clientSecret",
    ],
)
def test_redaction_policy_rejects_broad_dynamic_or_secret_paths(path):
    data = contract_payload()
    data["redaction_policy"]["argument_paths"] = [path]
    with pytest.raises(ValidationError):
        TrajectoryContract.model_validate(data)


def test_source_aliases_cannot_redirect_approved_input_to_credential_metadata():
    data = profile_payload()
    data["fields"]["input"] = [["metadata", "Authorization"]]
    with pytest.raises(ValidationError):
        SourceProfile.model_validate(data)


@pytest.mark.parametrize(
    "name", ["AWS_SECRET_ACCESS_KEY", "secretAccessKey", "requestHeaders", "request_headers", "httpHeaders"]
)
def test_credential_and_header_container_variants_are_rejected_at_all_config_entry_points(name):
    source = profile_payload()
    source["fields"]["input"] = [["metadata", name]]
    with pytest.raises(ValidationError, match="SECRET_PATH_FORBIDDEN"):
        SourceProfile.model_validate(source)
    redaction = contract_payload()
    redaction["redaction_policy"]["argument_paths"] = ["$." + name]
    with pytest.raises(ValidationError, match="SECRET_PATH_FORBIDDEN"):
        TrajectoryContract.model_validate(redaction)


@pytest.mark.parametrize("name", ["Authorization", "AWS_SECRET_ACCESS_KEY", "attributes.Authorization"])
def test_source_predicates_cannot_freeze_credential_values(name):
    with pytest.raises(ValidationError, match="SECRET_PATH_FORBIDDEN"):
        SourceProfile.model_validate(profile_payload(metadata_equals={name: "synthetic-secret"}))


@pytest.mark.parametrize("semantic_field", ["phase", "status"])
@pytest.mark.parametrize("identity_field", ["provider_type", "raw_name", "tool_raw_name"])
def test_phase_and_status_cannot_be_inferred_from_type_or_names(semantic_field, identity_field):
    data = profile_payload()
    data["fields"]["tool_raw_name"] = [["metadata", "attributes.gen_ai.tool.name"]]
    data["fields"][semantic_field] = data["fields"][identity_field]
    with pytest.raises(ValidationError, match="SEMANTIC_SOURCE_CONFLICT"):
        SourceProfile.model_validate(data)


@pytest.mark.parametrize("entry_point", ["source_alias", "redaction_path"])
def test_lower_configured_path_budget_is_enforced_for_all_paths(entry_point):
    data = contract_payload(limits={"max_jsonpath_segments": 2})
    if entry_point == "source_alias":
        data["source_profile_set"][0]["fields"]["input"] = [["metadata", "input", "value"]]
    else:
        data["redaction_policy"]["argument_paths"] = ["$.customer.account.id"]
    with pytest.raises(ValidationError, match="CONTRACT_PATH_SEGMENT_LIMIT"):
        TrajectoryContract.model_validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "workflow_scope_id",
        "event_id",
        "sequence",
        "terminal_event_count",
        "terminal_event_ids",
        "terminal_final_sequence",
    ],
)
def test_workflow_inventory_protocol_requires_inputs_to_verify_event_closure(field):
    data = profile_payload()
    del data["fields"][field]
    with pytest.raises(ValidationError, match="INVENTORY_FIELDS_INCOMPLETE"):
        SourceProfile.model_validate(data)


def test_configuration_cannot_validate_then_exceed_digest_byte_limit():
    data = contract_payload()
    data["redaction_policy"]["argument_paths"] = ["$." + "x" * 4089 + f"{index:04d}" for index in range(513)]
    with pytest.raises(ValidationError, match="JSON_BYTE_LIMIT"):
        TrajectoryContract.model_validate(data)


def test_starting_deadline_and_pages_can_be_calibrated_with_new_contract_identity():
    first = TrajectoryContract.model_validate(contract_payload())
    changed = TrajectoryContract.model_validate(contract_payload(limits={"deadline_seconds": 90, "max_pages": 30}))
    assert changed.digest != first.digest
    assert changed.limits.deadline_seconds == Decimal(90)
    assert changed.limits.max_pages == 30


def test_binding_returns_owned_configuration_and_digest_revalidates_mutable_containers():
    template = TrajectoryContract.model_validate(contract_payload())
    bound = template.bind_source_version(version("native-otel-v1"))
    bound.source_profile.safe_names["unsafe"] = "unsafe"
    assert "unsafe" not in bound.source_profile.safe_names
    template.required_capabilities.append("invalid-capability")
    with pytest.raises(ValidationError):
        _ = template.digest
    assert bound.digest  # 模板后续修改不污染已绑定执行配置。
    with pytest.raises(TrajectoryContractError):
        bound.bind_source_version(replace(version(None), trajectory_source_profile_id="fake"))


def test_limits_use_exact_seconds_and_preserve_v1_hard_bounds():
    limits = TrajectoryLimits.model_validate({"quiet_window_seconds": Decimal("1.00000000000000000000001")})
    assert limits.max_steps == 1000 and limits.page_size == 100 and limits.max_pages == 20
    assert limits.max_source_bytes == 8 * 1024 * 1024 and limits.max_artifact_bytes == 2 * 1024 * 1024
    assert limits.request_timeout_seconds == Decimal(5) and limits.deadline_seconds == Decimal(60)
    assert limits.max_inflight_acquisitions == 4
    assert '"quiet_window_seconds":1.00000000000000000000001' in canonical_dumps(limits)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_steps": 1001},
        {"max_steps": 0},
        {"page_size": 101},
        {"page_size": True},
        {"max_pages": "20"},
        {"max_source_bytes": 8 * 1024 * 1024 + 1},
        {"max_artifact_bytes": 2 * 1024 * 1024 + 1},
        {"request_timeout_seconds": Decimal("5.01")},
        {"deadline_seconds": 0},
        {"deadline_seconds": 301},
        {"quiet_window_seconds": -1},
        {"quiet_window_seconds": 60},
        {"quiet_window_seconds": 0.1},
        {"request_timeout_seconds": True},
        {"deadline_seconds": Decimal("NaN")},
        {"max_inflight_acquisitions": 5},
        {"max_json_depth": 33},
        {"max_string_bytes": 4097},
        {"max_numeric_digits": 65},
        {"max_jsonpath_segments": 33},
    ],
)
def test_invalid_or_unbounded_limits_are_rejected(changes):
    with pytest.raises(ValidationError):
        TrajectoryLimits.model_validate(changes)
