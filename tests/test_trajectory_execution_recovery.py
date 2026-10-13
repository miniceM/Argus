"""成功响应后失去执行租约：恢复原 Attempt，不再次调用 Agent。"""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.db_models import (
    AgentRecord,
    AgentVersionRecord,
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from app.evaluator_binding import manifest_measurement_digest
from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract
from test_issue_84_eval_only_retry import _frozen_binding, _manifest, _ok_invocation
from test_trajectory_assembler import prepared
from test_trajectory_evidence import postgres_engine as evidence_postgres_engine
from test_trajectory_worker import context as worker_context
from test_trajectory_worker import db as worker_database


@pytest.fixture
def postgres_engine():
    yield from evidence_postgres_engine.__wrapped__()


@pytest.fixture(params=["sqlite", "postgres"])
def setup_runtime(request, tmp_path):
    if request.param == "sqlite":
        from conftest import setup_runtime as original
        runtime = original.__wrapped__(tmp_path)
        yield runtime
        runtime[0].engine.dispose()
        return
    from app.limiter import MemoryAgentLimiter
    from app.orchestrator import LaunchOrchestrator
    from app.queue import MemoryQueueAdapter
    from app.reconciler import ExecutionReconciler
    from app.worker import ExecutionWorker
    from sqlalchemy.orm import Session
    engine = request.getfixturevalue("postgres_engine")
    @contextmanager
    def sessions():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
    manager = SimpleNamespace(engine=engine, get_session=sessions)
    with sessions() as session:
        session.add(AgentRecord(id="test-agent", name="Synthetic Agent"))
        session.flush()
        session.add(AgentVersionRecord(id="test-agent-v1", agent_id="test-agent", version="v1",
            endpoint="http://localhost/invoke", spec_digest="sha256:abc"))
    queue, limiter = MemoryQueueAdapter(), MemoryAgentLimiter()
    yield (manager, queue, limiter, LaunchOrchestrator(manager, queue, limiter),
           ExecutionWorker(manager, queue, limiter), ExecutionReconciler(manager, queue, limiter))


def frozen_manifest(*, idempotent=False):
    manifest = _manifest(_frozen_binding("intent_match"))
    _, contract, _ = prepared()
    manifest["schema_version"] = "1.3"
    manifest["agent"]["is_idempotent"] = idempotent
    manifest["agent"]["trajectory_source_profile_id"] = "native-otel-v1"
    manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract),
        SimpleNamespace(trajectory_source_profile_id="native-otel-v1"))
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
    return manifest


def crashed_response(runtime, *, idempotent=False):
    manager, queue, _, orchestrator, worker, _ = runtime
    manifest = frozen_manifest(idempotent=idempotent)
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", "response-crash", manifest)
    orchestrator.start_launch(launch.id)
    message = queue.read_group("response-crash", count=1)[0]
    class CrashCollector:
        async def collect(self, *args, **kwargs):
            raise asyncio.CancelledError
    worker.trajectory_collector = CrashCollector()
    with patch("app.worker.get_langfuse_client_safe", return_value=None), patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        invoke.return_value = _ok_invocation({"intent": "refund"})
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(worker.execute_item_message(*message))
        assert invoke.await_count == 1
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        checkpoint = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=item.id).one()
        original = (item.dispatch_generation, item.active_attempt_id, item.lease_token,
                    checkpoint.created_at, checkpoint.output_digest)
        item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    return message, manifest, original


@pytest.mark.parametrize("idempotent", [False, True])
def test_reconciler_preserves_successful_response_scope_without_consuming_retry_budget(setup_runtime, idempotent):
    message, _, original = crashed_response(setup_runtime, idempotent=idempotent)
    assert setup_runtime[5].reconcile_expired_leases() == 1
    with setup_runtime[0].get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        attempt = session.get(ExecutionAttemptRecord, original[1])
        assert item.execution_status == "queued" and item.dispatch_generation == original[0]
        assert item.active_attempt_id == original[1] and attempt.status == "RUNNING"
        assert item.lease_token is None


