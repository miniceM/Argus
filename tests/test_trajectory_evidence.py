import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from app.db import DatabaseManager, MigrationRunner
from app.db_models import (
    AgentVersionRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    TrajectoryEvidenceArtifactRecord,
)
from app.trajectory import evidence
from app.trajectory.assembler import assemble_trajectory, trajectory_digest
from app.trajectory.codec import canonical_dumps
from app.trajectory.schema import StandardTrajectory, TrajectoryScope
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.orm import Session
from test_trajectory_assembler import batch, configured, prepared
from test_trajectory_storage_schema import seed_history

ROOT = Path(__file__).resolve().parents[1]


def artifact():
    rows, contract, scope = prepared()
    scope = TrajectoryScope.model_validate(scope.model_dump() | {
        "launch_id": "launch-1", "item_execution_id": "item-1", "attempt_id": "attempt-1"})
    return assemble_trajectory(scope, batch(rows, contract, scope), contract), contract


def anchor(engine, trajectory):
    seed_history(engine)
    with engine.begin() as connection:
        connection.execute(update(AgentVersionRecord).values(trajectory_source_profile_id="native-otel-v1"))
        connection.execute(update(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.id == "checkpoint").values(
            trace_id=trajectory.scope.trace_id, observation_id=trajectory.scope.root_observation_id))


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'evidence.db'}")
    MigrationRunner(manager.engine, ROOT / "migrations").apply_all()
    trajectory, contract = artifact()
    anchor(manager.engine, trajectory)
    yield manager, trajectory, contract
    manager.engine.dispose()


def reseal(trajectory, change):
    payload = deepcopy(trajectory.model_dump())
    change(payload)
    value = StandardTrajectory.model_validate(payload)
    payload["provenance"]["trajectory_digest"] = trajectory_digest(value)
    return StandardTrajectory.model_validate(payload)


def store(db, trajectory, contract):
    with db.get_session() as session:
        return evidence.persist_trajectory_evidence(session, trajectory, contract)


def load(db, ref, trajectory, contract):
    with db.get_session() as session:
        return evidence.load_trajectory_evidence(session, ref, scope=trajectory.scope, contract=contract)


def test_insert_only_identity_exact_roundtrip_and_new_revision(db):
    manager, trajectory, contract = db
    ref = store(manager, trajectory, contract)
    first = load(manager, ref, trajectory, contract)
    assert canonical_dumps(first) == canonical_dumps(trajectory)
    assert evidence.inventory_complete(first, contract)
    assert "synthetic-secret" not in canonical_dumps(first)
    with manager.get_session() as session:
        row = session.get(TrajectoryEvidenceArtifactRecord, ref.artifact_id)
        before = (row.payload_json, row.created_at)
    assert store(manager, trajectory, contract) == ref
    with manager.get_session() as session:
        row = session.get(TrajectoryEvidenceArtifactRecord, ref.artifact_id)
        assert (row.payload_json, row.created_at) == before
    changed = reseal(trajectory, lambda p: p["steps"][2].update(duration_ms=9))
    second = store(manager, changed, contract)
    assert second != ref and load(manager, ref, trajectory, contract) == first


def test_writer_uses_the_callers_transaction_and_does_not_commit(db):
    manager, trajectory, contract = db
    with pytest.raises(RuntimeError, match="rollback-owner"):
        with manager.get_session() as session:
            evidence.persist_trajectory_evidence(session, trajectory, contract)
            raise RuntimeError("rollback-owner")
    with manager.get_session() as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 0


@pytest.mark.parametrize("field,value", [("attempt_id", "attempt-2"), ("dataset_item_id", "other"),
                                        ("launch_id", "launch-2"), ("dispatch_generation", 2),
                                        ("trace_id", "f" * 32), ("root_observation_id", "foreign-root")])
def test_writer_rejects_scope_not_frozen_by_the_original_checkpoint(db, field, value):
    manager, trajectory, contract = db
    def change(payload):
        payload["scope"][field] = value
        if field == "root_observation_id":
            payload["steps"][0].update(step_id=value, observation_id=value)
            for step in payload["steps"][1:]:
                if step["parent_step_id"] == trajectory.scope.root_observation_id:
                    step["parent_step_id"] = value
    foreign = reseal(trajectory, change)
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="SCOPE_MISMATCH"):
        store(manager, foreign, contract)


