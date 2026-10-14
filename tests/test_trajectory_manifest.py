import warnings
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from app.db import DatabaseManager
from app.db_models import ExperimentLaunchRecord
from app.evaluator_binding import (
    EvaluatorBindingError,
    evaluate_frozen_item,
    manifest_measurement_digest,
    resolve_execution_plan,
)
from app.manifest import LaunchService
from app.registry import AgentRegistry
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import TrajectoryContract
from app.trajectory.manifest import (
    TrajectoryManifestError,
    bind_trajectory_manifest,
    freeze_trajectory_contract,
    load_trajectory_contract,
)
from sqlalchemy.orm import sessionmaker
from test_launch_manifest_freeze import setup_db
from test_trajectory_evidence import postgres_engine as evidence_postgres_engine
from trajectory_contract_fixtures import contract_payload


@pytest.fixture
def context(tmp_path):
    db, registry = setup_db(tmp_path)
    for name, profile in (("native", "native-otel-v1"), ("pydantic", "pydantic-ai-v1")):
        registry.create_version(agent_id="banking-agent", version=name, endpoint="http://demo-agent:8080/invoke",
            trajectory_source_profile_id=profile)
    payload = contract_payload()
    payload["redaction_policy"].update(content_policy_version="pii-v1",
        string_policies={"$.customer_id": "identifier", "$.intent": "identifier"})
    contract = TrajectoryContract.model_validate(payload)
    return db, registry, LaunchService(db, registry), contract


def create(context, *, version="native", contract=None, **options):
    options.setdefault("capture_only", True)  # 合同冻结测试显式使用内部采集校准入口。
    return context[2].create_launch(agent_id="banking-agent", agent_version=version,
        dataset_name="banking-agent-regression", trajectory_contract=contract or context[3], **options)


def test_manifest_freezes_all_profiles_and_exact_decimal_through_database(context):
    payload = context[3].model_dump()
    payload["limits"]["quiet_window_seconds"] = Decimal("0.12345678901234567890123456789")
    contract = TrajectoryContract.model_validate(payload)
    launch = create(context, contract=contract)
    with context[0].get_session() as session:
        manifest = deepcopy(session.get(ExperimentLaunchRecord, launch.id).manifest)
    assert manifest["schema_version"] == "1.3"
    capsule = manifest["trajectory"]
    assert capsule["contract_json"] == canonical_dumps(contract)
    assert capsule["trajectory_contract_digest"] == contract.digest
    assert capsule["selected_source_profile_id"] == "native-otel-v1"
    assert capsule["selected_source_profile_version"] == "fixture-1"
    assert manifest["agent"]["trajectory_source_profile_id"] == "native-otel-v1"
    restored = load_trajectory_contract(manifest)
    assert restored.digest == contract.digest and restored.source_profile.profile_id == "native-otel-v1"
    assert len(restored.source_profile_set) == 2
    assert restored.limits.quiet_window_seconds == payload["limits"]["quiet_window_seconds"]
    assert manifest["measurement_digest"] == manifest_measurement_digest(manifest)
    assert manifest["contract_digests"]["measurement"] == {"digest": manifest["measurement_digest"], "version": "1.3"}


def test_same_full_contract_different_frameworks_share_measurement_identity(context):
    first, second = create(context), create(context, version="pydantic")
    assert first.manifest["trajectory"]["selected_source_profile_id"] != second.manifest["trajectory"]["selected_source_profile_id"]
    assert first.manifest["measurement_digest"] == second.manifest["measurement_digest"]
    assert first.manifest["evaluators"] == second.manifest["evaluators"]
    assert len(resolve_execution_plan(first.manifest)) == 4


def test_launch_uses_immutable_agent_declaration_instead_of_callers_bound_branch(context):
    bound = context[3].bind_source_version(context[1].get_version("banking-agent", "pydantic"))
    launch = create(context, contract=bound)
    assert load_trajectory_contract(launch.manifest).source_profile.profile_id == "native-otel-v1"
    assert bound.source_profile.profile_id == "pydantic-ai-v1"