def test_worker_resumes_original_response_before_agent_permits_credentials_or_new_attempt(setup_runtime):
    message, _, original = crashed_response(setup_runtime)
    manager, queue, limiter, _, worker, reconciler = setup_runtime
    reconciler.reconcile_expired_leases()
    original_worker_id = worker.worker_id
    worker.worker_id = "different-recovery-worker"
    collected = []
    class Collector:
        async def collect(self, manifest, **args):
            collected.append(args)
            with manager.get_session() as session:
                cp = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=message[1]).one()
                assert (cp.created_at, cp.output_digest, cp.final_attempt_id) == (original[3], original[4], original[1])
    worker.trajectory_collector = Collector()
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke, patch.object(
        limiter, "acquire_rate_permit", side_effect=AssertionError("resume consumed Agent rate")), patch.object(
        limiter, "acquire_concurrency_permit", side_effect=AssertionError("resume consumed Agent concurrency")):
        assert asyncio.run(worker.execute_item_message("resume-message", message[1], original[0])) is True
        assert invoke.await_count == 0
    assert len(collected) == 1 and collected[0]["lease_token"] != original[2]
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        assert item.execution_status == "succeeded" and item.eval_status == "succeeded"
        assert item.quality_conclusion == "pass" and item.final_attempt_id == original[1]
        assert session.get(ExecutionAttemptRecord, original[1]).worker_id == original_worker_id
        assert session.query(ExecutionAttemptRecord).filter_by(item_execution_id=item.id).count() == 1
    assert worker.claim_item(message[1], original[0]) is None


@pytest.mark.parametrize("failure", ["digest", "manifest", "expired"])
def test_unusable_saved_response_cannot_degrade_to_another_agent_call(setup_runtime, failure):
    message, _, original = crashed_response(setup_runtime)
    manager, _, _, _, worker, reconciler = setup_runtime
    with manager.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=message[1]).one()
        if failure == "digest":
            cp.output_digest = "sha256:" + "0" * 64
        elif failure == "manifest":
            cp.manifest_digest = "sha256:" + "0" * 64
        else:
            cp.expires_at = datetime.now(UTC) - timedelta(seconds=10)
    reconciler.reconcile_expired_leases()
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        assert asyncio.run(worker.execute_item_message("invalid-response", message[1], original[0])) is True
        assert invoke.await_count == 0
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        assert item.eval_status == "failed" and item.quality_conclusion == "unknown" and not item.scores
        assert item.eval_error.startswith("TRAJECTORY_RECOVERY_")


def test_old_lease_cannot_overwrite_the_original_successful_response(setup_runtime):
    message, manifest, original = crashed_response(setup_runtime)
    manager, _, _, _, worker, reconciler = setup_runtime
    reconciler.reconcile_expired_leases()
    claim = worker.claim_item(message[1], original[0])
    assert claim and claim["lease_token"] != original[2]
    assert worker._persist_execution_checkpoint(item_id=message[1], launch_id=claim["launch_id"],
        dataset_item_id="0", generation=original[0], output={"intent": "late-overwrite"}, dataset_input={},
        expected_output={}, manifest=manifest, final_attempt_id=original[1], trace_id=None, trace_url=None,
        observation_id=None, lease_token=original[2]) is False
    with manager.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=message[1]).one()
        assert cp.agent_output == {"intent": "refund"} and cp.output_digest == original[4]