def test_old_generation_cannot_write_but_original_artifact_survives_checkpoint_ttl_and_new_generation(db):
    manager, trajectory, contract = db
    ref = store(manager, trajectory, contract)
    with manager.get_session() as session:
        session.get(ExecutionCheckpointRecord, "checkpoint").expires_at = datetime.now(UTC) - timedelta(days=1)
        session.get(ExperimentItemExecutionRecord, "item-1").dispatch_generation = 2
    assert load(manager, ref, trajectory, contract) == trajectory
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="GENERATION_STALE"):
        store(manager, trajectory, contract)
    with manager.get_session() as session:
        session.execute(delete(ExecutionCheckpointRecord))
    assert load(manager, ref, trajectory, contract) == trajectory


@pytest.mark.parametrize("change", ["scope", "provenance", "digest", "payload", "noncanonical"])
def test_corruption_is_unavailable_and_duplicate_insert_does_not_repair_it(db, change):
    manager, trajectory, contract = db
    ref = store(manager, trajectory, contract)
    with manager.get_session() as session:
        record = session.get(TrajectoryEvidenceArtifactRecord, ref.artifact_id)
        if change == "scope":
            record.payload_json = canonical_dumps(reseal(trajectory, lambda p: p["scope"].update(launch_id="launch-2")))
        elif change == "provenance":
            record.trajectory_contract_digest = "sha256:" + "0" * 64
        elif change == "digest":
            record.trajectory_digest = "sha256:" + "0" * 64
        elif change == "payload":
            record.payload_json = "{}"
        else:
            record.payload_json = " " + record.payload_json
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError):
        load(manager, ref, trajectory, contract)
    if change in {"payload", "noncanonical"}:
        with pytest.raises(evidence.TrajectoryEvidenceUnavailableError):
            store(manager, trajectory, contract)
        with manager.get_session() as session:
            assert session.get(TrajectoryEvidenceArtifactRecord, ref.artifact_id).payload_json != canonical_dumps(trajectory)


def test_missing_artifact_and_mismatched_ref_never_fall_back_to_online_trace(db):
    manager, trajectory, contract = db
    ref = store(manager, trajectory, contract)
    other = ref.model_copy(update={"trajectory_digest": "sha256:" + "0" * 64})
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="REF_MISMATCH"):
        load(manager, other, trajectory, contract)
    with manager.get_session() as session:
        session.execute(delete(TrajectoryEvidenceArtifactRecord))
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="MISSING"):
        load(manager, ref, trajectory, contract)


@pytest.mark.parametrize("location", ["input", "output", "argument", "result", "name", "masked-value"])
def test_correct_digest_does_not_allow_unapproved_or_hidden_content_into_storage(db, location):
    manager, trajectory, contract = db
    def change(payload):
        if location == "input":
            payload["input_redacted"] = {"token": "synthetic-secret"}
        elif location == "output":
            payload["output_redacted"]["Authorization"] = "synthetic-secret"
        elif location == "argument":
            payload["steps"][1]["arguments_redacted"]["token"] = "synthetic-secret"
        elif location == "result":
            payload["steps"][2]["result_summary"]["token"] = "synthetic-secret"
        elif location == "name":
            payload["steps"][1]["raw_name"] = "synthetic-secret"
        else:
            payload["steps"][1]["argument_visibility"]["$.customer_id"] = "REDACTED"
    unsafe = reseal(trajectory, change)
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="REDACTION_INVALID") as caught:
        store(manager, unsafe, contract)
    assert "synthetic-secret" not in str(caught.value)
    with manager.get_session() as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 0


def test_complete_inventory_claim_requires_the_persisted_event_step_mapping(db):
    manager, trajectory, contract = db
    unsafe = reseal(trajectory, lambda p: p["steps"][-1]["result_summary"]["terminal_inventory"]["observed_events"].pop())
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="INVENTORY_INVALID"):
        store(manager, unsafe, contract)
    assert not evidence.inventory_complete(unsafe, contract)


