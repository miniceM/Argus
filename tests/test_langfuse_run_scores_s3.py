from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app import langfuse_run_scores  # noqa: E402
from app.db_models import (  # noqa: E402
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseRunScoreTaskRecord,
    LangfuseSyncTaskRecord,
)
from app.langfuse_run_scores import LangfuseRunScoreSyncer  # noqa: E402
from app.result_snapshots import create_result_snapshot  # noqa: E402
from test_run_result_s3 import _create_completed_launch  # noqa: E402


class FakeLangfuse:
    def __init__(self, *, failure_at=None, wrong_ack_at=None):
        self.scores = []
        self.calls = []
        self.failure_at = failure_at
        self.wrong_ack_at = wrong_ack_at
        self.api = SimpleNamespace(scores=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        call_number = len(self.calls)
        if self.failure_at == call_number:
            raise RuntimeError("upstream score write failed")
        self.scores.append(kwargs)
        score_id = "wrong-id" if self.wrong_ack_at == call_number else kwargs["id"]
        return SimpleNamespace(id=score_id)


def test_run_scores_are_linked_to_dataset_run_with_stable_ids(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        launch = session.get(__import__("app.db_models", fromlist=["ExperimentLaunchRecord"]).ExperimentLaunchRecord, launch_id)
        launch.manifest["dataset"]["source"] = "langfuse"
        snapshot = create_result_snapshot(session, launch)
        item_id = session.query(__import__("app.db_models", fromlist=["ExperimentItemExecutionRecord"]).ExperimentItemExecutionRecord).filter_by(
            launch_id=launch_id, dataset_item_id="case-1"
        ).one().id
        session.add(LangfuseSyncTaskRecord(
            id="item-sync-1",
            launch_id=launch_id,
            item_id=item_id,
            dataset_item_id="case-1",
            dataset_version="2026-09-28T00:00:00+00:00",
            dispatch_generation=1,
            trace_id="trace-1",
            dataset_run_name="run",
            scores_payload={"_dataset_run_id": "dataset-run-1"},
            status="SYNCED",
        ))
        session.commit()
        snapshot_id = snapshot.id

    client = FakeLangfuse()
    syncer = LangfuseRunScoreSyncer(db_mgr, client)
    assert syncer.process_batch() == 1
    assert len(client.scores) >= 3
    assert all(score["dataset_run_id"] == "dataset-run-1" for score in client.scores)
    assert all(score["id"].startswith("argus-run-") for score in client.scores)
    assert all(call["request_options"] == {"timeout_in_seconds": 5, "max_retries": 0} for call in client.calls)
    with db_mgr.get_session() as session:
        task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        assert task.status == "SYNCED"


def test_seed_source_run_scores_are_skipped(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        launch = session.get(__import__("app.db_models", fromlist=["ExperimentLaunchRecord"]).ExperimentLaunchRecord, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        snapshot_id = snapshot.id

    assert LangfuseRunScoreSyncer(db_mgr, FakeLangfuse()).process_batch() == 1
    with db_mgr.get_session() as session:
        task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        assert task.status == "SKIPPED"


def test_run_scores_use_persisted_dataset_run_for_synchronous_launch(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.manifest["dataset"]["source"] = "langfuse"
        launch.langfuse_sync_status = "SYNCED"
        launch.langfuse_experiment_id = "dataset-run-sync"
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        snapshot_id = snapshot.id

    client = FakeLangfuse()
    assert LangfuseRunScoreSyncer(db_mgr, client).process_batch() == 1

    assert client.scores
    assert all(score["dataset_run_id"] == "dataset-run-sync" for score in client.scores)
    with db_mgr.get_session() as session:
        task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        assert task.status == "SYNCED"


def test_waiting_for_item_sync_does_not_consume_run_score_attempts(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.manifest["dataset"]["source"] = "langfuse"
        # A persisted ID from an older run must not supersede a current pending generation.
        launch.langfuse_sync_status = "PENDING"
        launch.langfuse_experiment_id = "stale-dataset-run"
        item = session.query(ExperimentItemExecutionRecord).filter_by(
            launch_id=launch_id, dataset_item_id="case-1"
        ).one()
        snapshot = create_result_snapshot(session, launch)
        item_sync = LangfuseSyncTaskRecord(
            id="item-sync-waiting",
            launch_id=launch_id,
            item_id=item.id,
            dataset_item_id="case-1",
            dataset_version="2026-09-28T00:00:00+00:00",
            dispatch_generation=item.dispatch_generation,
            trace_id="trace-waiting",
            dataset_run_name="run",
            scores_payload={},
            status="PENDING",
        )
        session.add(item_sync)
        session.commit()
        snapshot_id = snapshot.id

    client = FakeLangfuse()
    syncer = LangfuseRunScoreSyncer(db_mgr, client, max_attempts=2)
    for _ in range(5):
        assert syncer.process_batch() == 1
        with db_mgr.get_session() as session:
            run_task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
            assert run_task.status == "PENDING"
            assert run_task.attempts == 0
            run_task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

    assert client.scores == []
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        item_sync = session.get(LangfuseSyncTaskRecord, "item-sync-waiting")
        item_sync.scores_payload = {"_dataset_run_id": "dataset-run-ready"}
        item_sync.status = "SYNCED"
        launch.langfuse_sync_status = "SYNCED"
        run_task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        run_task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert syncer.process_batch() == 1
    assert client.scores
    assert all(score["dataset_run_id"] == "dataset-run-ready" for score in client.scores)
    with db_mgr.get_session() as session:
        run_task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        assert run_task.status == "SYNCED"


def _create_langfuse_score_task(db_mgr):
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.manifest["dataset"]["source"] = "langfuse"
        launch.langfuse_sync_status = "SYNCED"
        launch.langfuse_experiment_id = "dataset-run-ack"
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        return snapshot.id


def _score_task(db_mgr, snapshot_id):
    with db_mgr.get_session() as session:
        return session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()


def test_run_score_http_failure_is_not_marked_synced_with_real_sdk_transport(setup_runtime):
    from unittest.mock import patch

    import httpx
    from langfuse import Langfuse
    from langfuse._client.resource_manager import LangfuseResourceManager

    db_mgr, _, _, _, _, _ = setup_runtime
    snapshot_id = _create_langfuse_score_task(db_mgr)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"message": "temporarily unavailable"})

    # The SDK keeps one resource manager per public key. Scope this transport test
    # so its mocked credentials cannot leak through get_client() into later tests.
    with patch.dict(LangfuseResourceManager._instances):
        sdk_client = Langfuse(
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
            base_url="https://langfuse.example",
            httpx_client=httpx.Client(transport=httpx.MockTransport(respond)),
            tracing_enabled=False,
        )
        try:
            assert LangfuseRunScoreSyncer(db_mgr, sdk_client).process_batch() == 1
            task = _score_task(db_mgr, snapshot_id)
            assert requests
            assert task.status == "PENDING"
            assert task.last_error
        finally:
            sdk_client.shutdown()


def test_run_score_authentication_error_is_permanent(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    snapshot_id = _create_langfuse_score_task(db_mgr)

    class UnauthorizedError(RuntimeError):
        status_code = 401

    def reject_score(*args, **kwargs):
        raise UnauthorizedError("invalid credentials")

    monkeypatch.setattr(langfuse_run_scores, "publish_run_score", reject_score)
    assert LangfuseRunScoreSyncer(db_mgr, FakeLangfuse()).process_batch() == 1
    task = _score_task(db_mgr, snapshot_id)
    assert task.status == "FAILED"
    assert task.attempts == 1
    assert task.last_error == "LANGFUSE_HTTP_401"


def test_missing_langfuse_client_does_not_mark_langfuse_run_as_skipped_or_synced(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    snapshot_id = _create_langfuse_score_task(db_mgr)

    assert LangfuseRunScoreSyncer(db_mgr, None).process_batch() == 1
    task = _score_task(db_mgr, snapshot_id)
    assert task.status == "FAILED"
    assert "LANGFUSE_CLIENT_UNAVAILABLE" in (task.last_error or "")




def test_empty_run_score_payload_is_skipped_not_synced(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    snapshot_id = _create_langfuse_score_task(db_mgr)
    with db_mgr.get_session() as session:
        task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        task.scores_payload = {}
        session.commit()

    client = FakeLangfuse()
    assert LangfuseRunScoreSyncer(db_mgr, client).process_batch() == 1
    task = _score_task(db_mgr, snapshot_id)
    assert task.status == "SKIPPED"
    assert task.last_error == "NO_NUMERIC_RUN_SCORES"
    assert client.calls == []


def test_run_score_ack_mismatch_does_not_mark_synced(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    snapshot_id = _create_langfuse_score_task(db_mgr)
    client = FakeLangfuse(wrong_ack_at=1)
    assert LangfuseRunScoreSyncer(db_mgr, client).process_batch() == 1
    task = _score_task(db_mgr, snapshot_id)
    assert task.status == "PENDING"
    assert "SCORE_ACK_MISMATCH" in (task.last_error or "")


def test_run_score_partial_success_retries_with_same_score_ids(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    snapshot_id = _create_langfuse_score_task(db_mgr)
    client = FakeLangfuse(failure_at=2)
    syncer = LangfuseRunScoreSyncer(db_mgr, client)
    assert syncer.process_batch() == 1
    first_ids = [call["id"] for call in client.calls]
    assert len(first_ids) == 2
    assert _score_task(db_mgr, snapshot_id).status == "PENDING"

    client.failure_at = None
    with db_mgr.get_session() as session:
        task = session.query(LangfuseRunScoreTaskRecord).filter_by(snapshot_id=snapshot_id).one()
        task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert syncer.process_batch() == 1
    assert _score_task(db_mgr, snapshot_id).status == "SYNCED"
    assert [call["id"] for call in client.calls[:2]] == [call["id"] for call in client.calls[2:4]]


def test_run_scores_publish_without_derived_link(setup_runtime):
    """Run score publication depends on the run id, never on a derived browser link."""
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.manifest["dataset"]["source"] = "langfuse"
        launch.langfuse_sync_status = "SYNCED"
        launch.langfuse_experiment_id = "dataset-run-sync"
        launch.langfuse_experiment_url = None  # link backfill has not run yet
        create_result_snapshot(session, launch)
        session.commit()

    client = FakeLangfuse()
    assert LangfuseRunScoreSyncer(db_mgr, client).process_batch() == 1
    assert client.scores
    assert all(score["dataset_run_id"] == "dataset-run-sync" for score in client.scores)
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        assert launch.langfuse_experiment_url is None
        task = session.query(LangfuseRunScoreTaskRecord).filter_by(launch_id=launch_id).one()
        assert task.status == "SYNCED"
