"""#217 独立验收：绝对预算、崩溃换代、真实 DB 竞争与取消边界。"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from app.db import DatabaseManager, MigrationRunner
from app.db_models import (
    AgentRecord,
    AgentVersionRecord,
    EvaluationAttemptRecord,
    EvaluationResultRecord,
    ExecutionAttemptRecord,
)
from app.evaluation_recovery import claim_evaluation
from app.langfuse_scores_client import ScoresReadError
from app.managed_score_recovery import _fail_managed, get_retry_config, recover_managed_scores
from sqlalchemy import create_engine, event, select, text
from test_issue_216_managed_scores import Remote
from test_issue_217_managed_async import (
    ROOT,
    _good_score,
    _item,
    _managed_manifest,
    _observation_for,
    _run_managed_http,
)


@pytest.fixture(params=["sqlite", "postgresql"])
def runtime(request, setup_runtime):
    if request.param == "sqlite":
        yield setup_runtime
        return
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL absent; requires isolated PostgreSQL")
    schema = "issue217_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = DatabaseManager(url)
    db.engine.dispose()
    db.engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    db.session_factory.configure(bind=db.engine)
    try:
        MigrationRunner(db.engine, ROOT / "migrations").apply_all()
        with setup_runtime[0].get_session() as source, db.get_session() as target:
            for cls in (AgentRecord, AgentVersionRecord):
                for row in source.scalars(select(cls)):
                    target.add(cls(**{c.name: getattr(row, c.name) for c in cls.__table__.columns}))
                target.flush()
        _, old_queue, old_limiter, old_orch, old_worker, old_rec = setup_runtime
        queue, limiter = type(old_queue)(), type(old_limiter)()
        yield (db, queue, limiter, type(old_orch)(db, queue, limiter),
               type(old_worker)(db, queue, limiter, worker_id="boundary-worker"),
               type(old_rec)(db, queue, limiter))
    finally:
        db.engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def start(runtime):
    db, _, _, _, _, launch_id, item_id, calls, ok = _run_managed_http(
        runtime, _managed_manifest(), {"answer": "ok"}
    )
    assert ok and calls == 1
    obs, trace_id, obs_id = _observation_for(db, item_id)
    return db, launch_id, item_id, obs, _good_score(trace_id, obs_id)


def rows(db, cls, item_id):
    with db.get_session() as session:
        return list(session.scalars(select(cls).where(cls.item_execution_id == item_id)))


def test_expired_deadline_never_reads_remote(runtime, monkeypatch):
    from app.db_models import ExperimentItemExecutionRecord as Item

    db, _, item_id, obs, good = start(runtime)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_DEADLINE_SECONDS", "1")
    with db.get_session() as session:
        session.get(Item, item_id).evaluation_started_at = datetime.now(UTC) - timedelta(seconds=2)
    factory = Mock(return_value=Remote(obs, [good]))
    out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="late", client_factory=factory)
    assert out["reason"] == "MANAGED_SCORES_DEADLINE_EXCEEDED"
    factory.assert_not_called()
    assert _item(db, item_id).quality_conclusion == "unknown"
    assert rows(db, EvaluationResultRecord, item_id) == []


def test_deadline_during_fetch_rejects_late_score(runtime, monkeypatch):
    from app.db_models import ExperimentItemExecutionRecord as Item

    db, _, item_id, obs, good = start(runtime)
    now = datetime.now(UTC)
    clock = [now]

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(metaclass=ClockMeta):
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz=UTC):
            return clock[0]

    class Delayed(Remote):
        def scores_for(self, *args):
            clock[0] = now + timedelta(seconds=2)
            return super().scores_for(*args)

    with db.get_session() as session:
        session.get(Item, item_id).evaluation_started_at = now
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_DEADLINE_SECONDS", "1")
    monkeypatch.setattr("app.managed_score_recovery._now", Clock.now)
    monkeypatch.setattr("app.langfuse_managed_scores.datetime", Clock)
    out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="slow",
                                 client_factory=lambda: Delayed(obs, [good]))
    assert out["reason"] == "MANAGED_SCORES_DEADLINE_EXCEEDED"
    assert _item(db, item_id).eval_status == "failed"
    assert rows(db, EvaluationResultRecord, item_id) == []


def test_retry_after_is_respected_and_duplicate_cannot_skip_backoff(runtime):
    db, _, item_id, obs, _ = start(runtime)

    class Limited(Remote):
        def scores_for(self, *args):
            raise ScoresReadError("RATE_LIMITED", retry_after="120")

    before = datetime.now(UTC)
    out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="limited",
                                 client_factory=lambda: Limited(obs, []))
    assert out["reason"] == "RETRY_SCHEDULED"
    waiting = _item(db, item_id)
    assert waiting.available_at.replace(tzinfo=UTC) >= before + timedelta(seconds=120)
    factory = Mock(return_value=Remote(obs, []))
    duplicate = recover_managed_scores(db, item_id=item_id, evaluation_generation=waiting.evaluation_generation,
                                       worker_id="early-duplicate", client_factory=factory)
    assert not duplicate["claimed"]
    factory.assert_not_called()


def test_cancel_during_backoff_settles_without_agent_rerun(runtime):
    db, launch_id, item_id, obs, _ = start(runtime)
    recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="waiting",
                           client_factory=lambda: Remote(obs, []))
    runtime[3].cancel_launch(launch_id)
    runtime[5].run_reconcile_cycle()
    final = _item(db, item_id)
    assert final.execution_status == "succeeded" and len(rows(db, ExecutionAttemptRecord, item_id)) == 1
    assert final.eval_status == "failed" and final.evaluation_status == "failed"
    assert "EVALUATION_CANCELLED" in final.evaluation_error
    assert final.quality_conclusion == "unknown" and final.evaluation_lease_token is None


def test_stale_worker_does_not_corrupt_winning_attempt_audit(runtime):
    from app.db_models import ExperimentItemExecutionRecord as Item

    db, launch_id, item_id, obs, good = start(runtime)
    old = claim_evaluation(db, item_id=item_id, evaluation_generation=1, worker_id="old")
    with db.get_session() as session:
        session.get(Item, item_id).evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    won = recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="new",
                                 client_factory=lambda: Remote(obs, [good]))
    # Managed 必须先经 Reconciler 换代再接管过期租约；不会复用上一轮预算。
    if not won["claimed"]:
        runtime[5].reconcile_expired_evaluation_leases()
        with db.get_session() as session:
            session.get(Item, item_id).available_at = datetime.now(UTC) - timedelta(seconds=1)
        current = _item(db, item_id)
        won = recover_managed_scores(db, item_id=item_id, evaluation_generation=current.evaluation_generation,
                                     worker_id="new", client_factory=lambda: Remote(obs, [good]))
    assert won["finalized"]
    _fail_managed(db, old, error_code="TIMEOUT", error_message="late", launch_id=launch_id)
    attempts = rows(db, EvaluationAttemptRecord, item_id)
    winning = max(attempts, key=lambda a: a.evaluation_generation)
    assert winning.status == "succeeded"


def test_reconciler_scan_cannot_clear_newly_renewed_lease(runtime):
    if runtime[0].engine.dialect.name != "postgresql":
        pytest.skip("MVCC interleaving requires PostgreSQL")
    from app.db_models import ExperimentItemExecutionRecord as Item

    db, _, item_id, _, _ = start(runtime)
    claim = claim_evaluation(db, item_id=item_id, evaluation_generation=1, worker_id="live")
    assert claim
    with db.get_session() as session:
        session.get(Item, item_id).evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    renewed = []

    def after_scan(conn, cursor, statement, parameters, context, executemany):
        if "evaluation_lease_expires_at IS NOT NULL" in statement and not renewed:
            renewed.append(True)
            # 精确注入 SELECT 已读旧快照、写回前 Worker 恢复/续租的窗口。
            renewed.append(claim_evaluation(db, item_id=item_id, evaluation_generation=1,
                                            worker_id="new-owner", lease_seconds=120))
            assert renewed[-1]

    event.listen(db.engine, "after_cursor_execute", after_scan)
    try:
        assert runtime[5].reconcile_expired_evaluation_leases() == 0
    finally:
        event.remove(db.engine, "after_cursor_execute", after_scan)
    assert renewed and _item(db, item_id).evaluation_lease_token == renewed[-1]["lease_token"]


def test_crashed_collections_consume_budget(runtime, monkeypatch):
    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.managed_score_recovery import _record_managed_attempt_started

    db, launch_id, item_id, _, _ = start(runtime)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "2")
    for n in range(2):
        item = _item(db, item_id)
        claim = claim_evaluation(db, item_id=item_id, evaluation_generation=item.evaluation_generation, worker_id=f"crash-{n}")
        assert claim
        _record_managed_attempt_started(db, item_id=item_id, launch_id=launch_id,
                                       evaluation_generation=item.evaluation_generation, target_bindings=[],
                                       reused_output_digest="same", worker_id=f"crash-{n}")
        with db.get_session() as session:
            session.get(Item, item_id).evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        runtime[5].reconcile_expired_evaluation_leases()
        with db.get_session() as session:
            session.get(Item, item_id).available_at = datetime.now(UTC) - timedelta(seconds=1)
    final = _item(db, item_id)
    assert final.evaluation_status == "failed" and final.quality_conclusion == "unknown"
    assert "MANAGED_SCORES_EXHAUSTED" in final.evaluation_error
    assert len(rows(db, EvaluationAttemptRecord, item_id)) == 2
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


@pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "1e309"])
def test_infinite_attempt_config_is_safe(monkeypatch, raw):
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", raw)
    assert get_retry_config()["max_attempts"] == 10


def test_multiworker_single_adoption_on_both_databases(runtime, monkeypatch):
    from test_issue_217_managed_async import test_concurrent_workers_single_adoption

    test_concurrent_workers_single_adoption(runtime, monkeypatch)


def test_live_dispatcher_managed_wait_does_not_block_http(monkeypatch):
    """真实 lifespan 调度层（而非仅 Worker 内层 Semaphore）的并发隔离。"""
    import asyncio
    from types import SimpleNamespace

    from app import main

    async def scenario():
        started, release, http_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        messages = iter([
            [("managed-1", "item-1", 1, "MANAGED_SCORE")],
            [("managed-2", "item-2", 1, "MANAGED_SCORE")],
            [("http-1", "item-3", 1, "INVOCATION")],
        ])
        managed_calls = []

        async def managed(*args):
            managed_calls.append(args)
            started.set()
            await release.wait()

        async def invocation(*args):
            http_started.set()

        fake_worker = SimpleNamespace(poll_queue=lambda **kw: next(messages, []),
                                      execute_managed_message=managed, execute_item_message=invocation)
        fake_queue = Mock()
        monkeypatch.setattr(main, "worker", fake_worker)
        monkeypatch.setattr(main, "queue_adapter", fake_queue)
        monkeypatch.setattr(main, "settings", SimpleNamespace(
            worker_concurrency=1, argus_worker_enabled=True, argus_reconciler_enabled=False,
            argus_db_mode="standalone"))
        monkeypatch.setenv("ARGUS_MANAGED_SCORE_CONCURRENCY", "1")
        monkeypatch.setattr(main.outbox_syncer, "process_batch", lambda **kw: 0)
        monkeypatch.setattr(main.run_score_syncer, "process_batch", lambda **kw: 0)
        monkeypatch.setattr(main, "build_link_loop_lease", lambda redis: SimpleNamespace(
            try_acquire=lambda: False, release=lambda: None))
        async with main.lifespan(main.app):
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                await asyncio.wait_for(http_started.wait(), timeout=1)
                assert len(managed_calls) == 1, "评分槽必须有界，不创建无限等待协程"
                fake_queue.ack.assert_any_call("managed-2")
            finally:
                release.set()

    asyncio.run(scenario())


def test_timed_out_thread_cannot_overwrite_new_generation(runtime, monkeypatch):
    import copy
    import threading

    from app.langfuse_managed_scores import ManagedScoreCollector
    from test_issue_217_managed_async import _make_collection_due

    db, _, item_id, obs, good = start(runtime)
    released, finished = threading.Event(), threading.Event()

    class Slow(Remote):
        def scores_for(self, *args):
            assert released.wait(5), "故障注入线程必须在测试结束前释放"
            return super().scores_for(*args)

    slow = Slow(obs, [good])
    collect = ManagedScoreCollector.collect

    def monitored(self, *args, **kwargs):
        try:
            return collect(self, *args, **kwargs)
        finally:
            if self.client is slow:
                finished.set()

    monkeypatch.setattr(ManagedScoreCollector, "collect", monitored)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_COLLECT_TIMEOUT_SECONDS", "0.05")
    try:
        out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="timed-out",
                                     client_factory=lambda: slow)
        assert out["reason"] == "RETRY_SCHEDULED"
        due = _make_collection_due(db, item_id)
        replacement = {**copy.deepcopy(good), "id": "new-valid-score"}
        monkeypatch.setenv("ARGUS_MANAGED_SCORE_COLLECT_TIMEOUT_SECONDS", "5")
        done = recover_managed_scores(db, item_id=item_id, evaluation_generation=due.evaluation_generation,
                                      worker_id="replacement", client_factory=lambda: Remote(obs, [replacement]))
        assert done["finalized"] and done["eval_status"] == "succeeded"
        released.set()
        assert finished.wait(5)
        results = rows(db, EvaluationResultRecord, item_id)
        assert len(results) == 1 and results[0].external_score_id == "new-valid-score"
        assert _item(db, item_id).evaluation_status == "recovered"
        assert max(rows(db, EvaluationAttemptRecord, item_id), key=lambda r: r.evaluation_generation).status == "succeeded"
        assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1
    finally:
        released.set()
        assert finished.wait(5)


def test_two_postgresql_reconcilers_advance_one_generation(runtime):
    if runtime[0].engine.dialect.name != "postgresql":
        pytest.skip("row-lock interleaving requires PostgreSQL")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.db_models import ExperimentItemExecutionRecord as Item

    db, _, item_id, _, _ = start(runtime)
    assert claim_evaluation(db, item_id=item_id, evaluation_generation=1, worker_id="crashed")
    with db.get_session() as session:
        session.get(Item, item_id).evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    barrier = Barrier(2)

    def scanned(conn, cursor, statement, parameters, context, executemany):
        if "evaluation_lease_expires_at IS NOT NULL" in statement:
            barrier.wait(timeout=5)

    event.listen(db.engine, "after_cursor_execute", scanned)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: runtime[5].reconcile_expired_evaluation_leases(), range(2)))
    finally:
        event.remove(db.engine, "after_cursor_execute", scanned)
    assert sorted(outcomes) == [0, 1]
    assert _item(db, item_id).evaluation_generation == 2
    assert len(rows(db, EvaluationAttemptRecord, item_id)) == 1
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_managed_outbox_restores_scorable_observation_from_checkpoint(runtime, monkeypatch):
    """SDK 尚未导出即进程退出，已有 Outbox 必须独立恢复真实可评分 TOOL。"""
    import json
    from types import SimpleNamespace

    from app.db_models import (
        ExecutionCheckpointRecord,
        ExperimentItemExecutionRecord,
        ExperimentLaunchRecord,
        LangfuseSyncTaskRecord,
    )
    from app.langfuse_sync import LangfuseOutboxSyncer
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    db, launch_id, item_id, _, _ = start(runtime)
    with db.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(
            ExecutionCheckpointRecord.item_execution_id == item_id))
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item_id))
        assert task is not None
        session.get(ExperimentLaunchRecord, launch_id).dataset_id = "dataset-langfuse"
        for obj in (item, checkpoint, task):
            obj.trace_id, obj.observation_id = "a" * 32, "b" * 16
        task.dataset_version = "2026-10-20T00:00:00+00:00"
        task.scores_payload = {**task.scores_payload, "_dataset_source": "langfuse"}
    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "otel")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "test-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "test-secret")
    monkeypatch.setattr("app.langfuse_sync.settings", SimpleNamespace(langfuse_base_url="https://langfuse.example"))
    captured = []

    def post(payload, **kwargs):
        req = trace_service_pb2.ExportTraceServiceRequest.FromString(payload)
        captured.extend(span for rs in req.resource_spans for scopes in rs.scope_spans for span in scopes.spans)
        return 202, b""

    monkeypatch.setattr("app.langfuse_otel_projection.post_langfuse_projection", post)
    client = Mock()
    syncer = LangfuseOutboxSyncer(db, client)
    claimed = syncer.claim_tasks()
    assert len(claimed) == 1
    assert syncer.process_single_task(*claimed[0])
    assert len(captured) == 1
    span = captured[0]
    assert span.name == "remote-agent-http"
    attrs = {a.key: a.value.string_value for a in span.attributes}
    assert attrs["langfuse.observation.type"] == "tool"
    assert attrs["langfuse.environment"] == "production"
    assert json.loads(attrs["langfuse.observation.input"])["request"] == checkpoint.input_payload
    assert json.loads(attrs["langfuse.observation.output"]) == {"answer": "ok"}
    assert span.trace_id.hex() == "a" * 32 and span.span_id.hex() == "b" * 16
    client.api.scores.create.assert_not_called()
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


@pytest.mark.parametrize("prior", ["PENDING", "SYNCED", "FAILED", "SKIPPED"])
def test_managed_collection_waits_for_durable_observation_publication(runtime, monkeypatch, prior):
    from app.db_models import LangfuseSyncTaskRecord

    db, _, item_id, obs, good = start(runtime)
    with db.get_session() as session:
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item_id))
        task.status = prior
    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "otel")
    factory = Mock(return_value=Remote(obs, [good]))
    out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1, worker_id="restore",
                                 client_factory=factory)
    assert out["reason"] == "RETRY_SCHEDULED"
    factory.assert_not_called()
    with db.get_session() as session:
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item_id))
        assert task.status == "PENDING"
    assert _item(db, item_id).eval_status == "pending"
    assert _item(db, item_id).quality_conclusion == "unknown"
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


@pytest.mark.parametrize("strategy", ["otel", "legacy"])
def test_managed_sdk_and_outbox_have_one_scorable_publisher(runtime, monkeypatch, strategy):
    from unittest.mock import MagicMock

    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", strategy)
    client = MagicMock()
    client.get_current_trace_id.return_value = "a" * 32
    client.get_current_observation_id.return_value = "b" * 16
    result = _run_managed_http(runtime, _managed_manifest(), {"answer": "ok"}, lf_client=client)
    assert result[-2] == 1 and result[-1]
    children = [call.kwargs for call in client.start_as_current_observation.call_args_list
                if call.kwargs.get("name", "").startswith("remote-agent-http")]
    assert len(children) == 1
    assert children[0]["as_type"] == ("span" if strategy == "otel" else "tool")
    assert children[0]["name"] == ("remote-agent-http-transport" if strategy == "otel" else "remote-agent-http")


def test_managed_projection_refuses_corrupt_checkpoint(runtime):
    from app.db_models import ExecutionCheckpointRecord, ExperimentItemExecutionRecord, LangfuseSyncTaskRecord
    from app.execution_checkpoint import CheckpointUnavailableError
    from app.langfuse_run_items import read_managed_observation_projection

    db, _, item_id, _, _ = start(runtime)
    with db.get_session() as session:
        cp = session.scalar(select(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.item_execution_id == item_id))
        cp.agent_output = {"tampered": True}
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item_id))
        item = session.get(ExperimentItemExecutionRecord, item_id)
        payload = {"launch_id": item.launch_id, "item_id": item_id, "dispatch_generation": item.dispatch_generation,
                   "trace_id": task.trace_id, "observation_id": task.observation_id}
    with pytest.raises(CheckpointUnavailableError) as exc:
        read_managed_observation_projection(db, payload)
    assert exc.value.code == "CHECKPOINT_CORRUPT"
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_projection_failure_resubmit_has_one_new_budget_without_agent(runtime, monkeypatch):
    from app.db_models import LangfuseSyncTaskRecord
    from test_issue_217_managed_async import _make_collection_due

    db, launch_id, item_id, obs, _ = start(runtime)
    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "otel")
    factory = Mock(return_value=Remote(obs, []))
    first = recover_managed_scores(db, item_id=item_id, evaluation_generation=1,
                                   worker_id="pending", client_factory=factory)
    assert first["reason"] == "RETRY_SCHEDULED"
    with db.get_session() as session:
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item_id))
        task.status = "FAILED"
        task.attempts = 5
    due = _make_collection_due(db, item_id)
    failed = recover_managed_scores(db, item_id=item_id, evaluation_generation=due.evaluation_generation,
                                    worker_id="failed", client_factory=factory)
    assert failed["reason"] == "OBSERVATION_PROJECTION_FAILED" and failed["finalized"]
    assert item_id in runtime[3].retry_failed_evaluations(launch_id)["submitted"]
    current = _item(db, item_id)
    again = recover_managed_scores(db, item_id=item_id, evaluation_generation=current.evaluation_generation,
                                   worker_id="resubmitted", client_factory=factory)
    assert again["reason"] == "RETRY_SCHEDULED"
    with db.get_session() as session:
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item_id))
        assert task.status == "PENDING" and task.attempts == 0
    factory.assert_not_called()
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


@pytest.mark.parametrize("code", ["RATE_LIMITED", "UPSTREAM_UNAVAILABLE", "TIMEOUT"])
def test_transient_score_failure_retries_then_converges_without_agent(runtime, code):
    from test_issue_217_managed_async import _make_collection_due

    db, _, item_id, obs, good = start(runtime)

    class Fault(Remote):
        def scores_for(self, *args):
            raise ScoresReadError(code)

    out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1,
                                 worker_id="fault", client_factory=lambda: Fault(obs, []))
    assert out["reason"] == "RETRY_SCHEDULED"
    waiting = _item(db, item_id)
    assert waiting.execution_status == "succeeded" and waiting.eval_status == "pending"
    assert waiting.quality_conclusion == "unknown" and waiting.evaluation_generation == 2
    due = _make_collection_due(db, item_id)
    done = recover_managed_scores(db, item_id=item_id, evaluation_generation=due.evaluation_generation,
                                  worker_id="restored", client_factory=lambda: Remote(obs, [good]))
    assert done["finalized"] and done["eval_status"] == "succeeded"
    assert len(rows(db, EvaluationResultRecord, item_id)) == 1
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


@pytest.mark.parametrize("code", ["AUTHENTICATION_FAILED", "AUTHORIZATION_FAILED"])
def test_auth_failure_stops_unknown_without_agent(runtime, code):
    db, _, item_id, obs, _ = start(runtime)

    class Fault(Remote):
        def scores_for(self, *args):
            raise ScoresReadError(code)

    out = recover_managed_scores(db, item_id=item_id, evaluation_generation=1,
                                 worker_id="denied", client_factory=lambda: Fault(obs, []))
    assert out["finalized"] and out["reason"] == code
    final = _item(db, item_id)
    assert final.execution_status == "succeeded" and final.eval_status == "failed"
    assert final.quality_conclusion == "unknown"
    assert rows(db, EvaluationResultRecord, item_id) == []
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_late_attempt_start_cannot_spend_new_manual_cycle_budget(runtime, monkeypatch):
    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.managed_score_recovery import _cycle_attempts_used, _record_managed_attempt_started

    db, launch_id, item_id, obs, good = start(runtime)
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "1")
    old = claim_evaluation(db, item_id=item_id, evaluation_generation=1, worker_id="paused-before-record")
    with db.get_session() as session:
        session.get(Item, item_id).evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
    assert runtime[5].reconcile_expired_evaluation_leases() == 1
    assert _item(db, item_id).evaluation_status == "failed"
    assert item_id in runtime[3].retry_failed_evaluations(launch_id)["submitted"]
    current = _item(db, item_id)
    _record_managed_attempt_started(db, item_id=item_id, launch_id=launch_id, evaluation_generation=1,
                                   target_bindings=[], reused_output_digest="stale", worker_id="late-old",
                                   lease_token=old["lease_token"])
    with db.get_session() as session:
        assert _cycle_attempts_used(session, item_id, current.evaluation_started_at) == 0
    done = recover_managed_scores(db, item_id=item_id, evaluation_generation=current.evaluation_generation,
                                  worker_id="current", client_factory=lambda: Remote(obs, [good]))
    assert done["finalized"] and done["eval_status"] == "succeeded"
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_cancel_in_http_success_submission_gap_settles(runtime):
    """HTTP 已持久成功但首轮提交前崩溃，取消也必须 settled。"""
    from app.db_models import ExperimentItemExecutionRecord as Item
    from app.db_models import RunResultSnapshotRecord as Snapshot

    db, launch_id, item_id, _, _ = start(runtime)
    with db.get_session() as session:
        item = session.get(Item, item_id)
        item.evaluation_status = "none"
        item.evaluation_generation = 0
        item.evaluation_started_at = None
        item.available_at = None
    runtime[3].cancel_launch(launch_id)
    runtime[5].run_reconcile_cycle()
    final = _item(db, item_id)
    assert final.execution_status == "succeeded"
    assert final.eval_status == final.evaluation_status == "failed"
    assert "EVALUATION_CANCELLED" in final.evaluation_error
    assert final.quality_conclusion == "unknown"
    assert final.available_at is None and final.evaluation_lease_token is None
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1

    with db.get_session() as session:
        snapshot = session.scalar(select(Snapshot).where(Snapshot.launch_id == launch_id)
                                  .order_by(Snapshot.revision.desc()))
        assert snapshot is not None
        assert snapshot.items[0]["eval_status"] == "failed"
        assert snapshot.items[0]["quality_conclusion"] == "unknown"


def test_replayed_failure_cannot_rewrite_same_lease_success_audit(runtime):
    """即使晚到收尾带原胜出 token，已成功 attempt 也不可改写。"""
    from app import managed_score_recovery as recovery

    db, launch_id, item_id, obs, good = start(runtime)
    claim_seen = []
    original_claim = recovery.claim_evaluation

    def claim(*args, **kwargs):
        result = original_claim(*args, **kwargs)
        claim_seen.append(result)
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(recovery, "claim_evaluation", claim)
        result = recover_managed_scores(
            db, item_id=item_id, evaluation_generation=1, worker_id="winner",
            client_factory=lambda: Remote(obs, [good]),
        )
    assert result["reason"] == "MANAGED_SCORES_ADOPTED"
    before = rows(db, EvaluationAttemptRecord, item_id)[0]
    rejected = _fail_managed(
        db, claim_seen[0], launch_id=launch_id,
        error_code="LATE_FAILURE", error_message="迟到重复收尾",
    )
    assert rejected["reason"] == "SUPERSEDED_OR_LEASE_LOST"
    after = rows(db, EvaluationAttemptRecord, item_id)[0]
    assert (after.status, after.completed_at, after.error_type) == (
        before.status, before.completed_at, before.error_type,
    )
    assert after.status == "succeeded"
    assert _item(db, item_id).quality_conclusion == "pass"
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_finalize_cannot_publish_success_after_deadline_inside_policy(runtime, monkeypatch):
    """锁内计算质量结论期间跨 Deadline，写入前仍需拒绝 PASS。"""
    from app import managed_score_recovery as recovery
    from app import quality_policy
    from app.db_models import ExperimentItemExecutionRecord as Item

    db, _, item_id, obs, good = start(runtime)
    clock = [datetime.now(UTC)]
    monkeypatch.setattr(recovery, "_now", lambda: clock[0])
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_DEADLINE_SECONDS", "1")
    with db.get_session() as session:
        session.get(Item, item_id).evaluation_started_at = clock[0]
    evaluate = quality_policy.evaluate_quality_policy

    def delayed(*args, **kwargs):
        decision = evaluate(*args, **kwargs)
        clock[0] += timedelta(seconds=2)
        return decision

    monkeypatch.setattr(quality_policy, "evaluate_quality_policy", delayed)
    result = recover_managed_scores(
        db, item_id=item_id, evaluation_generation=1, worker_id="late-policy",
        client_factory=lambda: Remote(obs, [good]),
    )
    assert result["reason"] == "MANAGED_SCORES_DEADLINE_EXCEEDED"
    final = _item(db, item_id)
    assert final.eval_status == "failed" and final.quality_conclusion == "unknown"
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_timed_out_collectors_still_count_towards_process_inflight_budget(monkeypatch):
    """超时 caller 退场不等于远端线程退出，不能逃逸 Managed 在途上限。"""
    import threading

    from app import managed_score_recovery as recovery
    from app.langfuse_managed_scores import ManagedScoreCollector

    monkeypatch.setenv("ARGUS_MANAGED_SCORE_CONCURRENCY", "1")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def collect(*args, **kwargs):
        calls.append(1)
        entered.set()
        try:
            assert release.wait(5)
            return {"decisions": []}
        finally:
            finished.set()

    monkeypatch.setattr(ManagedScoreCollector, "collect", collect)
    try:
        first = recovery._collect_with_timeout(
            None, lambda: object(), "launch", "first", fence={}, timeout_seconds=0.02,
        )
        assert entered.is_set() and first["decisions"][0]["reason"] == "TIMEOUT"
        second = recovery._collect_with_timeout(
            None, lambda: object(), "launch", "second", fence={}, timeout_seconds=0.02,
        )
        assert len(calls) == 1
        assert second["decisions"][0]["reason"] == "COLLECTOR_BUSY"
    finally:
        release.set()
        assert finished.wait(5)


@pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "NaN", "1e309", "bad", "-1", "0"])
def test_managed_concurrency_config_safely_falls_back(raw, monkeypatch):
    from app.worker import _managed_slot_count

    monkeypatch.setenv("ARGUS_MANAGED_SCORE_CONCURRENCY", raw)
    assert _managed_slot_count() == 4


def test_managed_concurrency_config_has_finite_upper_bound(monkeypatch):
    from app.worker import _managed_slot_count

    monkeypatch.setenv("ARGUS_MANAGED_SCORE_CONCURRENCY", "1e308")
    assert _managed_slot_count() == 100


def test_future_generation_cannot_skip_persisted_backoff(runtime):
    """正式保留原独立探针：提前消息不能凭下一代身份绕过 available_at。"""
    db, _, item_id, obs, good = start(runtime)
    first = recover_managed_scores(
        db, item_id=item_id, evaluation_generation=1, worker_id="first",
        client_factory=lambda: Remote(obs, []),
    )
    assert first["reason"] == "RETRY_SCHEDULED"
    waiting = _item(db, item_id)
    assert waiting.available_at.replace(tzinfo=UTC) > datetime.now(UTC)
    factory = Mock(return_value=Remote(obs, [good]))
    early = recover_managed_scores(
        db, item_id=item_id, evaluation_generation=waiting.evaluation_generation,
        worker_id="premature", client_factory=factory,
    )
    assert not early["claimed"]
    factory.assert_not_called()
    assert len(rows(db, ExecutionAttemptRecord, item_id)) == 1


def test_live_dispatcher_isolates_managed_from_default_executor(setup_runtime, monkeypatch):
    """真实 Worker 的阻塞收集不能耗尽 poll_queue / HTTP 共用的默认执行器。"""
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from app import main
    from app import managed_score_recovery as recovery

    worker = setup_runtime[4]
    entered, release = threading.Event(), threading.Event()
    sent = [False, False]

    def collect(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"claimed": True, "finalized": False}

    def poll(**kwargs):
        if not sent[0]:
            sent[0] = True
            return [("managed", "item-score", 1, "MANAGED_SCORE")]
        if entered.is_set() and not sent[1]:
            sent[1] = True
            return [("http", "item-http", 1, "INVOCATION")]
        return []

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        http_started = asyncio.Event()

        async def invoke(*unused):
            http_started.set()

        monkeypatch.setattr(recovery, "recover_managed_scores", collect)
        monkeypatch.setattr(worker, "poll_queue", poll)
        monkeypatch.setattr(worker, "execute_item_message", invoke)
        monkeypatch.setattr(worker, "queue", Mock())
        monkeypatch.setattr(main, "worker", worker)
        monkeypatch.setattr(main, "settings", SimpleNamespace(
            worker_concurrency=1, argus_worker_enabled=True, argus_reconciler_enabled=False,
            argus_db_mode="standalone"))
        monkeypatch.setattr(main.outbox_syncer, "process_batch", lambda **unused: 0)
        monkeypatch.setattr(main.run_score_syncer, "process_batch", lambda **unused: 0)
        monkeypatch.setattr(main, "build_link_loop_lease", lambda _: Mock(try_acquire=lambda: False))
        async with main.lifespan(main.app):
            try:
                await asyncio.wait_for(http_started.wait(), timeout=1)
                assert entered.is_set() and not release.is_set()
            finally:
                release.set()
        assert worker._managed_executor is None

    asyncio.run(scenario())
