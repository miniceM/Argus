"""冻结轨迹输入与 Snapshot 引用；不提前启用轨迹规则或资格。"""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.db_models import (
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    RunResultSnapshotRecord,
    TrajectoryEvidenceArtifactRecord,
)
from app.result_snapshots import create_result_snapshot
from app.trajectory.inputs import (
    freeze_trajectory_input,
    load_checkpoint_input,
    load_snapshot_input,
    make_evaluation_context,
)
from app.trajectory.worker_acquisition import WorkerTrajectoryCollector
from test_trajectory_evidence import anchor, artifact
from test_trajectory_evidence import postgres_engine as evidence_postgres_engine
from test_trajectory_worker import context as worker_context
from test_trajectory_worker import db as worker_database
from test_trajectory_worker import factory_for


@pytest.fixture
def postgres_engine():
    yield from evidence_postgres_engine.__wrapped__()


@pytest.fixture(params=["sqlite", "postgres"])
def context(request, tmp_path):
    database = None
    if request.param == "sqlite":
        database = worker_database.__wrapped__(tmp_path)
        db = next(database)
    else:
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
        trajectory, contract = artifact()
        anchor(engine, trajectory)
        db = SimpleNamespace(engine=engine, get_session=sessions), trajectory, contract
    prepared = worker_context.__wrapped__(db)
    manager, scope, _, _ = prepared
    from app.evaluator_binding import canonical_digest, manifest_measurement_digest
    from app.execution_checkpoint import binding_provenance_from_manifest
    from test_issue_84_eval_only_retry import _frozen_binding, _manifest
    with manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, scope.launch_id)
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        manifest = _manifest(_frozen_binding("intent_match"))
        manifest["schema_version"], manifest["trajectory"] = "1.3", launch.manifest["trajectory"]
        manifest["agent"]["trajectory_source_profile_id"] = "native-otel-v1"
        manifest["dataset"]["items"] = [{"id": scope.dataset_item_id, "input": cp.input_payload,
            "expected_output": cp.expected_output}]
        manifest["measurement_digest"] = manifest_measurement_digest(manifest)
        manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
        launch.manifest = manifest
        cp.manifest_digest, cp.binding_provenance = canonical_digest(manifest), binding_provenance_from_manifest(manifest)
    yield prepared
    if database is not None:
        database.close()


@pytest.fixture
def ready(context):
    manager, scope, _, _ = context
    with manager.get_session() as session:
        manifest = session.get(ExperimentLaunchRecord, scope.launch_id).manifest
    requests = []
    collector = WorkerTrajectoryCollector(manager.get_session, factory_provider=lambda: factory_for(context, requests))
    acquired = asyncio.run(collector.collect(manifest, item_id=scope.item_execution_id,
        generation=scope.dispatch_generation, lease_token="lease-1"))
    assert acquired.state == "READY"
    return acquired


def checkpoint(manager):
    with manager.get_session() as session:
        return session.get(ExecutionCheckpointRecord, "checkpoint")


def freeze(context):
    manager, scope, _, _ = context
    with manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, scope.launch_id)
        item = session.get(ExperimentItemExecutionRecord, scope.item_execution_id)
        launch.status = "COMPLETED"
        item.execution_status, item.eval_status, item.quality_conclusion = "succeeded", "succeeded", "pass"
        item.final_attempt_id, item.active_attempt_id = scope.attempt_id, None
        item.trace_id, item.observation_id = scope.trace_id, scope.root_observation_id
        return create_result_snapshot(session, launch)


def test_old_manifest_has_no_new_database_or_snapshot_payload_access():
    class Forbidden:
        def __getattr__(self, key):
            pytest.fail("legacy trajectory input performed database access")
    assert load_checkpoint_input(Forbidden(), Forbidden(), {"schema_version": "1.2"}) is None
    assert make_evaluation_context(Forbidden(), Forbidden(), {"schema_version": "1.2"}) is None
    assert freeze_trajectory_input(Forbidden(), item=Forbidden(), manifest={"schema_version": "1.2"}) is None


def test_ready_context_reads_original_frozen_artifact_and_owns_its_values(context, ready):
    manager, scope, _, _ = context
    with manager.get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        manifest = session.get(ExperimentLaunchRecord, scope.launch_id).manifest
        value = make_evaluation_context(session, cp, manifest)
        assert value.trajectory_evidence_ref == ready.evidence_ref and value.trajectory.scope == scope
        assert value.output == cp.agent_output and value.input_payload == cp.input_payload
        value.output["answer"] = "changed by consumer"
        value.trajectory.steps.clear()
    with manager.get_session() as session:
        value = make_evaluation_context(session, session.get(ExecutionCheckpointRecord, "checkpoint"),
            session.get(ExperimentLaunchRecord, scope.launch_id).manifest)
        assert value.output == {"answer": "synthetic"} and value.trajectory.steps


