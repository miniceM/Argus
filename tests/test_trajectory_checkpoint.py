from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest
from app.db_models import ExecutionCheckpointRecord, ExperimentItemExecutionRecord, ExperimentLaunchRecord
from app.evaluator_binding import canonical_digest
from app.execution_checkpoint import canonical_output_digest
from app.trajectory.assembler import assemble_trajectory
from app.trajectory.checkpoint import (
    TrajectoryCheckpointError,
    abort_trajectory_round,
    begin_trajectory_round,
    load_trajectory_checkpoint,
    publish_trajectory_round,
)
from app.trajectory.codec import canonical_dumps
from app.trajectory.evidence import persist_trajectory_evidence
from app.trajectory.schema import TrajectoryAcquisition
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_trajectory_assembler import batch, prepared
from test_trajectory_evidence import anchor, artifact
from test_trajectory_evidence import db as evidence_db
from test_trajectory_evidence import postgres_engine as evidence_postgres_engine


@pytest.fixture
def db(tmp_path):
    yield from evidence_db.__wrapped__(tmp_path)


@pytest.fixture
def context(db):
    manager, trajectory, contract = db
    now = datetime.now(UTC)
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, "item-1")
        item.execution_status, item.active_attempt_id = "running", "attempt-1"
        item.lease_token, item.lease_expires_at = "lease-1", now + timedelta(hours=1)
        checkpoint = session.get(ExecutionCheckpointRecord, "checkpoint")
        checkpoint.agent_output = {"answer": "synthetic"}
        checkpoint.output_digest = canonical_output_digest(checkpoint.agent_output)
        launch = session.get(ExperimentLaunchRecord, "launch-1")
        launch.manifest = launch.manifest | {"dataset": {"items": [{"id": trajectory.scope.dataset_item_id,
            "input": checkpoint.input_payload, "expected_output": checkpoint.expected_output}]}}
        checkpoint.manifest_digest = canonical_digest(launch.manifest)
        checkpoint.created_at = checkpoint.updated_at = now
    return manager, trajectory.scope, contract, now


def begin(context, *, lease="lease-1", elapsed=0):
    manager, scope, contract, now = context
    with manager.get_session() as session:
        return begin_trajectory_round(session, scope, contract, lease_token=lease, now=now + timedelta(seconds=elapsed))


def view(context):
    manager, scope, contract, _ = context
    with manager.get_session() as session:
        return load_trajectory_checkpoint(session, scope, contract)


def pending(claim):
    return TrajectoryAcquisition(scope=claim.acquisition.scope, state="PENDING", completeness=None,
        evidence_ref=None, reason_code="TRAJECTORY_ACQUISITION_PENDING", retryable=True,
        deadline_at=claim.acquisition.deadline_at)


def candidate(context):
    _, scope, contract, _ = context
    rows, _, _ = prepared()
    return assemble_trajectory(scope, batch(rows[:1], contract, scope), contract)


def publish(context, claim, *, used=100, result=None, evidence=None, lease="lease-1", elapsed=1):
    manager, _, contract, now = context
    with manager.get_session() as session:
        return publish_trajectory_round(session, claim, contract, lease_token=lease,
            result=result or pending(claim), source_bytes_used=used, candidate=evidence,
            now=now + timedelta(seconds=elapsed))


def test_round_reserves_before_io_and_preserves_original_deadline(context):
    manager, _, contract, now = context
    first = begin(context)
    assert first.source_bytes_used == 0 and first.round_token
    assert first.acquisition.deadline_at == now + timedelta(seconds=float(contract.limits.deadline_seconds))
    assert first.acquisition.state == "PENDING" and first.acquisition.evidence_ref is None
    assert view(context).source_bytes_used == contract.limits.max_source_bytes
    with manager.get_session() as session:
        state = session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json
        assert "observations" not in state and "payload_json" not in state
    saved = publish(context, first, evidence=candidate(context))
    assert saved.source_bytes_used == 100 and saved.acquisition.evidence_ref is None
    assert saved.candidate_ref and saved.candidate.completeness.status == "ROOT_ONLY"
    second = begin(context, elapsed=2)
    assert second.source_bytes_used == 100 and second.acquisition.deadline_at == first.acquisition.deadline_at
    assert publish(context, second, used=150, evidence=candidate(context)).source_bytes_used == 150


