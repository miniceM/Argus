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