def test_trajectory_contract_changes_break_request_idempotency(context):
    first = create(context, idempotency_key="trajectory-request")
    assert create(context, idempotency_key="trajectory-request").id == first.id
    payload = context[3].model_dump()
    payload["limits"]["max_pages"] = 7
    changed = TrajectoryContract.model_validate(payload)
    with pytest.raises(ValueError, match="Idempotency key conflict"):
        create(context, contract=changed, idempotency_key="trajectory-request")
    second = create(context, contract=changed)
    assert second.manifest["measurement_digest"] != first.manifest["measurement_digest"]


def test_freeze_rejects_unapproved_or_missing_agent_profile(context):
    for name, profile in (("unknown", "not-approved"), ("undeclared", None)):
        context[1].create_version(agent_id="banking-agent", version=name, endpoint="http://demo-agent:8080/invoke",
            trajectory_source_profile_id=profile)
        with pytest.raises(TrajectoryManifestError, match="SOURCE_MISMATCH"):
            create(context, version=name)


def test_legacy_launch_does_not_receive_trajectory_contract_or_new_schema(context):
    first = context[2].create_launch(agent_id="banking-agent", agent_version="v1",
        dataset_name="banking-agent-regression", idempotency_key="legacy-request")
    assert first.manifest["schema_version"] == "1.2" and "trajectory" not in first.manifest
    assert "trajectory_source_profile_id" not in first.manifest["agent"]
    assert load_trajectory_contract(first.manifest) is None
    assert len(resolve_execution_plan(first.manifest)) == 4
    assert context[2].create_launch(agent_id="banking-agent", agent_version="v1",
        dataset_name="banking-agent-regression", idempotency_key="legacy-request").id == first.id


def test_corrupt_trajectory_manifest_blocks_shared_evaluation_before_any_score(context):
    manifest = deepcopy(create(context).manifest)
    manifest["trajectory"]["trajectory_contract_digest"] = "sha256:" + "0" * 64
    result = evaluate_frozen_item(manifest, output={"intent": "balance"}, expected_output={"intent": "balance"})
    assert result.scores == {} and result.quality_conclusion == "unknown"
    assert result.eval_status == "failed" and result.eval_error == "TRAJECTORY_MANIFEST_DIGEST_MISMATCH"


def test_consistent_capsule_change_without_frozen_measurement_update_is_rejected(context):
    manifest = deepcopy(create(context).manifest)
    payload = context[3].model_dump()
    payload["limits"]["max_pages"] = 7
    changed = TrajectoryContract.model_validate(payload)
    manifest["trajectory"]["contract_json"] = canonical_dumps(changed)
    manifest["trajectory"]["trajectory_contract_digest"] = changed.digest
    with pytest.raises(EvaluatorBindingError) as error:
        resolve_execution_plan(manifest)
    assert error.value.code == "TRAJECTORY_MANIFEST_MEASUREMENT_MISMATCH"


def test_mutated_contract_container_is_rejected_without_raw_serializer_warning(context):
    context[3].source_profile_set[0] = "SYNTHETIC_SECRET_PROFILE"
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        with pytest.raises(TrajectoryManifestError, match="CONTRACT_INVALID"):
            freeze_trajectory_contract(context[3])
    assert recorded == []