def test_snapshot_freezes_only_reference_and_recovers_it_after_checkpoint_cleanup(context, ready):
    manager, scope, _, _ = context
    snapshot = freeze(context)
    payload = snapshot.items[0]["trajectory_input"]
    assert payload["version"] == "1.0" and payload["acquisition"]["evidence_ref"]["artifact_id"] == ready.evidence_ref.artifact_id
    assert "steps" not in str(payload) and "payload_json" not in str(payload)
    assert "evidence_contract_version" not in snapshot.summary and "required_evidence_status" not in snapshot.items[0]
    original = deepcopy(snapshot.items)
    with manager.get_session() as session:
        session.delete(session.get(ExecutionCheckpointRecord, "checkpoint"))
    with manager.get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id)
        assert loaded.trajectory.scope == scope and loaded.trajectory_evidence_ref == ready.evidence_ref
        assert session.get(RunResultSnapshotRecord, snapshot.id).items == original


@pytest.mark.parametrize("failure", ["missing", "corrupt", "ref", "contract", "snapshot_digest"])
def test_snapshot_missing_or_tampered_evidence_is_finite_unavailable_without_rewriting_history(context, ready, failure):
    manager, scope, _, _ = context
    snapshot = freeze(context)
    original = deepcopy(snapshot.items)
    with manager.get_session() as session:
        artifact = session.get(TrajectoryEvidenceArtifactRecord, ready.evidence_ref.artifact_id)
        if failure == "missing":
            session.delete(artifact)
        elif failure == "corrupt":
            artifact.payload_json = "{}"
        else:
            row = session.get(RunResultSnapshotRecord, snapshot.id)
            payload = deepcopy(row.items)
            if failure == "ref":
                payload[0]["trajectory_input"]["acquisition"]["evidence_ref"]["artifact_id"] = "different-artifact"
            elif failure == "contract":
                payload[0]["trajectory_input"]["trajectory_contract_digest"] = "sha256:" + "0" * 64
            else:
                row.source_result_digest = "0" * 64
            row.items = payload
            original = deepcopy(payload)
    with manager.get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id)
        assert loaded.trajectory is None and loaded.reason_code.startswith("TRAJECTORY_")
        assert session.get(RunResultSnapshotRecord, snapshot.id).items == original


def test_pending_and_missing_scope_do_not_publish_private_candidate_as_evaluation_input(context):
    manager, scope, _, _ = context
    with manager.get_session() as session:
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        manifest = session.get(ExperimentLaunchRecord, scope.launch_id).manifest
        result = load_checkpoint_input(session, cp, manifest)
        assert result.trajectory is None and result.trajectory_evidence_ref is None
        cp.observation_id = None
        session.flush()
        result = load_checkpoint_input(session, cp, manifest)
        assert result.acquisition is None and result.reason_code == "TRAJECTORY_INPUT_SCOPE_UNAVAILABLE"


def test_expired_checkpoint_does_not_expire_its_snapshot_artifact(context, ready):
    manager, scope, _, _ = context
    with manager.get_session() as session:
        session.get(ExecutionCheckpointRecord, "checkpoint").expires_at = datetime.now(UTC) - timedelta(days=1)
    snapshot = freeze(context)
    with manager.get_session() as session:
        assert load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id).trajectory is not None


def test_evaluation_only_receives_original_frozen_reference_without_invoking_agent(context, ready):
    from app.evaluation_recovery import recover_evaluation
    from app.evaluator_binding import evaluate_frozen_item
    manager, scope, _, _ = context
    snapshot = freeze(context)
    original = deepcopy(snapshot.items)
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, scope.item_execution_id)
        item.evaluation_status, item.evaluation_generation = "evaluating", 1
        item.eval_status, item.quality_conclusion = "failed", "unknown"
    captured = []
    def evaluate(*args, **kwargs):
        captured.append(kwargs.get("context"))
        return evaluate_frozen_item(*args, **kwargs)
    with patch("app.evaluation_recovery.evaluate_frozen_item", side_effect=evaluate), patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        result = recover_evaluation(manager, item_id=scope.item_execution_id, evaluation_generation=1, worker_id="input-recovery")
        assert result["finalized"] and invoke.await_count == 0
    assert len(captured) == 1 and captured[0].trajectory_evidence_ref == ready.evidence_ref
    assert captured[0].trajectory.scope == scope
    with manager.get_session() as session:
        assert session.get(RunResultSnapshotRecord, snapshot.id).items == original
        assert session.query(ExecutionAttemptRecord).filter_by(item_execution_id=scope.item_execution_id).count() == 1