@pytest.mark.parametrize("when", ["before_resume", "during_evaluation"])
def test_cancellation_serializes_with_resumed_finalization(setup_runtime, when):
    message, _, original = crashed_response(setup_runtime)
    manager, _, _, _, worker, reconciler = setup_runtime
    reconciler.reconcile_expired_leases()
    def cancel():
        with manager.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, message[1])
            session.get(ExperimentLaunchRecord, item.launch_id).cancel_requested_at = datetime.now(UTC)
    if when == "before_resume":
        cancel()
    class Collector:
        async def collect(self, *args, **kwargs):
            return None
    worker.trajectory_collector = Collector()
    from app.evaluator_binding import evaluate_frozen_item
    def evaluate(*args, **kwargs):
        cancel()
        return evaluate_frozen_item(*args, **kwargs)
    with patch("app.trajectory.execution_recovery.evaluate_frozen_item", side_effect=evaluate), patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        assert asyncio.run(worker.execute_item_message("cancel-resume", message[1], original[0])) is True
        assert invoke.await_count == 0
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        assert item.execution_status == "cancelled" and item.eval_status == "skipped"
        assert item.quality_conclusion == "unknown" and not item.scores
        assert session.get(ExecutionAttemptRecord, original[1]).status == "SUCCEEDED"


def test_current_owner_also_cannot_refresh_or_replace_a_saved_response(setup_runtime):
    from app.trajectory.execution_recovery import _claim_response
    message, manifest, original = crashed_response(setup_runtime)
    manager, _, _, _, worker, reconciler = setup_runtime
    reconciler.reconcile_expired_leases()
    claim = worker.claim_item(message[1], original[0])
    _claim_response(worker, message[1], original[0], claim["lease_token"])
    assert worker._persist_execution_checkpoint(item_id=message[1], launch_id=claim["launch_id"],
        dataset_item_id="0", generation=original[0], output={"intent": "replacement"}, dataset_input={},
        expected_output={}, manifest=manifest, final_attempt_id=original[1], trace_id=None, trace_url=None,
        observation_id=None, lease_token=claim["lease_token"]) is True
    with manager.get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=message[1]).one()
        assert (cp.created_at, cp.output_digest, cp.agent_output) == (original[3], original[4], {"intent": "refund"})


@pytest.mark.parametrize("cause", ["lost_lease", "external_cancel"])
def test_interrupted_resume_preserves_response_and_does_not_finalize(setup_runtime, cause):
    message, _, original = crashed_response(setup_runtime)
    manager, _, _, _, worker, reconciler = setup_runtime
    reconciler.reconcile_expired_leases()
    class Collector:
        async def collect(self, *args, **kwargs):
            if cause == "lost_lease":
                kwargs["cancelled"].set()
                return None
            raise asyncio.CancelledError
    worker.trajectory_collector = Collector()
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        if cause == "external_cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(worker.execute_item_message("interrupted-resume", message[1], original[0]))
        else:
            assert asyncio.run(worker.execute_item_message("interrupted-resume", message[1], original[0])) is False
        assert invoke.await_count == 0
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        cp = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=item.id).one()
        assert item.execution_status == "running" and item.active_attempt_id == original[1]
        assert (cp.created_at, cp.output_digest) == (original[3], original[4])
        assert session.query(ExecutionAttemptRecord).filter_by(item_execution_id=item.id).count() == 1


def test_invalid_original_attempt_clears_stale_scores_without_authorizing_an_agent_call(setup_runtime):
    message, _, original = crashed_response(setup_runtime)
    manager, _, _, _, worker, reconciler = setup_runtime
    reconciler.reconcile_expired_leases()
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        item.scores, item.quality_evaluation = {"intent_match": 1.0}, {"quality_conclusion": "pass"}
        session.get(ExecutionAttemptRecord, original[1]).status = "FAILED"
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        assert asyncio.run(worker.execute_item_message("invalid-attempt", message[1], original[0])) is True
        assert invoke.await_count == 0
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        assert item.quality_conclusion == "unknown" and not item.scores and item.quality_evaluation is None
        assert item.eval_error == "TRAJECTORY_RECOVERY_ATTEMPT_INVALID"