def test_crashed_round_new_lease_cannot_reset_reserved_budget(context):
    manager, _, contract, now = context
    first = begin(context)
    with pytest.raises(TrajectoryCheckpointError, match="ROUND_BUSY"):
        begin(context)
    with manager.get_session() as session:
        session.get(ExperimentItemExecutionRecord, "item-1").lease_token = "lease-2"
    recovered = begin(context, lease="lease-2", elapsed=2)
    assert recovered.round_token != first.round_token
    assert recovered.source_bytes_used == contract.limits.max_source_bytes
    assert recovered.acquisition.deadline_at == first.acquisition.deadline_at
    with pytest.raises(TrajectoryCheckpointError, match="LEASE_LOST"):
        publish(context, first)


def test_abort_consumes_reservation_and_late_publish_cannot_overwrite(context):
    first = begin(context)
    with context[0].get_session() as session:
        assert abort_trajectory_round(session, first, context[2], lease_token="lease-1", now=context[3])
    assert view(context).source_bytes_used == context[2].limits.max_source_bytes
    with pytest.raises(TrajectoryCheckpointError, match="ROUND_STALE"):
        publish(context, first)


@pytest.mark.parametrize("failure", ["generation", "lease", "expired", "cancelled", "item-status", "attempt"])
def test_fences_reject_before_reservation_or_candidate_write(context, failure):
    manager, _, _, now = context
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, "item-1")
        if failure == "generation":
            item.dispatch_generation += 1
        elif failure == "lease":
            item.lease_token = "other"
        elif failure == "expired":
            item.lease_expires_at = now
        elif failure == "item-status":
            item.execution_status = "succeeded"
        elif failure == "attempt":
            item.active_attempt_id = "attempt-2"
        else:
            session.get(ExperimentLaunchRecord, "launch-1").cancel_requested_at = now
    with pytest.raises(TrajectoryCheckpointError):
        begin(context)
    with manager.get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        assert cp.trajectory_acquisition_json is None and cp.trajectory_runtime_json is None


@pytest.mark.parametrize("field", ["agent_output", "input_payload", "expected_output", "binding_provenance", "manifest", "trace_id"])
def test_changed_frozen_source_is_rejected_and_does_not_refresh_deadline(context, field):
    claim = begin(context)
    publish(context, claim, evidence=candidate(context))
    with context[0].get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        if field == "manifest":
            session.get(ExperimentLaunchRecord, "launch-1").manifest = {"schema_version": "1.2", "frozen": "changed"}
        else:
            setattr(cp, field, "changed" if field == "trace_id" else [{"binding_id": "other"}] if field == "binding_provenance" else {"changed": True})
    with pytest.raises(TrajectoryCheckpointError):
        begin(context, elapsed=2)


def test_expired_pending_restores_real_candidate_without_a_source_query(context):
    first = begin(context)
    saved = publish(context, first, evidence=candidate(context))
    final = begin(context, elapsed=float(context[2].limits.deadline_seconds))
    assert final.round_token is None and final.acquisition.state == "READY"
    assert final.acquisition.evidence_ref == saved.candidate_ref
    assert final.acquisition.completeness.status == "ROOT_ONLY"
    assert canonical_dumps(view(context).candidate) == canonical_dumps(saved.candidate)


def test_expired_pending_without_candidate_is_explicitly_unavailable(context):
    begin(context)
    final = begin(context, elapsed=float(context[2].limits.deadline_seconds))
    assert final.acquisition.state == "UNAVAILABLE" and final.acquisition.reason_code == "TRAJECTORY_ROOT_UNAVAILABLE"
    assert final.acquisition.evidence_ref is None


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_ready_recovery_reports_broken_artifact_without_repairing_history(context, damage):
    from app.db_models import TrajectoryEvidenceArtifactRecord
    first = begin(context)
    artifact = candidate(context)
    with context[0].get_session() as session:
        ref = persist_trajectory_evidence(session, artifact, context[2])
    ready = TrajectoryAcquisition(scope=context[1], state="READY", completeness=artifact.completeness,
        evidence_ref=ref, reason_code=None, retryable=False, deadline_at=first.acquisition.deadline_at)
    publish(context, first, result=ready)
    with context[0].get_session() as session:
        original = deepcopy(session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_acquisition_json)
        if damage == "missing":
            session.execute(delete(TrajectoryEvidenceArtifactRecord))
        else:
            session.get(TrajectoryEvidenceArtifactRecord, ref.artifact_id).payload_json = "{}"
    restored = view(context)
    assert restored.acquisition.state == "UNAVAILABLE" and restored.acquisition.evidence_ref is None
    assert restored.acquisition.reason_code.startswith("TRAJECTORY_EVIDENCE_")
    with context[0].get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_acquisition_json == original