@pytest.mark.parametrize("change", ["limit", "profile-fields", "profile-version", "mapping", "redaction"])
def test_each_measurement_relevant_contract_change_has_a_distinct_identity(context, change):
    first = create(context)
    payload = context[3].model_dump()
    if change == "limit":
        payload["limits"]["page_size"] = 17
    elif change == "profile-fields":
        payload["source_profile_set"][0]["fields"]["observation_id"].append(["alternateObservationId"])
    elif change == "profile-version":
        profile = payload["source_profile_set"][0]
        profile["version"] = "fixture-2"
        for entry in payload["tool_mapping"]["entries"]:
            if entry["source_profile_id"] == profile["profile_id"]:
                entry["source_profile_version"] = "fixture-2"
    elif change == "mapping":
        payload["tool_mapping"]["version"] = "fixture-2"
    else:
        payload["redaction_policy"]["argument_paths"].append("$.customer.balance")
    changed = TrajectoryContract.model_validate(payload)
    second = create(context, contract=changed)
    assert second.manifest["measurement_digest"] != first.manifest["measurement_digest"]
    assert second.manifest["trajectory"]["trajectory_contract_digest"] != context[3].digest


@pytest.mark.parametrize("case,code", [
    ("missing-capsule", "CONTRACT_INVALID"), ("extra-capsule-key", "CONTRACT_INVALID"),
    ("missing-json", "CONTRACT_INVALID"), ("invalid-json", "CONTRACT_INVALID"),
    ("duplicate-json-key", "CONTRACT_INVALID"), ("noncanonical", "CONTRACT_INVALID"),
    ("unknown-profile", "SOURCE_MISMATCH"), ("source-version", "SOURCE_MISMATCH"),
    ("branch-conflict", "SOURCE_MISMATCH"), ("legacy-graft", "SCHEMA_MISMATCH"),
    ("unsupported-schema", "SCHEMA_UNSUPPORTED"), ("non-string-schema", "SCHEMA_UNSUPPORTED"),
])
def test_corrupt_frozen_contract_is_rejected_with_finite_reason(context, case, code):
    manifest = deepcopy(create(context).manifest)
    capsule = manifest["trajectory"]
    if case == "missing-capsule":
        del manifest["trajectory"]
    elif case == "extra-capsule-key":
        capsule["unexpected"] = "SYNTHETIC_SECRET"
    elif case == "missing-json":
        del capsule["contract_json"]
    elif case == "invalid-json":
        capsule["contract_json"] = "SYNTHETIC_SECRET"
    elif case == "duplicate-json-key":
        capsule["contract_json"] = '{"contract_version":"1.0","contract_version":"SYNTHETIC_SECRET"}'
    elif case == "noncanonical":
        capsule["contract_json"] = " " + capsule["contract_json"]
    elif case == "unknown-profile":
        capsule["selected_source_profile_id"] = manifest["agent"]["trajectory_source_profile_id"] = "not-approved"
    elif case == "source-version":
        capsule["selected_source_profile_version"] = "not-approved"
    elif case == "branch-conflict":
        capsule["selected_source_profile_id"] = "pydantic-ai-v1"
    elif case == "legacy-graft":
        manifest["schema_version"] = "1.2"
    elif case == "unsupported-schema":
        manifest["schema_version"] = "1.10"
    else:
        manifest["schema_version"] = ["1.3"]
    with pytest.raises(EvaluatorBindingError) as error:
        resolve_execution_plan(manifest)
    assert error.value.code == "TRAJECTORY_MANIFEST_" + code
    assert str(error.value) == error.value.code
    result = evaluate_frozen_item(manifest, output={}, expected_output={})
    assert result.eval_status == "failed" and result.quality_conclusion == "unknown" and result.scores == {}
    assert "SYNTHETIC_SECRET" not in str(result)


@pytest.mark.parametrize("shape", ["missing", "changed-digest", "changed-version"])
def test_frozen_three_layer_measurement_metadata_cannot_disagree(context, shape):
    manifest = deepcopy(create(context).manifest)
    if shape == "missing":
        del manifest["contract_digests"]
    elif shape == "changed-digest":
        manifest["contract_digests"]["measurement"]["digest"] = "sha256:" + "0" * 64
    else:
        manifest["contract_digests"]["measurement"]["version"] = "1.2"
    with pytest.raises(EvaluatorBindingError) as error:
        resolve_execution_plan(manifest)
    assert error.value.code == "TRAJECTORY_MANIFEST_MEASUREMENT_MISMATCH"


