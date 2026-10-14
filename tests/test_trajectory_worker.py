import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from app.db_models import (
    AgentVersionRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from app.evaluator_binding import canonical_digest
from app.trajectory.codec import canonical_dumps
from app.trajectory.factory import QualifiedReaderFactory, ReaderApproval, reader_artifact_digest
from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract
from app.trajectory.worker_acquisition import WorkerTrajectoryCollector, WorkerTrajectoryError, parse_reader_approvals
from test_trajectory_acquisition import Clock, run_async
from test_trajectory_assembler import configured, prepared
from test_trajectory_checkpoint import view
from test_trajectory_coordinator import context as coordinator_context
from test_trajectory_coordinator import db as coordinator_db

DOCUMENT = b"synthetic API document"
API_DIGEST = "sha256:" + hashlib.sha256(DOCUMENT).hexdigest()


@pytest.fixture
def db(tmp_path):
    yield from coordinator_db.__wrapped__(tmp_path)


@pytest.fixture
def context(db):
    manager, scope, contract, now = coordinator_context.__wrapped__(db)
    contract = configured(contract, limits={"deadline_seconds": 2, "quiet_window_seconds": Decimal("0.01")})
    with manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, scope.launch_id)
        version = session.get(AgentVersionRecord, launch.agent_version_id)
        manifest = dict(launch.manifest)
        manifest["schema_version"] = "1.3"
        manifest["agent"] = dict(manifest.get("agent", {}), trajectory_source_profile_id="native-otel-v1")
        manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract), version)
        launch.manifest = manifest
        session.get(ExecutionCheckpointRecord, "checkpoint").manifest_digest = canonical_digest(manifest)
    return manager, scope, contract, now


def factory_for(context, requests, *, approved=True, changed_document=False):
    rows, _, _ = prepared()
    def handler(request):
        requests.append(request)
        # API 文档 probe 也必须发生在数据库已提交保守预留之后。
        assert view(context).round_token is not None
        if request.url.path == "/api/openapi.yaml":
            return httpx.Response(200, content=b"changed" if changed_document else DOCUMENT)
        return httpx.Response(200, content=canonical_dumps({"data": rows, "meta": {"cursor": None}}).encode())
    return QualifiedReaderFactory(server_url="http://127.0.0.1:3000", public_key="synthetic-public",
        secret_key="synthetic-secret", transport_factory=lambda: httpx.MockTransport(handler),
        approvals=(ReaderApproval(context[2].digest, reader_artifact_digest(), API_DIGEST),) if approved else ())


@run_async
async def test_legacy_manifest_does_not_read_new_configuration_or_database():
    def forbidden():
        pytest.fail("legacy path accessed trajectory configuration or DB")
    collector = WorkerTrajectoryCollector(forbidden, factory_provider=forbidden)
    assert await collector.collect({"schema_version": "1.2"}, item_id="item", generation=1,
                                   lease_token="lease") is None


@run_async
@pytest.mark.parametrize("preparation_delay", [0, 2.1])
async def test_worker_collector_qualifies_after_reservation_and_preserves_original_deadline(context, preparation_delay):
    await asyncio.sleep(preparation_delay)
    requests = []
    collector = WorkerTrajectoryCollector(context[0].get_session, factory_provider=lambda: factory_for(context, requests))
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
    # 该测试验证假 Reader 的准入/提交顺序；fixture 准备和 CI 调度不应推进采集时钟。
    # 原 checkpoint、2秒 deadline 和正式超时测试均保持不变。
    from app.trajectory.coordinator import TrajectoryCoordinator
    clock = Clock()
    clock.base = context[3]

    async def sleep(seconds):
        clock.advance(seconds)
        await asyncio.sleep(0)

    with patch("app.trajectory.worker_acquisition.TrajectoryCoordinator",
        side_effect=lambda sessions: TrajectoryCoordinator(sessions, clock=clock, sleep=sleep)):
        result = await collector.collect(manifest, item_id=context[1].item_execution_id,
            generation=context[1].dispatch_generation, lease_token="lease-1")
    assert result.state == "READY" and result.completeness.status == "OBSERVED_COMPLETE"
    assert result.deadline_at == context[3] + timedelta(seconds=2)
    assert view(context).acquisition == result and requests[0].url.path == "/api/openapi.yaml"
    assert sum(request.url.path == "/api/openapi.yaml" for request in requests) == 1