def test_candidate_and_checkpoint_roll_back_as_one_transaction(context):
    from app.db_models import TrajectoryEvidenceArtifactRecord
    first = begin(context)
    with pytest.raises(RuntimeError, match="owner-rollback"):
        with context[0].get_session() as session:
            publish_trajectory_round(session, first, context[2], lease_token="lease-1", result=pending(first),
                source_bytes_used=100, candidate=candidate(context), now=context[3])
            raise RuntimeError("owner-rollback")
    with context[0].get_session() as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 0
    assert view(context).source_bytes_used == context[2].limits.max_source_bytes


@pytest.mark.parametrize("used", [True, -1, 8388609])
def test_invalid_wire_accounting_is_not_coerced_or_committed(context, used):
    first = begin(context)
    with pytest.raises(TrajectoryCheckpointError, match="BUDGET_INVALID"):
        publish(context, first, used=used)


def test_external_reason_text_cannot_enter_checkpoint_json(context):
    first = begin(context)
    result = pending(first).model_copy(update={"reason_code": "SYNTHETIC_SECRET_ABC123"})
    with pytest.raises(TrajectoryCheckpointError, match="STATE_INVALID"):
        publish(context, first, result=result)
    assert "SYNTHETIC_SECRET" not in canonical_dumps(view(context).acquisition)


@pytest.mark.parametrize("field", ["input_payload", "expected_output"])
def test_original_checkpoint_inputs_must_match_frozen_dataset_before_initialization(context, field):
    with context[0].get_session() as session:
        setattr(session.get(ExecutionCheckpointRecord, "checkpoint"), field, {"changed": True})
    with pytest.raises(TrajectoryCheckpointError, match="INPUT_MISMATCH"):
        begin(context)


def test_unbound_actual_profile_mismatch_fails_before_any_reservation(context):
    from app.db_models import AgentVersionRecord
    with context[0].get_session() as session:
        session.get(AgentVersionRecord, "version").trajectory_source_profile_id = "http-only-v1"
    with pytest.raises(TrajectoryCheckpointError, match="SOURCE_INVALID"):
        begin(context)


def test_foreign_candidate_is_rejected_before_another_items_artifact_insert(context):
    from app.db_models import TrajectoryEvidenceArtifactRecord
    from test_trajectory_evidence import reseal
    claim = begin(context)
    original = candidate(context)
    other = reseal(original, lambda p: p["scope"].update(launch_id="launch-2", item_execution_id="item-2", attempt_id="attempt-2"))
    with context[0].get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        session.add(ExecutionCheckpointRecord(id="other-cp", item_execution_id="item-2", launch_id="launch-2",
            dataset_item_id=cp.dataset_item_id, dispatch_generation=1, output_digest=cp.output_digest,
            agent_output=cp.agent_output, input_payload=cp.input_payload, expected_output=cp.expected_output,
            binding_provenance=[], manifest_digest=cp.manifest_digest, final_attempt_id="attempt-2",
            trace_id=other.scope.trace_id, observation_id=other.scope.root_observation_id, expires_at=cp.expires_at))
    # 调用方即使捕获错误后提交，也不能留下跨 Item 的制品或状态。
    with context[0].get_session() as session:
        with pytest.raises(TrajectoryCheckpointError, match="SCOPE_MISMATCH"):
            publish_trajectory_round(session, claim, context[2], lease_token="lease-1", result=pending(claim),
                source_bytes_used=100, candidate=other, now=context[3])
    with context[0].get_session() as session:
        assert session.scalar(select(func.count()).select_from(TrajectoryEvidenceArtifactRecord)) == 0


def test_live_reservation_cannot_be_tampered_to_return_unspent_bytes(context):
    begin(context)
    with context[0].get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        cp.trajectory_runtime_json = cp.trajectory_runtime_json | {"charged_bytes": 1}
    with pytest.raises(TrajectoryCheckpointError, match="STATE_INVALID"):
        view(context)


def test_pending_publish_after_original_deadline_is_rejected(context):
    first = begin(context)
    with pytest.raises(TrajectoryCheckpointError, match="DEADLINE_EXPIRED"):
        publish(context, first, elapsed=float(context[2].limits.deadline_seconds) + 1)


def test_lease_expiring_during_artifact_validation_cannot_publish(context, monkeypatch):
    import app.trajectory.checkpoint as module
    first = begin(context)
    current = context[3]
    original = module.persist_trajectory_evidence
    def slow(*args, **kwargs):
        nonlocal current
        result = original(*args, **kwargs)
        current += timedelta(hours=2)
        return result
    monkeypatch.setattr(module, "persist_trajectory_evidence", slow)
    monkeypatch.setattr(module, "_now", lambda value: current)
    with pytest.raises(TrajectoryCheckpointError, match="LEASE_LOST"):
        publish(context, first, evidence=candidate(context))
    assert view(context).source_bytes_used == context[2].limits.max_source_bytes