def test_normal_worker_supplies_owned_context_while_legacy_output_score_is_unchanged(setup_runtime):
    from app.evaluator_binding import evaluate_frozen_item
    from test_issue_84_eval_only_retry import _run_item
    from test_trajectory_execution_recovery import frozen_manifest
    captured = []
    def evaluate(*args, **kwargs):
        captured.append(kwargs.get("context"))
        return evaluate_frozen_item(*args, **kwargs)
    with patch("app.worker.evaluate_frozen_item", side_effect=evaluate):
        result = _run_item(setup_runtime, frozen_manifest(), "input-normal-worker", {"intent": "refund"})
    assert result[-2:] == (1, True)
    assert len(captured) == 1 and captured[0].output == {"intent": "refund"}
    assert captured[0].trajectory is None and captured[0].trajectory_evidence_ref is None


@pytest.mark.parametrize("failure", ["input", "expected", "manifest", "dataset_owner", "launch_owner", "attempt_owner"])
def test_evaluation_only_rejects_changed_checkpoint_input_before_scoring(context, ready, failure):
    from app.evaluation_recovery import recover_evaluation
    manager, scope, _, _ = context
    freeze(context)
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, scope.item_execution_id)
        item.evaluation_status, item.evaluation_generation = "evaluating", 1
        item.eval_status, item.quality_conclusion = "failed", "unknown"
        cp = session.get(ExecutionCheckpointRecord, "checkpoint")
        if failure == "input":
            cp.input_payload = {"changed": True}
        elif failure == "expected":
            cp.expected_output = {"changed": True}
        elif failure in {"dataset_owner", "launch_owner", "attempt_owner"}:
            from app.evaluator_binding import canonical_digest
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            if failure == "dataset_owner":
                from app.evaluator_binding import manifest_measurement_digest
                from app.execution_checkpoint import canonical_output_digest
                manifest = deepcopy(launch.manifest)
                manifest["dataset"]["items"].append({"id": "other-case", "input": {"q": "other-input"},
                    "expected_output": {"expected_intent": "refund"}})
                manifest["measurement_digest"] = manifest_measurement_digest(manifest)
                manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
                launch.manifest = manifest
                cp.dataset_item_id, cp.input_payload, cp.expected_output = "other-case", {"q": "other-input"}, {"expected_intent": "refund"}
                cp.agent_output = {"intent": "refund"}
                cp.output_digest = canonical_output_digest(cp.agent_output)
            elif failure == "launch_owner":
                session.add(ExperimentLaunchRecord(id="other-launch", agent_id=launch.agent_id,
                    agent_version=launch.agent_version, agent_version_id=launch.agent_version_id, dataset_name="other", dataset_version="v1",
                    name="other", manifest=deepcopy(launch.manifest)))
                session.flush()
                cp.launch_id = "other-launch"
            else:
                session.add(ExecutionAttemptRecord(id="other-attempt", item_execution_id=item.id,
                    dispatch_generation=item.dispatch_generation, attempt_no=2, status="SUCCEEDED"))
                session.flush()
                cp.final_attempt_id = "other-attempt"
            cp.manifest_digest = canonical_digest(launch.manifest)
            # 缺 root 不能绕过业务身份验证，向 Worker 发布别的 Item 的业务输入。
            cp.observation_id = None
            assert make_evaluation_context(session, cp, launch.manifest) is None
        else:
            cp.manifest_digest = "sha256:" + "0" * 64
    with patch("app.evaluation_recovery.evaluate_frozen_item") as evaluate, patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        result = recover_evaluation(manager, item_id=scope.item_execution_id, evaluation_generation=1, worker_id="input-invalid")
        assert result["finalized"] and result["reason"] in {"CHECKPOINT_CORRUPT", "CHECKPOINT_BINDING_MISMATCH"}
        assert evaluate.call_count == invoke.await_count == 0
    with manager.get_session() as session:
        assert session.get(ExperimentItemExecutionRecord, scope.item_execution_id).quality_conclusion == "unknown"