def test_complete_inventory_cannot_hide_a_business_step_without_workflow(db):
    manager, trajectory, contract = db
    def change(payload):
        extra = deepcopy(payload["steps"][2])
        extra.update(step_id="orphan-business", observation_id="orphan-business", index=len(payload["steps"]),
                     workflow_scope_id=None, causal_predecessor_ids=[])
        payload["steps"].append(extra)
    unsafe = reseal(trajectory, change)
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="INVENTORY_INVALID"):
        store(manager, unsafe, contract)


@pytest.mark.parametrize("location", ["warning", "error_code"])
def test_diagnostic_fields_cannot_store_arbitrary_reason_shaped_content(db, location):
    manager, trajectory, contract = db
    secret = "SYNTHETIC_SECRET_ABC123"
    def change(payload):
        if location == "warning":
            payload["completeness"]["warnings"].append(secret)
        else:
            payload["steps"][2]["error_code"] = secret
    unsafe = reseal(trajectory, change)
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="REASON_INVALID") as caught:
        store(manager, unsafe, contract)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("path", ["$.terminal_inventory.observed_events", "$.terminal_inventory",
                                "$.terminal_inventory.observed_events[0].event_id"])
def test_contract_cannot_allowlist_the_reserved_inventory_mapping_path(db, path):
    manager, trajectory, contract = db
    changed = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
        "result_paths": contract.redaction_policy.result_paths + [path]})
    rows, _, _ = prepared()
    trajectory = assemble_trajectory(trajectory.scope, batch(rows, changed, trajectory.scope), changed)
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="CONTRACT_INVALID"):
        store(manager, trajectory, changed)


def test_complete_inventory_exempts_only_frozen_structure_roles_with_their_safe_shape(db):
    manager, original, contract = db
    from app.trajectory.contract import TrajectoryContract
    from test_trajectory_contract import version
    payload = contract.model_dump()
    profile = next(p for p in payload["source_profile_set"] if p["profile_id"] == "native-otel-v1")
    profile["structural_roles"] = [{"provider_type": "SPAN", "raw_name": "business-wrapper", "step_type": "AGENT",
        "safe_name": "business-wrapper", "metadata_equals": {"source": "safe-fixture"}}]
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    rows, _, _ = prepared()
    wrapper = deepcopy(rows[0]) | {"id": "wrapper", "type": "SPAN", "name": "business-wrapper",
        "parentObservationId": original.scope.root_observation_id, "metadata": {"source": "safe-fixture"}}
    trajectory = assemble_trajectory(original.scope, batch(rows + [wrapper], contract, original.scope), contract)
    assert trajectory.completeness.status == "OBSERVED_COMPLETE"
    assert evidence.inventory_complete(trajectory, contract)
    assert load(manager, store(manager, trajectory, contract), trajectory, contract) == trajectory
    unsafe = reseal(trajectory, lambda p: next(s for s in p["steps"] if s["step_id"] == "wrapper").update(status="SUCCEEDED"))
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="INVENTORY_INVALID"):
        store(manager, unsafe, contract)


def test_lower_artifact_budget_and_contract_identity_are_checked_before_insert(db):
    manager, trajectory, contract = db
    lower = configured(contract, limits={"max_artifact_bytes": 1})
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="PROVENANCE_MISMATCH"):
        store(manager, trajectory, lower)
    unsafe = reseal(trajectory, lambda p: p["provenance"].update(trajectory_contract_digest=lower.digest))
    with pytest.raises(evidence.TrajectoryEvidenceUnavailableError, match="BYTE_LIMIT"):
        store(manager, unsafe, lower)


@pytest.mark.parametrize("visibility", ["MISSING", "REDACTED", "UNCOLLECTED", "INVALID"])
def test_genuine_assembled_parameter_states_survive_frozen_storage(db, visibility):
    manager, original, contract = db
    rows, _, _ = prepared()
    rows[1]["metadata"]["attributes.workflow.arguments_visibility"] = visibility
    rows[1]["input"] = {} if visibility == "MISSING" else "broken" if visibility == "INVALID" else None
    if visibility == "REDACTED":
        rows[1]["metadata"]["attributes.workflow.hidden_paths"] = ["$.customer_id"]
    trajectory = assemble_trajectory(original.scope, batch(rows, contract, original.scope), contract)
    ref = store(manager, trajectory, contract)
    assert canonical_dumps(load(manager, ref, trajectory, contract)) == canonical_dumps(trajectory)