@run_async
@pytest.mark.parametrize("approved,changed,reason", [(False, False, "UNQUALIFIED"),
                                                  (True, True, "API_CONTRACT_MISMATCH")])
async def test_unqualified_or_drifted_source_is_persisted_unavailable(context, approved, changed, reason):
    requests = []
    collector = WorkerTrajectoryCollector(context[0].get_session,
        factory_provider=lambda: factory_for(context, requests, approved=approved, changed_document=changed))
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
    result = await collector.collect(manifest, item_id=context[1].item_execution_id,
        generation=context[1].dispatch_generation, lease_token="lease-1")
    assert result.state == "UNAVAILABLE" and result.reason_code == "TRAJECTORY_SOURCE_" + reason
    assert result.evidence_ref is None and view(context).acquisition == result
    assert len(requests) == int(approved)


@pytest.mark.parametrize("encoded", ["not-json", "{}", "[{}]", "x" * 16385,
    '[{"trajectory_contract_digest":"secret","reader_artifact_digest":"secret","api_document_digest":"secret"}]'])
def test_runner_approval_configuration_is_bounded_and_finite(encoded):
    with pytest.raises(ValueError, match="TRAJECTORY_READER_APPROVAL_CONFIGURATION_INVALID") as caught:
        parse_reader_approvals(encoded)
    assert "secret" not in str(caught.value)


def test_runner_approval_configuration_defaults_deny_and_preserves_exact_tuple():
    assert parse_reader_approvals(None) == parse_reader_approvals("") == ()
    record = dict(trajectory_contract_digest="sha256:" + "0" * 64,
                  reader_artifact_digest="sha256:" + "1" * 64, api_document_digest=API_DIGEST)
    assert parse_reader_approvals(json.dumps([record])) == (ReaderApproval(**record),)


@run_async
@pytest.mark.parametrize("missing", ["scope", "checkpoint"])
async def test_missing_trusted_scope_is_a_finite_diagnostic_without_source_io(context, missing):
    def forbidden():
        pytest.fail("missing scope caused source IO")
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
        checkpoint = session.get(ExecutionCheckpointRecord, "checkpoint")
        if missing == "scope":
            checkpoint.observation_id = None
        else:
            session.delete(checkpoint)
    collector = WorkerTrajectoryCollector(context[0].get_session, factory_provider=forbidden)
    with pytest.raises(WorkerTrajectoryError, match="TRAJECTORY_WORKER_SCOPE_UNAVAILABLE"):
        await collector.collect(manifest, item_id=context[1].item_execution_id,
            generation=context[1].dispatch_generation, lease_token="lease-1")


@pytest.mark.parametrize("schema,error", [("1.2", None), ("1.3", None), ("1.3", "SCOPE_UNAVAILABLE")])
def test_formal_worker_collects_after_saved_response_and_releases_agent_permit(setup_runtime, schema, error):
    from types import SimpleNamespace

    from app.db_models import ExecutionAttemptRecord, ExperimentItemExecutionRecord
    from app.evaluator_binding import manifest_measurement_digest, resolve_execution_plan
    from test_issue_84_eval_only_retry import _frozen_binding, _manifest, _run_item

    manager, _, limiter, _, worker, _ = setup_runtime
    manifest = _manifest(_frozen_binding("intent_match"))
    if schema == "1.3":
        _, contract, _ = prepared()
        manifest["schema_version"] = schema
        manifest["agent"]["trajectory_source_profile_id"] = "native-otel-v1"
        manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract),
            SimpleNamespace(trajectory_source_profile_id="native-otel-v1"))
        manifest["measurement_digest"] = manifest_measurement_digest(manifest)
        manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
        assert len(resolve_execution_plan(manifest)) == 1
    calls = []
    class Collector:
        async def collect(self, frozen, *, item_id, generation, lease_token, cancelled):
            calls.append(item_id)
            with manager.get_session() as session:
                cp = session.scalar(__import__("sqlalchemy").select(ExecutionCheckpointRecord).where(
                    ExecutionCheckpointRecord.item_execution_id == item_id))
                item = session.get(ExperimentItemExecutionRecord, item_id)
                assert cp.agent_output == {"intent": "refund"} and cp.dispatch_generation == generation
                assert item.active_attempt_id == cp.final_attempt_id and item.lease_token == lease_token
                assert not cancelled.is_set()
            assert not limiter._permits.get("test-agent-v1")
            if error:
                raise WorkerTrajectoryError(error)
    worker.trajectory_collector = Collector()
    result = _run_item(setup_runtime, manifest, "trajectory-worker", {"intent": "refund"})
    assert result[-2:] == (1, True)
    assert len(calls) == (1 if schema == "1.3" else 0)
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, result[5])
        assert item.execution_status == "succeeded" and item.quality_conclusion == "pass"
        assert session.query(ExecutionAttemptRecord).filter_by(item_execution_id=item.id).count() == 1