def test_snapshot_uses_its_frozen_manifest_and_original_generation_after_current_launch_changes(context, ready):
    manager, scope, _, _ = context
    snapshot = freeze(context)
    with manager.get_session() as session:
        session.delete(session.get(ExecutionCheckpointRecord, "checkpoint"))
        item = session.get(ExperimentItemExecutionRecord, scope.item_execution_id)
        item.dispatch_generation += 1
        session.get(ExperimentLaunchRecord, scope.launch_id).manifest = {"schema_version": "1.2", "changed": True}
    with manager.get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id)
        assert loaded.trajectory.scope == scope and loaded.trajectory_evidence_ref == ready.evidence_ref


def test_unavailable_input_at_snapshot_creation_cannot_be_upgraded_by_later_storage_repair(context, ready):
    manager, scope, _, _ = context
    with manager.get_session() as session:
        artifact = session.get(TrajectoryEvidenceArtifactRecord, ready.evidence_ref.artifact_id)
        original_payload = artifact.payload_json
        artifact.payload_json = "{}"
    snapshot = freeze(context)
    reason = snapshot.items[0]["trajectory_input"]["reason_code"]
    assert reason is not None
    with manager.get_session() as session:
        session.get(TrajectoryEvidenceArtifactRecord, ready.evidence_ref.artifact_id).payload_json = original_payload
    with manager.get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id)
        assert loaded.trajectory is None and loaded.reason_code == reason


@pytest.mark.parametrize("failure", ["malformed_dataset", "manifest_version", "wrong_version", "wrong_scope", "unknown_reason", "duplicate_case"])
def test_snapshot_strict_envelope_and_scope_fail_closed_even_with_recomputed_checksum(context, ready, failure):
    from app.result_snapshots import _canonical_digest
    manager, scope, _, _ = context
    snapshot = freeze(context)
    with manager.get_session() as session:
        row = session.get(RunResultSnapshotRecord, snapshot.id)
        if failure == "malformed_dataset":
            row.manifest = ["malformed"]
        elif failure == "manifest_version":
            row.manifest = row.manifest | {"schema_version": "2.0"}
        else:
            items = deepcopy(row.items)
            payload = items[0]["trajectory_input"]
            if failure == "wrong_version":
                payload["version"] = "2.0"
            elif failure == "wrong_scope":
                payload["acquisition"]["scope"]["dataset_item_id"] = "other-case"
            elif failure == "unknown_reason":
                payload["reason_code"] = "UNTRUSTED_REASON"
            else:
                items.append(deepcopy(items[0]))
            row.items = items
        row.manifest_digest, row.source_result_digest = _canonical_digest(row.manifest), _canonical_digest(row.items)
    with manager.get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id)
        assert loaded.trajectory is None and loaded.reason_code == "TRAJECTORY_INPUT_SNAPSHOT_INVALID"


@pytest.mark.parametrize("trajectory_binding", [False, True])
def test_shared_executor_forwards_context_only_for_explicit_trajectory_input(trajectory_binding):
    from dataclasses import replace

    from app.evaluator_binding import BuiltinPythonExecutor, evaluate_frozen_item, freeze_binding
    from app.evaluators import EvaluatorRegistry, default_evaluator_registry
    from app.trajectory.inputs import EvaluationContext
    from test_issue_84_eval_only_retry import IDENTITY, _manifest
    registry = EvaluatorRegistry(include_builtins=False)
    definition = registry.register_definition(default_evaluator_registry.definition("intent_match"))
    source = default_evaluator_registry.version("intent_match", "1.0.0")
    version = registry.register_version(replace(source, input_contract=(
        source.input_contract | {"input_kind": "trajectory"} if trajectory_binding else source.input_contract)))
    captured = []
    class Capture(BuiltinPythonExecutor):
        def execute(self, implementation, *, output, expected_output, **kwargs):
            captured.append(kwargs)
            return super().execute(implementation, output=output, expected_output=expected_output)
    manifest = _manifest(freeze_binding(definition, version, runner_identity=IDENTITY).to_payload())
    context = EvaluationContext({"intent": "refund"}, {"expected_intent": "refund"}, {}, None, None)
    result = evaluate_frozen_item(manifest, output=context.output, expected_output=context.expected_output,
        registry=registry, executors={"builtin_python": Capture(registry)}, context=context)
    assert result.scores == {"intent_match": 1.0} and result.quality_conclusion == "pass"
    assert captured == ([{"context": context}] if trajectory_binding else [{}])