def test_callers_nested_mutation_during_dataset_resolution_does_not_change_frozen_contract(context, monkeypatch):
    from app.dataset import DatasetResolver
    original, digest = DatasetResolver.resolve, context[3].digest

    def mutate(*args, **options):
        context[3].source_profile_set.clear()
        return original(*args, **options)

    monkeypatch.setattr(DatasetResolver, "resolve", mutate)
    launch = create(context)
    assert load_trajectory_contract(launch.manifest).digest == digest
    assert launch.manifest["trajectory"]["trajectory_contract_digest"] == digest


@pytest.mark.parametrize("schema", ["1.0", "1.1", "1.2", None])
def test_registered_historical_versions_remain_readable_without_backfill(context, schema):
    manifest = deepcopy(context[2].create_launch(agent_id="banking-agent", agent_version="v1",
        dataset_name="banking-agent-regression").manifest)
    manifest["schema_version"] = schema
    before = deepcopy(manifest)
    assert load_trajectory_contract(manifest) is None
    assert len(resolve_execution_plan(manifest)) == 4
    assert evaluate_frozen_item(manifest, output={"intent": "balance"}, expected_output={"intent": "balance"}).scores["intent_match"] == 1
    assert manifest == before


@pytest.fixture
def postgres_engine():
    yield from evidence_postgres_engine.__wrapped__()


def test_postgres_manifest_jsonb_preserves_full_contract_decimal_precision(postgres_engine):
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.engine = postgres_engine
    manager.session_factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    registry = AgentRegistry(manager)
    registry.import_yaml(Path(__file__).resolve().parents[1] / "config/agents.yaml")
    registry.create_version(agent_id="banking-agent", version="native", endpoint="http://demo-agent:8080/invoke",
        trajectory_source_profile_id="native-otel-v1")
    payload = contract_payload()
    payload["redaction_policy"].update(content_policy_version="pii-v1",
        string_policies={"$.customer_id": "identifier", "$.intent": "identifier"})
    payload["limits"]["quiet_window_seconds"] = Decimal("0.12345678901234567890123456789")
    contract = TrajectoryContract.model_validate(payload)
    context = manager, registry, LaunchService(manager, registry), contract
    launch = create(context)
    with manager.get_session() as session:
        manifest = deepcopy(session.get(ExperimentLaunchRecord, launch.id).manifest)
    restored = load_trajectory_contract(manifest)
    assert restored.limits.quiet_window_seconds == payload["limits"]["quiet_window_seconds"]
    assert restored.digest == contract.digest
    assert manifest["measurement_digest"] == manifest_measurement_digest(manifest)


def test_new_launch_rejects_ungoverned_trajectory_contract_before_persistence(context):
    payload = context[3].model_dump()
    payload["redaction_policy"].pop("content_policy_version", None)
    payload["redaction_policy"].pop("string_policies", None)
    ungoverned = TrajectoryContract.model_validate(payload)
    with context[0].get_session() as session:
        before = session.query(ExperimentLaunchRecord).count()
    with pytest.raises(TrajectoryManifestError, match="CONTENT_POLICY_REQUIRED"):
        create(context, contract=ungoverned)
    with context[0].get_session() as session:
        assert session.query(ExperimentLaunchRecord).count() == before


def test_existing_manifest_13_without_content_policy_remains_frozen_and_readable(context):
    manifest = deepcopy(create(context).manifest)
    legacy_contract = TrajectoryContract.model_validate(contract_payload())
    manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(legacy_contract),
        context[1].get_version("banking-agent", "native"))
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
    before = deepcopy(manifest)
    restored = load_trajectory_contract(manifest)
    assert restored.redaction_policy.content_policy_version is None
    assert restored.digest == legacy_contract.digest
    assert len(resolve_execution_plan(manifest)) == 4
    assert manifest == before