@run_async
async def test_expired_original_checkpoint_deadline_has_zero_configuration_and_source_io(context):
    def forbidden():
        pytest.fail("expired deadline opened a source")
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
        session.get(ExecutionCheckpointRecord, "checkpoint").created_at = context[3] - timedelta(seconds=3)
    collector = WorkerTrajectoryCollector(context[0].get_session, factory_provider=forbidden)
    result = await collector.collect(manifest, item_id=context[1].item_execution_id,
        generation=context[1].dispatch_generation, lease_token="lease-1")
    assert result.state == "UNAVAILABLE" and result.deadline_at == context[3] - timedelta(seconds=1)


@run_async
@pytest.mark.parametrize("changed", ["lease", "generation", "cancel"])
async def test_stale_or_cancelled_worker_is_abandoned_before_source_configuration(context, changed):
    def forbidden():
        pytest.fail("abandoned execution opened a source")
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
        item = session.get(ExperimentItemExecutionRecord, context[1].item_execution_id)
        if changed == "lease":
            item.lease_token = "new-owner"
        elif changed == "generation":
            item.dispatch_generation += 1
        else:
            session.get(ExperimentLaunchRecord, context[1].launch_id).status = "CANCELLED"
    collector = WorkerTrajectoryCollector(context[0].get_session, factory_provider=forbidden)
    with pytest.raises(WorkerTrajectoryError) as caught:
        await collector.collect(manifest, item_id=context[1].item_execution_id,
            generation=context[1].dispatch_generation, lease_token="lease-1")
    assert caught.value.abandoned
    with context[0].get_session() as session:
        assert session.get(ExecutionCheckpointRecord, "checkpoint").trajectory_runtime_json is None


@run_async
@pytest.mark.parametrize("mode", ["task", "lease-event"])
async def test_task_cancel_during_api_qualification_closes_client_and_aborts_reserved_round(context, mode):
    entered, closed = asyncio.Event(), asyncio.Event()
    class Transport(httpx.MockTransport):
        async def aclose(self):
            closed.set()
    async def handler(request):
        assert request.url.path == "/api/openapi.yaml" and view(context).round_token is not None
        entered.set()
        await asyncio.Event().wait()
    factory = QualifiedReaderFactory(server_url="http://127.0.0.1:3000", public_key="synthetic-public",
        secret_key="synthetic-secret", transport_factory=lambda: Transport(handler),
        approvals=(ReaderApproval(context[2].digest, reader_artifact_digest(), API_DIGEST),))
    collector = WorkerTrajectoryCollector(context[0].get_session, factory_provider=lambda: factory)
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
    stopped = asyncio.Event()
    task = asyncio.create_task(collector.collect(manifest, item_id=context[1].item_execution_id,
        generation=context[1].dispatch_generation, lease_token="lease-1", cancelled=stopped))
    await asyncio.wait_for(entered.wait(), 1)
    if mode == "task":
        task.cancel()
    else:
        stopped.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and view(context).round_token is None
    assert view(context).acquisition.state == "PENDING" and view(context).candidate_ref is None