def test_partial_inventory_and_root_only_are_storable_without_becoming_complete(db):
    manager, original, contract = db
    rows, _, _ = prepared()
    for source in (rows[:1], rows[:3] + rows[-1:]):
        trajectory = assemble_trajectory(original.scope, batch(source, contract, original.scope), contract)
        ref = store(manager, trajectory, contract)
        restored = load(manager, ref, trajectory, contract)
        assert not evidence.inventory_complete(restored, contract)
        assert restored.completeness.status in {"ROOT_ONLY", "PARTIAL"}


def test_loader_does_not_flush_or_discard_the_callers_pending_orm_changes(db):
    manager, trajectory, contract = db
    ref = store(manager, trajectory, contract)
    with Session(manager.engine) as session:
        row = session.get(TrajectoryEvidenceArtifactRecord, ref.artifact_id)
        row.payload_json = "{}"
        restored = evidence.load_trajectory_evidence(session, ref, scope=trajectory.scope, contract=contract)
        assert restored == trajectory and row.payload_json == "{}"
        session.rollback()


def test_array_parameter_invalid_state_is_safe_even_when_the_empty_placeholder_is_an_object(db):
    manager, original, contract = db
    payload = contract.model_dump()
    payload["redaction_policy"]["argument_paths"] = ["$[0]"]
    from app.trajectory.contract import TrajectoryContract
    from test_trajectory_contract import version
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    rows, _, _ = prepared()
    rows[1]["metadata"]["attributes.workflow.arguments_visibility"] = "INVALID"
    rows[1]["input"] = "broken"
    trajectory = assemble_trajectory(original.scope, batch(rows, contract, original.scope), contract)
    assert trajectory.steps[1].argument_visibility == {"$[0]": "INVALID"}
    ref = store(manager, trajectory, contract)
    assert load(manager, ref, trajectory, contract) == trajectory


@pytest.mark.parametrize("location", ["root", "tool", "event"])
def test_allowlisted_business_result_named_terminal_inventory_does_not_become_the_event_protocol(db, location):
    manager, original, contract = db
    payload = contract.model_dump()
    field = "output_paths" if location == "root" else "result_paths"
    payload["redaction_policy"][field].append("$.terminal_inventory.account_count")
    from app.trajectory.contract import TrajectoryContract
    from test_trajectory_contract import version
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    rows, _, _ = prepared()
    if location == "event":
        from test_trajectory_causal_links import event
        progress = event(rows[1], "progress", 5, "TERMINAL", "4" * 32)
        progress.update(type="SPAN", name="workflow.terminal", output={"terminal_inventory": {"account_count": 2}})
        terminal = event(rows[1], "terminal", 6, "TERMINAL", "3" * 32)
        terminal.update(type="SPAN", name="workflow.terminal")
        terminal["metadata"].update({"attributes.workflow.event_count": 6,
            "attributes.workflow.event_ids": [f"{n:032x}" for n in range(1, 7)], "attributes.workflow.final_sequence": 6})
        rows = rows[:-1] + [progress, terminal]
    else:
        rows[0 if location == "root" else 2]["output"] = {"terminal_inventory": {"account_count": 2}}
    trajectory = assemble_trajectory(original.scope, batch(rows, contract, original.scope), contract)
    ref = store(manager, trajectory, contract)
    assert evidence.inventory_complete(trajectory, contract)
    assert load(manager, ref, trajectory, contract) == trajectory


def test_postgres_concurrent_writers_return_one_insert_only_reference(postgres_engine):
    trajectory, contract = artifact()
    anchor(postgres_engine, trajectory)
    ready = Barrier(4)
    def write(_):
        with Session(postgres_engine) as session, session.begin():
            ready.wait(timeout=5)
            return evidence.persist_trajectory_evidence(session, trajectory, contract)
    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = list(pool.map(write, range(4)))
    assert all(ref == refs[0] for ref in refs)
    with Session(postgres_engine) as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 1
        restored = evidence.load_trajectory_evidence(session, refs[0], scope=trajectory.scope, contract=contract)
        assert canonical_dumps(restored) == canonical_dumps(trajectory)


@pytest.fixture
def postgres_engine():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL absent; requires isolated PostgreSQL")
    admin = create_engine(url)
    schema = "s4_evidence_repo_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(admin.url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        MigrationRunner(engine, ROOT / "migrations").apply_all()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()
