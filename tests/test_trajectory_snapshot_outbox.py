"""S45-15：Snapshot 与轨迹 Item 投影任务在同一事务冻结。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from app.db import MigrationRunner
from app.db_models import (
    EvaluationResultRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseRunScoreTaskRecord,
    LangfuseSyncTaskRecord,
    RunResultSnapshotRecord,
    TrajectoryEvidenceArtifactRecord,
)
from app.langfuse_links import select_current_run_evidence
from app.langfuse_projection import trajectory_score_id
from app.langfuse_sync import LangfuseOutboxSyncer, aggregate_launch_sync_status, launch_sync_breakdown
from app.result_snapshots import create_result_snapshot
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from test_trajectory_evidence import postgres_engine as postgres_engine
from test_trajectory_required_evidence import ROOT
from test_trajectory_required_evidence import required_trajectory_snapshot as required_trajectory_snapshot

TYPE = "TRAJECTORY_ITEM_SCORES"


def tasks(db, snapshot_id=None):
    with db.get_session() as session:
        query = select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.task_type == TYPE)
        if snapshot_id is not None:
            query = query.where(LangfuseSyncTaskRecord.result_snapshot_id == snapshot_id)
        return list(session.scalars(query).all())


def new_revision(db, snapshot):
    with db.get_session() as session:
        result = session.scalar(select(EvaluationResultRecord).where(EvaluationResultRecord.launch_id == snapshot.launch_id))
        result.value = False
        session.flush()
        revised = create_result_snapshot(session, session.get(ExperimentLaunchRecord, snapshot.launch_id))
        assert revised.id != snapshot.id and revised.revision == snapshot.revision + 1
        return revised


def test_freeze_enqueues_owned_snapshot_and_typed_results_atomically(required_trajectory_snapshot):
    db, snapshot, acquired = required_trajectory_snapshot
    rows = tasks(db)
    assert len(rows) == 1
    task = rows[0]
    item = snapshot.items[0]
    assert task.result_snapshot_id == snapshot.id
    assert task.item_id == item["trajectory_input"]["acquisition"]["scope"]["item_execution_id"]
    assert task.trace_id == item["trace_id"]
    assert task.observation_id == item["observation_id"]
    assert task.scores_payload["typed_results"] == item["evaluation_results"]
    assert task.scores_payload["trajectory_input"] == item["trajectory_input"]
    assert task.scores_payload["snapshot_id"] == snapshot.id
    assert task.scores_payload["revision"] == snapshot.revision
    assert task.scores_payload["trajectory_input"]["acquisition"]["evidence_ref"]["artifact_id"] == acquired.evidence_ref.artifact_id


def test_identical_freeze_does_not_duplicate_snapshot_tasks(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        same = create_result_snapshot(session, session.get(ExperimentLaunchRecord, snapshot.launch_id))
    assert same.id == snapshot.id
    assert len(tasks(db)) == 1


def test_eval_only_new_revision_keeps_both_snapshot_tasks_at_same_generation(required_trajectory_snapshot):
    db, original, _ = required_trajectory_snapshot
    revised = new_revision(db, original)
    rows = tasks(db)
    assert len(rows) == 2
    assert {r.result_snapshot_id for r in rows} == {original.id, revised.id}
    assert len({r.dispatch_generation for r in rows}) == 1
    assert tasks(db, original.id)[0].scores_payload["typed_results"][0]["value"] is True
    assert tasks(db, revised.id)[0].scores_payload["typed_results"][0]["value"] is False


def clone(row, **overrides):
    payload = {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
    payload.update({"id": str(uuid4()), **overrides})
    return LangfuseSyncTaskRecord(**payload)


def test_snapshot_unique_identity_rejects_duplicates_even_at_different_generation(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    original = tasks(db, snapshot.id)[0]
    with pytest.raises(IntegrityError):
        with db.get_session() as session:
            session.add(clone(original, dispatch_generation=original.dispatch_generation + 1))
            session.flush()
    assert len(tasks(db)) == 1


def test_legacy_task_unique_identity_is_preserved(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    original = tasks(db, snapshot.id)[0]
    with db.get_session() as session:
        session.add(clone(original, task_type="FULL_EVAL_SYNC", result_snapshot_id=None))
    with pytest.raises(IntegrityError):
        with db.get_session() as session:
            session.add(clone(original, task_type="FULL_EVAL_SYNC", result_snapshot_id=None))
            session.flush()


def test_referenced_snapshot_cannot_be_deleted(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    assert len(tasks(db)) == 1
    # 去除另一种 Run Score 引用，以便此用例只验证新 Item Outbox 的 RESTRICT。
    with db.get_session() as session:
        session.execute(delete(LangfuseRunScoreTaskRecord).where(LangfuseRunScoreTaskRecord.snapshot_id == snapshot.id))
    with pytest.raises(IntegrityError):
        with db.get_session() as session:
            session.execute(delete(RunResultSnapshotRecord).where(RunResultSnapshotRecord.id == snapshot.id))
    with db.get_session() as session:
        assert session.get(RunResultSnapshotRecord, snapshot.id) is not None


def test_outbox_insert_failure_rolls_back_new_revision_and_result_change(required_trajectory_snapshot, monkeypatch):
    db, original, _ = required_trajectory_snapshot
    with db.get_session() as session:
        run_tasks = list(session.scalars(select(LangfuseRunScoreTaskRecord.id)).all())
    def reject(**kwargs):
        raise RuntimeError("synthetic trajectory outbox failure")
    monkeypatch.setattr("app.result_snapshots.LangfuseSyncTaskRecord", reject)
    with pytest.raises(RuntimeError, match="synthetic trajectory outbox failure"):
        new_revision(db, original)
    with db.get_session() as session:
        assert list(session.scalars(select(RunResultSnapshotRecord.id)).all()) == [original.id]
        assert list(session.scalars(select(LangfuseRunScoreTaskRecord.id)).all()) == run_tasks
        result = session.scalar(select(EvaluationResultRecord).where(EvaluationResultRecord.launch_id == original.launch_id))
        assert result.value is True
        item = session.scalar(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == original.launch_id))
        assert item.dispatch_generation == tasks(db)[0].dispatch_generation
    assert len(tasks(db)) == 1


def consumer(db):
    api = SimpleNamespace(scores=SimpleNamespace(create=MagicMock()),
                          dataset_run_items=SimpleNamespace(create=MagicMock()))
    return LangfuseOutboxSyncer(db, SimpleNamespace(api=api)), api


def test_claim_payload_explicitly_identifies_task_type_and_snapshot(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    syncer, _ = consumer(db)
    _, _, payload = syncer.claim_tasks()[0]
    assert payload["task_type"] == TYPE
    assert payload["result_snapshot_id"] == snapshot.id


def test_historical_task_projects_its_snapshot_without_latest_or_current_generation(required_trajectory_snapshot, monkeypatch):
    db, original, _ = required_trajectory_snapshot
    revised = new_revision(db, original)
    syncer, api = consumer(db)
    def forbidden_latest(*args):
        raise AssertionError("trajectory task must not query latest snapshot")
    monkeypatch.setattr(syncer, "_resolve_snapshot_provenance", forbidden_latest)
    with db.get_session() as session:
        item = session.scalar(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == original.launch_id))
        item.dispatch_generation += 1
    task_id, token, payload = syncer.claim_tasks()[0]
    assert payload["result_snapshot_id"] == original.id
    assert syncer.process_single_task(task_id, token, payload)
    api.dataset_run_items.create.assert_not_called()
    call = api.scores.create.call_args.kwargs
    binding_id = original.items[0]["evaluation_results"][0]["provenance"]["binding_id"]
    acquisition = original.items[0]["trajectory_input"]["acquisition"]
    assert call["id"] == trajectory_score_id(item_id=payload["item_id"], snapshot_id=original.id, binding_id=binding_id)
    assert call["value"] == 1.0
    assert call["metadata"]["original_value"] is True
    assert call["metadata"]["snapshot_id"] == original.id
    assert call["metadata"]["trajectory_digest"] == acquisition["evidence_ref"]["trajectory_digest"]
    assert call["metadata"]["trajectory_contract_digest"] == acquisition["evidence_ref"]["trajectory_contract_digest"]
    assert call["metadata"]["root_observation_id"] == acquisition["scope"]["root_observation_id"]
    assert call["observation_id"] == acquisition["scope"]["root_observation_id"]
    assert tasks(db, revised.id)[0].status == "PENDING"


def test_projection_outage_retries_same_score_and_preserves_frozen_quality(required_trajectory_snapshot):
    db, original, _ = required_trajectory_snapshot
    syncer, api = consumer(db)
    api.scores.create.side_effect = [ConnectionError("synthetic disconnection"), None]
    task_id, token, payload = syncer.claim_tasks()[0]
    assert not syncer.process_single_task(task_id, token, payload)
    with db.get_session() as session:
        task = session.get(LangfuseSyncTaskRecord, task_id)
        assert task.status == "PENDING" and task.attempts == 1
        task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        snapshot = session.get(RunResultSnapshotRecord, original.id)
        assert snapshot.source_result_digest == original.source_result_digest
        assert snapshot.items == original.items and snapshot.summary == original.summary
    second_id, second_token, second_payload = syncer.claim_tasks()[0]
    assert second_id == task_id and second_token != token
    assert syncer.process_single_task(second_id, second_token, second_payload)
    assert len(api.scores.create.call_args_list) == 2
    assert api.scores.create.call_args_list[0].kwargs["id"] == api.scores.create.call_args_list[1].kwargs["id"]
    api.dataset_run_items.create.assert_not_called()


def test_trajectory_full_eval_task_only_links_and_does_not_duplicate_scores(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    original = tasks(db, snapshot.id)[0]
    with db.get_session() as session:
        session.get(LangfuseSyncTaskRecord, original.id).status = "SYNCED"
        session.add(clone(original, task_type="FULL_EVAL_SYNC", result_snapshot_id=None,
                          scores_payload={"_dataset_source": "seed", "_typed_results": snapshot.items[0]["evaluation_results"]}))
    syncer, api = consumer(db)
    task_id, token, payload = syncer.claim_tasks()[0]
    assert syncer.process_single_task(task_id, token, payload)
    api.scores.create.assert_not_called()


def test_missing_owned_artifact_never_uploads_to_another_root(required_trajectory_snapshot):
    db, snapshot, acquired = required_trajectory_snapshot
    with db.get_session() as session:
        session.delete(session.get(TrajectoryEvidenceArtifactRecord, acquired.evidence_ref.artifact_id))
    syncer, api = consumer(db)
    task_id, token, payload = syncer.claim_tasks()[0]
    assert syncer.process_single_task(task_id, token, payload)
    assert tasks(db, snapshot.id)[0].status == "SKIPPED"
    assert "TRAJECTORY" in tasks(db, snapshot.id)[0].last_error
    api.dataset_run_items.create.assert_not_called()
    api.scores.create.assert_not_called()


@pytest.mark.parametrize("old_shape", ["index", "constraint"])
def test_postgres_upgrade_keeps_existing_rows_and_replaces_old_unique_shape(postgres_engine, tmp_path, old_shape):
    request = SimpleNamespace(param="postgres", getfixturevalue=lambda name: postgres_engine)
    setup = required_trajectory_snapshot.__wrapped__(request, tmp_path)
    db, snapshot, _ = next(setup)
    try:
        original = tasks(db, snapshot.id)[0]
        legacy_id = str(uuid4())
        with db.get_session() as session:
            session.add(clone(original, id=legacy_id, task_type="FULL_EVAL_SYNC", result_snapshot_id=None))
            session.execute(delete(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == original.id))
        # 专用测试 schema 模拟升级前的真实旧表，仅删新列及依赖索引。
        with postgres_engine.begin() as connection:
            connection.execute(text("ALTER TABLE langfuse_sync_tasks DROP COLUMN result_snapshot_id"))
            connection.execute(text("DROP INDEX IF EXISTS uq_sync_legacy_item_gen_type"))
            if old_shape == "index":
                connection.execute(text("CREATE UNIQUE INDEX uq_sync_item_gen_type ON langfuse_sync_tasks(item_id, dispatch_generation, task_type)"))
            else:
                connection.execute(text("ALTER TABLE langfuse_sync_tasks ADD CONSTRAINT uq_sync_item_gen_type UNIQUE(item_id, dispatch_generation, task_type)"))
            connection.execute(text("DELETE FROM schema_migrations WHERE version='019_trajectory_snapshot_outbox.sql'"))
        assert MigrationRunner(postgres_engine, ROOT / "migrations").apply_all() == ["019_trajectory_snapshot_outbox.sql"]
        with db.get_session() as session:
            restored = session.get(LangfuseSyncTaskRecord, legacy_id)
            assert restored.result_snapshot_id is None
            assert restored.item_id == original.item_id and restored.scores_payload == original.scores_payload
            session.add(clone(original))
        revised = new_revision(db, snapshot)
        assert {task.result_snapshot_id for task in tasks(db)} == {snapshot.id, revised.id}
        with pytest.raises(IntegrityError):
            with db.get_session() as session:
                session.add(clone(original, task_type="FULL_EVAL_SYNC", result_snapshot_id=None))
                session.flush()
    finally:
        setup.close()


@pytest.mark.parametrize("current_status,expected", [("PENDING", "SYNCING"), ("FAILED", "FAILED"), ("SYNCED", "SYNCED")])
def test_sync_aggregation_uses_link_and_current_snapshot_without_old_revision(required_trajectory_snapshot, current_status, expected):
    db, original, _ = required_trajectory_snapshot
    revised = new_revision(db, original)
    first = tasks(db, original.id)[0]
    with db.get_session() as session:
        session.get(LangfuseSyncTaskRecord, first.id).status = "FAILED"
        session.get(LangfuseSyncTaskRecord, first.id).attempts = 5
        current = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.result_snapshot_id == revised.id))
        current.status = current_status
        session.add(clone(first, task_type="FULL_EVAL_SYNC", result_snapshot_id=None, status="SYNCED",
                          attempts=1, scores_payload={"_dataset_source": "seed"}))
        for run_task in session.scalars(select(LangfuseRunScoreTaskRecord)).all():
            run_task.status = "SYNCED" if run_task.snapshot_id == revised.id else "FAILED"
            run_task.attempts = 1 if run_task.snapshot_id == revised.id else 5
    assert aggregate_launch_sync_status(db, original.launch_id) == expected
    breakdown = launch_sync_breakdown(db, original.launch_id)
    assert breakdown.item_trace.status == ("PENDING" if current_status == "PENDING" else current_status)
    assert breakdown.item_trace.task_count == 2
    assert breakdown.run_score.status == "SYNCED" and breakdown.run_score.task_count == 1


def test_run_link_evidence_only_consumes_full_eval_task_even_with_same_generation():
    item = SimpleNamespace(id="item", dispatch_generation=1)
    common = {"item_id": "item", "dispatch_generation": 1, "status": "SYNCED", "dataset_run_name": "run"}
    projection = SimpleNamespace(**common, id="projection", task_type=TYPE,
                                 scores_payload={"_dataset_run_id": "wrong"})
    link = SimpleNamespace(**common, id="link", task_type="FULL_EVAL_SYNC",
                           scores_payload={"_dataset_run_id": "correct"})
    for rows in ([projection, link], [link, projection]):
        evidence = select_current_run_evidence([item], rows)
        assert evidence.run_id == "correct"
        assert evidence.current_task_evidence[0].task_id == "link"


def test_claim_cannot_be_retargeted_to_another_valid_snapshot(required_trajectory_snapshot):
    db, original, _ = required_trajectory_snapshot
    revised = new_revision(db, original)
    syncer, api = consumer(db)
    task_id, token, payload = syncer.claim_tasks()[0]
    tampered = dict(payload, result_snapshot_id=revised.id, scores_payload=tasks(db, revised.id)[0].scores_payload)
    assert not syncer.process_single_task(task_id, token, tampered)
    api.scores.create.assert_not_called()
    api.dataset_run_items.create.assert_not_called()
    assert tasks(db, original.id)[0].result_snapshot_id == original.id
    assert tasks(db, original.id)[0].status == "PENDING"


def test_unknown_task_type_is_rejected_before_remote_calls(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    original = tasks(db, snapshot.id)[0]
    with db.get_session() as session:
        session.get(LangfuseSyncTaskRecord, original.id).status = "SYNCED"
        session.add(clone(original, task_type="UNKNOWN_TASK", result_snapshot_id=None))
    syncer, api = consumer(db)
    syncer.max_attempts = 1
    task_id, token, payload = syncer.claim_tasks()[0]
    assert not syncer.process_single_task(task_id, token, payload)
    with db.get_session() as session:
        task = session.get(LangfuseSyncTaskRecord, task_id)
        assert task.status == "FAILED" and task.last_error == "SYNC_TASK_TYPE_UNSUPPORTED"
    api.scores.create.assert_not_called()
    api.dataset_run_items.create.assert_not_called()


def test_expired_claim_is_recovered_by_another_worker_and_old_token_cannot_upload(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    first, first_api = consumer(db)
    task_id, old_token, old_payload = first.claim_tasks()[0]
    with db.get_session() as session:
        session.get(LangfuseSyncTaskRecord, task_id).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    second, second_api = consumer(db)
    recovered_id, token, payload = second.claim_tasks()[0]
    assert recovered_id == task_id and token != old_token
    assert not first.process_single_task(task_id, old_token, old_payload)
    first_api.scores.create.assert_not_called()
    assert second.process_single_task(recovered_id, token, payload)
    assert second_api.scores.create.call_count == 1
    assert tasks(db, snapshot.id)[0].status == "SYNCED"


def test_snapshot_task_type_cannot_be_changed_to_the_legacy_route(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    syncer, api = consumer(db)
    with db.get_session() as session:
        session.scalar(select(LangfuseSyncTaskRecord)).dataset_version = "2026-10-14T00:00:00Z"
    task_id, token, payload = syncer.claim_tasks()[0]
    altered = dict(payload, task_type="FULL_EVAL_SYNC")
    assert not syncer.process_single_task(task_id, token, altered)
    api.dataset_run_items.create.assert_not_called()
    api.scores.create.assert_not_called()
    assert tasks(db, snapshot.id)[0].status == "PENDING"


def test_historical_snapshot_keeps_legacy_sync_without_projection_backfill(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    original = tasks(db, snapshot.id)[0]
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        stored.summary = {k: v for k, v in stored.summary.items() if k != "item_score_projection_version"}
        session.execute(delete(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == original.id))
        session.add(clone(original, task_type="FULL_EVAL_SYNC", result_snapshot_id=None, status="SYNCED",
            scores_payload={"_dataset_source": "seed", "_typed_results": snapshot.items[0]["evaluation_results"]}))
        for row in session.scalars(select(LangfuseRunScoreTaskRecord)).all():
            row.status = "SYNCED"
    assert aggregate_launch_sync_status(db, snapshot.launch_id) == "SYNCED"
    assert launch_sync_breakdown(db, snapshot.launch_id).overall == "SYNCED"
    with db.get_session() as session:
        before = deepcopy(session.get(RunResultSnapshotRecord, snapshot.id).summary)
        same = create_result_snapshot(session, session.get(ExperimentLaunchRecord, snapshot.launch_id))
        assert same.id == snapshot.id and same.summary == before
    assert tasks(db) == []
    # 历史 FULL task 继续使用旧 generation Score 身份。
    with db.get_session() as session:
        session.scalar(select(LangfuseSyncTaskRecord)).status = "PENDING"
    syncer, api = consumer(db)
    task_id, token, payload = syncer.claim_tasks()[0]
    assert syncer.process_single_task(task_id, token, payload)
    assert api.scores.create.call_count == 1
    assert api.scores.create.call_args.kwargs["id"].startswith(f"score:{original.item_id}:gen")


def test_missing_current_projection_task_is_pending_in_both_status_views(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    original = tasks(db, snapshot.id)[0]
    with db.get_session() as session:
        session.execute(delete(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == original.id))
        session.add(clone(original, task_type="FULL_EVAL_SYNC", result_snapshot_id=None,
                          status="SYNCED", scores_payload={"_dataset_source": "seed"}))
        for row in session.scalars(select(LangfuseRunScoreTaskRecord)).all():
            row.status = "SYNCED"
    assert aggregate_launch_sync_status(db, snapshot.launch_id) == "SYNCING"
    assert launch_sync_breakdown(db, snapshot.launch_id).item_trace.status == "PENDING"