@run_async
async def test_invalid_runner_configuration_is_a_persisted_finite_diagnostic(context):
    def bad_configuration():
        raise ValueError("SYNTHETIC_SECRET_BAD_CONFIGURATION")
    collector = WorkerTrajectoryCollector(context[0].get_session, factory_provider=bad_configuration)
    with context[0].get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, context[1].launch_id).manifest
    result = await collector.collect(manifest, item_id=context[1].item_execution_id,
        generation=context[1].dispatch_generation, lease_token="lease-1")
    assert result.state == "UNAVAILABLE" and result.reason_code == "TRAJECTORY_SOURCE_CONFIGURATION_INVALID"
    assert "SYNTHETIC_SECRET" not in canonical_dumps(result)
    assert view(context).acquisition == result


@pytest.mark.parametrize("kind", ["fence", "lease-event"])
def test_formal_worker_does_not_evaluate_after_acquisition_loses_execution_ownership(setup_runtime, kind):
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.evaluator_binding import manifest_measurement_digest
    from test_issue_84_eval_only_retry import _frozen_binding, _manifest, _run_item
    manifest = _manifest(_frozen_binding("intent_match"))
    _, contract, _ = prepared()
    manifest["schema_version"] = "1.3"
    manifest["agent"]["trajectory_source_profile_id"] = "native-otel-v1"
    manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract),
        SimpleNamespace(trajectory_source_profile_id="native-otel-v1"))
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
    class Collector:
        async def collect(self, *args, cancelled, **kwargs):
            if kind == "fence":
                raise WorkerTrajectoryError("LEASE_LOST", abandoned=True)
            cancelled.set()
            raise asyncio.CancelledError
    setup_runtime[4].trajectory_collector = Collector()
    with patch("app.worker.evaluate_frozen_item", side_effect=AssertionError("abandoned work evaluated")):
        result = _run_item(setup_runtime, manifest, "trajectory-fence", {"intent": "refund"})
    assert result[-2:] == (1, False)
    with setup_runtime[0].get_session() as session:
        cp = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=result[5]).one()
        assert cp.agent_output == {"intent": "refund"}


@run_async
async def test_corrupt_manifest_is_a_finite_collector_diagnostic_without_scope_or_source_io():
    def forbidden():
        pytest.fail("invalid frozen contract accessed DB or source")
    collector = WorkerTrajectoryCollector(forbidden, factory_provider=forbidden)
    with pytest.raises(WorkerTrajectoryError, match="TRAJECTORY_WORKER_CONTRACT_INVALID"):
        await collector.collect({"schema_version": "1.3", "trajectory": {}}, item_id="item",
                                generation=1, lease_token="lease")


def test_contract_corruption_after_response_does_not_lose_business_success_or_create_false_scores(setup_runtime):
    from types import SimpleNamespace

    from app.evaluator_binding import manifest_measurement_digest
    from test_issue_84_eval_only_retry import _frozen_binding, _manifest, _run_item
    manager, _, _, _, worker, _ = setup_runtime
    manifest = _manifest(_frozen_binding("intent_match"))
    _, contract, _ = prepared()
    manifest["schema_version"] = "1.3"
    manifest["agent"]["trajectory_source_profile_id"] = "native-otel-v1"
    manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract),
        SimpleNamespace(trajectory_source_profile_id="native-otel-v1"))
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
    save = worker._persist_execution_checkpoint
    def corrupt_after_saved_response(**arguments):
        save(**arguments)
        arguments["manifest"]["trajectory"]["trajectory_contract_digest"] = "sha256:" + "0" * 64
    worker._persist_execution_checkpoint = corrupt_after_saved_response
    result = _run_item(setup_runtime, manifest, "trajectory-corrupt-after-response", {"intent": "refund"})
    assert result[-2:] == (1, True)
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, result[5])
        checkpoint = session.query(ExecutionCheckpointRecord).filter_by(item_execution_id=item.id).one()
        assert checkpoint.agent_output == {"intent": "refund"} and checkpoint.trajectory_acquisition_json is None
        assert item.execution_status == "succeeded" and item.eval_status == "failed"
        assert item.quality_conclusion == "unknown" and not item.scores