def test_locked_boundary_preserves_pending_changes_instead_of_refreshing_them_away(context):
    with context[0].get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        cp.expected_output = {"synthetic": "must-retain"}
        with pytest.raises(TrajectoryCheckpointError, match="TRANSACTION_DIRTY"):
            begin_trajectory_round(session, context[1], context[2], lease_token="lease-1", now=context[3])
        assert cp.expected_output == {"synthetic": "must-retain"}
        session.rollback()


def test_ready_pointer_and_candidate_survive_ttl_and_new_generation(context):
    first = begin(context)
    saved = publish(context, first, evidence=candidate(context))
    final = begin(context, elapsed=float(context[2].limits.deadline_seconds))
    with context[0].get_session() as session:
        session.get(ExecutionCheckpointRecord, "checkpoint").expires_at = context[3] - timedelta(days=1)
        session.get(ExperimentItemExecutionRecord, "item-1").dispatch_generation += 1
    restored = view(context)
    assert restored.acquisition == final.acquisition and restored.candidate_ref == saved.candidate_ref
    with pytest.raises(TrajectoryCheckpointError, match="GENERATION_STALE"):
        begin(context)


@pytest.fixture
def postgres_engine():
    yield from evidence_postgres_engine.__wrapped__()


@pytest.fixture
def postgres_context(postgres_engine):
    trajectory, contract = artifact()
    anchor(postgres_engine, trajectory)
    class Sessions:
        @contextmanager
        def get_session(self):
            with Session(postgres_engine) as session, session.begin():
                yield session
    return context.__wrapped__((Sessions(), trajectory, contract))


def test_postgres_two_workers_reserve_only_one_round(postgres_context):
    start = Barrier(2)
    def reserve(_):
        start.wait(timeout=5)
        try:
            return begin(postgres_context)
        except TrajectoryCheckpointError as error:
            return error.reason_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert results.count("TRAJECTORY_CHECKPOINT_ROUND_BUSY") == 1
    stored = view(postgres_context)
    assert stored.revision == 1 and stored.source_bytes_used == postgres_context[2].limits.max_source_bytes


def test_postgres_two_publishers_commit_one_candidate_and_no_duplicate_update(postgres_context):
    first, source = begin(postgres_context), candidate(postgres_context)
    start = Barrier(2)
    def finish(_):
        start.wait(timeout=5)
        try:
            return publish(postgres_context, first, evidence=source)
        except TrajectoryCheckpointError as error:
            return error.reason_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert results.count("TRAJECTORY_CHECKPOINT_ROUND_STALE") == 1
    assert view(postgres_context).candidate == source


def test_postgres_lease_time_is_checked_after_waiting_for_the_item_lock(postgres_context):
    import time
    manager, scope, contract, _ = postgres_context
    entered = Event()
    def reserve():
        entered.set()
        with manager.get_session() as session:
            return begin_trajectory_round(session, scope, contract, lease_token="lease-1")
    with ThreadPoolExecutor(max_workers=1) as pool:
        with manager.get_session() as session:
            item = session.scalar(select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.id == scope.item_execution_id).with_for_update())
            item.lease_expires_at = datetime.now(UTC) + timedelta(seconds=0.1)
            session.flush()
            future = pool.submit(reserve)
            assert entered.wait(timeout=5)
            time.sleep(0.2)
            assert not future.done()
        with pytest.raises(TrajectoryCheckpointError, match="LEASE_LOST"):
            future.result(timeout=5)


def test_deadline_crossed_during_candidate_validation_cannot_store_pending(context, monkeypatch):
    import app.trajectory.checkpoint as module
    first = begin(context)
    current, original = context[3], module.persist_trajectory_evidence
    def slow(*args, **kwargs):
        nonlocal current
        value = original(*args, **kwargs)
        current += timedelta(seconds=float(context[2].limits.deadline_seconds) + 1)
        return value
    monkeypatch.setattr(module, "persist_trajectory_evidence", slow)
    monkeypatch.setattr(module, "_now", lambda value: current)
    with pytest.raises(TrajectoryCheckpointError, match="DEADLINE_EXPIRED"):
        publish(context, first, evidence=candidate(context))


def test_invalid_frozen_binding_returns_a_safe_reason_without_external_text(context):
    with context[0].get_session() as session:
        launch = session.get(ExperimentLaunchRecord, "launch-1")
        launch.manifest = launch.manifest | {"evaluators": ["SYNTHETIC_SECRET_ABC123"]}
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        cp.manifest_digest = canonical_digest(launch.manifest)
    with pytest.raises(TrajectoryCheckpointError, match="SOURCE_INVALID") as error:
        begin(context)
    assert "SYNTHETIC_SECRET" not in str(error.value)