@pytest.mark.parametrize("when", ["after_reconcile", "before_reconcile"])
@pytest.mark.parametrize("idempotent", [False, True])
def test_checkpoint_deleted_after_reconciliation_cannot_fall_back_to_business_execution(setup_runtime, when, idempotent):
    message, _, original = crashed_response(setup_runtime, idempotent=idempotent)
    manager, _, _, _, worker, reconciler = setup_runtime
    if when == "after_reconcile":
        assert reconciler.reconcile_expired_leases() == 1
    with manager.get_session() as session:
        checkpoint = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=message[1]).one()
        session.delete(checkpoint)
    if when == "before_reconcile":
        assert reconciler.reconcile_expired_leases() == 1
    class Collector:
        async def collect(self, *args, **kwargs):
            return None
    worker.trajectory_collector = Collector()
    with patch("app.worker.get_langfuse_client_safe", return_value=None), patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        invoke.return_value = _ok_invocation({"intent": "repeated-business"})
        assert asyncio.run(worker.execute_item_message("deleted-checkpoint", message[1], original[0])) is True
        assert invoke.await_count == 0
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        assert item.eval_status == "failed" and item.quality_conclusion == "unknown" and not item.scores
        assert item.eval_error == "TRAJECTORY_RECOVERY_CHECKPOINT_MISSING"
        assert item.execution_status == "succeeded" and item.final_attempt_id == original[1]
        assert session.get(ExecutionAttemptRecord, original[1]).status == "SUCCEEDED"
        assert session.query(ExecutionAttemptRecord).filter_by(item_execution_id=item.id).count() == 1


@pytest.mark.parametrize("stage", ["inflight_reservation", "original_deadline"])
def test_formal_resume_keeps_the_original_deadline_and_unknown_wire_charge(tmp_path, stage):
    from app.execution_checkpoint import binding_provenance_from_manifest
    from app.limiter import MemoryAgentLimiter
    from app.queue import MemoryQueueAdapter
    from app.trajectory.checkpoint import begin_trajectory_round, load_trajectory_checkpoint
    from app.trajectory.worker_acquisition import WorkerTrajectoryCollector
    from app.worker import ExecutionWorker
    database = worker_database.__wrapped__(tmp_path)
    context = worker_context.__wrapped__(next(database))
    manager, scope, contract, now = context
    try:
        with manager.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            manifest = frozen_manifest()
            manifest["trajectory"] = launch.manifest["trajectory"]
            cp = session.get(ExecutionCheckpointRecord, "checkpoint")
            manifest["dataset"]["items"] = [{"id": scope.dataset_item_id,
                "input": cp.input_payload, "expected_output": cp.expected_output}]
            manifest["measurement_digest"] = manifest_measurement_digest(manifest)
            manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
            launch.manifest = manifest
            from app.evaluator_binding import canonical_digest
            cp.manifest_digest = canonical_digest(manifest)
            cp.binding_provenance = binding_provenance_from_manifest(manifest)
            attempt = session.get(ExecutionAttemptRecord, scope.attempt_id)
            attempt.status, attempt.lease_token = "RUNNING", "lease-1"
            if stage == "original_deadline":
                cp.created_at -= timedelta(seconds=3)
        with manager.get_session() as session:
            claim = begin_trajectory_round(session, scope, contract, lease_token="lease-1")
            original_deadline = claim.acquisition.deadline_at
        with manager.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, scope.item_execution_id)
            item.execution_status, item.lease_token, item.lease_expires_at = "queued", None, None
        def forbidden():
            pytest.fail("resume reset original deadline or refunded unknown wire reservation")
        worker = ExecutionWorker(manager, MemoryQueueAdapter(), MemoryAgentLimiter(),
            trajectory_collector=WorkerTrajectoryCollector(manager.get_session, factory_provider=forbidden))
        with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
            assert asyncio.run(worker.execute_item_message("budget-resume", scope.item_execution_id, scope.dispatch_generation))
            assert invoke.await_count == 0
        with manager.get_session() as session:
            saved = load_trajectory_checkpoint(session, scope, contract)
            assert saved.acquisition.deadline_at == original_deadline and saved.round_token is None
            expected_bytes = contract.limits.max_source_bytes if stage == "inflight_reservation" else 0
            assert saved.source_bytes_used == expected_bytes
            assert saved.acquisition.state == "UNAVAILABLE" and saved.acquisition.evidence_ref is None
    finally:
        database.close()
