from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

import app.limiter as limiter_module  # noqa: E402
from app.db_models import ExecutionAttemptRecord as Attempt  # noqa: E402
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402
from app.executor import RemoteAgentExecutor, SingleInvocationResult  # noqa: E402
from app.queue import RedisStreamQueueAdapter  # noqa: E402


def create_launch_helper(env, count: int = 1) -> tuple[str, list[tuple[str, str, int]]]:
    db_mgr, queue, limiter, orch, worker, rec = env
    manifest = {
        "agent": {
            "agent_id": "test-agent",
            "version": "v1",
            "agent_version_id": "test-agent-v1",
            "endpoint": "http://localhost/invoke",
            "method": "POST",
            "request_mapping": {},
            "is_idempotent": False,
        },
        "execution_policy": {
            "timeout_seconds": 60,
            "max_retries": 2,
            "max_concurrency": 1,
            "rate_limit_per_minute": 60,
        },
        "dataset": {"items": [{"id": str(i), "input": {}} for i in range(count)]},
        "evaluators": [],
    }
    launch = orch.create_launch("test-agent", "v1", "ds", "v1", "review", manifest)
    orch.start_launch(launch.id)
    msgs = queue.read_group("review", count=count)
    return launch.id, msgs


# 1. Quality failure must not become launch pass
def test_quality_failure_must_not_become_launch_pass(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    with db_mgr.get_session() as s:
        item = s.get(Item, msgs[0][1])
        item.execution_status = "succeeded"
        item.eval_status = "succeeded"
        item.quality_conclusion = "fail"
    rec.reconcile_launch_states()
    with db_mgr.get_session() as s:
        assert s.get(Launch, lid).quality_conclusion == "fail"


# 2. Resume must not bypass ambiguous protection
def test_resume_must_not_bypass_ambiguous_protection(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    with db_mgr.get_session() as s:
        item = s.get(Item, msgs[0][1])
        item.execution_status = "failed"
        s.get(Launch, lid).status = "FAILED"
        s.add(
            Attempt(
                id="ambiguous",
                item_execution_id=item.id,
                attempt_no=1,
                status="FAILED",
                error_type="AMBIGUOUS_OUTCOME",
            )
        )
    with pytest.raises(ValueError, match="AMBIGUOUS_OUTCOME"):
        orch.retry_failed_items(lid)
    with pytest.raises(ValueError, match="AMBIGUOUS_OUTCOME"):
        orch.resume_launch(lid)


# 3. Limiter wait must not create attempt
def test_limiter_wait_must_not_create_attempt(setup_runtime, monkeypatch):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    monkeypatch.setattr(lim, "acquire_rate_permit", lambda *args, **kwargs: False)
    asyncio.run(w.execute_item_message(*msgs[0]))
    with db_mgr.get_session() as s:
        attempts = s.scalars(select(Attempt)).all()
        assert len(attempts) == 0


# 4. Cancellation with finished item reaches CANCELLED
def test_cancellation_with_finished_item_reaches_cancelled(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime, 2)
    claim = w.claim_item(msgs[0][1], 1)
    orch.cancel_launch(lid)
    assert w.finalize_item(msgs[0][1], 1, claim["lease_token"], "SUCCEEDED", "succeeded", "pass")
    rec.reconcile_launch_states()
    with db_mgr.get_session() as s:
        assert s.get(Launch, lid).status == "CANCELLED"


# 5. Expired lease cannot be resurrected
def test_expired_lease_cannot_be_resurrected(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    claim = w.claim_item(msgs[0][1], 1)
    with db_mgr.get_session() as s:
        s.get(Item, msgs[0][1]).lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
    assert w.renew_lease(msgs[0][1], claim["lease_token"]) is False


# 6. Worker creates traceparent without incoming request
def test_worker_creates_traceparent_without_incoming_request(setup_runtime, monkeypatch):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    captured = []

    async def invoke(self, payload, headers):
        captured.append(headers)
        return SingleInvocationResult(
            status_code=200,
            body={},
            raw_response="{}",
            headers={},
            duration_ms=1,
            trace_context_received=False,
            error_category=None,
            error_message=None,
            is_retryable=False,
            may_have_side_effects=True,
        )

    monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", invoke)
    asyncio.run(w.execute_item_message(*msgs[0]))
    assert "traceparent" in captured[0]
    assert captured[0]["traceparent"].startswith("00-")


# 7. Concurrency permit survives long call
def test_concurrency_permit_survives_long_call(setup_runtime, monkeypatch):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    clock = [0.0]
    monkeypatch.setattr(limiter_module, "time", types.SimpleNamespace(time=lambda: clock[0]))
    extra_permits = []

    async def invoke(self, payload, headers):
        clock[0] = 31.0
        extra_permits.append(lim.acquire_concurrency_permit("test-agent-v1", 1, owner_id="second-worker"))
        return SingleInvocationResult(
            status_code=200,
            body={},
            raw_response="{}",
            headers={},
            duration_ms=31000,
            trace_context_received=False,
            error_category=None,
            error_message=None,
            is_retryable=False,
            may_have_side_effects=True,
        )

    monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", invoke)
    asyncio.run(w.execute_item_message(*msgs[0]))
    assert extra_permits == [None]


# 8. Redis Stream Queue Adapter recovers on NOGROUP
def test_redis_queue_recovers_on_nogroup():
    mock_redis = MagicMock()
    # First xreadgroup raises NOGROUP, then succeeds
    import redis.exceptions
    mock_redis.xreadgroup.side_effect = [
        redis.exceptions.ResponseError("NOGROUP No such key 'argus:stream' or consumer group 'argus:workers'"),
        [("argus:stream", [("msg-1", {"item_execution_id": "item-1", "dispatch_generation": "1"})])],
    ]

    adapter = RedisStreamQueueAdapter(mock_redis)
    res = adapter.read_group("worker-1", count=1)
    assert len(res) == 1
    assert res[0][1] == "item-1"
    # Ensure xgroup_create was called to recreate group
    assert mock_redis.xgroup_create.call_count >= 2


# 9. Metrics recording on item completion and lease expiry
def test_metrics_recorded_on_execution_and_reconciliation(setup_runtime, monkeypatch):
    from app.metrics import runtime_metrics

    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)

    async def invoke(self, payload, headers):
        return SingleInvocationResult(
            status_code=200,
            body={"output": "ok"},
            raw_response="{}",
            headers={},
            duration_ms=10,
            trace_context_received=True,
            error_category=None,
            error_message=None,
            is_retryable=False,
            may_have_side_effects=False,
        )

    monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", invoke)
    prev_succeeded = runtime_metrics.item_executions_total.get("SUCCEEDED", 0)
    asyncio.run(w.execute_item_message(*msgs[0]))
    assert runtime_metrics.item_executions_total.get("SUCCEEDED", 0) == prev_succeeded + 1
    assert runtime_metrics.attempts_total.get("COMPLETED", 0) >= 1

    # Verify launch transitioned to RUNNING
    with db_mgr.get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.status in ("RUNNING", "COMPLETED")


# 10. Evaluator FAIL must yield COMPLETED + fail (never pass or unknown)
def test_evaluator_fail_must_not_become_unknown_or_pass():
    from app.state_machine import aggregate_launch_status_from_items

    # 2 succeeded items: one pass, one fail
    counts = {"succeeded": 2}
    quality = {"pass": 1, "fail": 1}
    st, q = aggregate_launch_status_from_items(counts, quality)
    assert st == "COMPLETED"
    assert q == "fail"


# 11. Evaluator UNKNOWN must yield COMPLETED + unknown (never pass)
def test_evaluator_unknown_must_not_become_pass():
    from app.state_machine import aggregate_launch_status_from_items

    counts = {"succeeded": 2}
    quality = {"pass": 1, "unknown": 1}
    st, q = aggregate_launch_status_from_items(counts, quality)
    assert st == "COMPLETED"
    assert q == "unknown"


# 12. Outbox PROCESSING lease expiry reclaim and strong confirmation
def test_outbox_processing_lease_expiry_reclaim(setup_runtime):
    import uuid

    from app.db_models import LangfuseSyncTaskRecord
    from app.langfuse_sync import LangfuseOutboxSyncer

    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    item_id = msgs[0][1]

    # Create an expired PROCESSING task simulating crashed worker
    task_id = str(uuid.uuid4())
    past_time = datetime.now(UTC) - timedelta(seconds=60)
    with db_mgr.get_session() as s:
        task = LangfuseSyncTaskRecord(
            id=task_id,
            launch_id=lid,
            item_id=item_id,
            dataset_item_id="ds-item-0",
            dataset_version="v1",
            dispatch_generation=1,
            task_type="FULL_EVAL_SYNC",
            trace_id="trace-123",
            observation_id="obs-123",
            dataset_run_name="test-run",
            scores_payload={"accuracy": 1.0},
            status="PROCESSING",
            owner_id="crashed-syncer",
            claim_token="old-token",
            lease_expires_at=past_time,
            attempts=1,
            next_retry_at=past_time,
        )
        s.add(task)
        s.commit()

    # Mock Langfuse API
    mock_lf = MagicMock()
    mock_lf.api.dataset_run_items.create.return_value = MagicMock()
    mock_lf.api.scores.create.return_value = MagicMock()

    syncer = LangfuseOutboxSyncer(db_mgr, mock_lf, syncer_id="new-syncer")
    processed = syncer.process_batch(batch_size=1)
    assert processed == 1

    # Verify task was successfully reclaimed and updated to SYNCED
    with db_mgr.get_session() as s:
        synced_task = s.get(LangfuseSyncTaskRecord, task_id)
        assert synced_task.status == "SYNCED"
        assert synced_task.claim_token != "old-token"
        assert synced_task.lease_expires_at is None

    # Verify remote calls were made with dataset_version and stable score_id
    mock_lf.api.dataset_run_items.create.assert_called_once_with(
        run_name="test-run",
        dataset_item_id="ds-item-0",
        dataset_version="v1",
        trace_id="trace-123",
        observation_id="obs-123",
    )
    mock_lf.api.scores.create.assert_called_once_with(
        id=f"score:{item_id}:gen1:accuracy",
        name="accuracy",
        value=1.0,
        trace_id="trace-123",
        observation_id="obs-123",
    )


# 13. Finalize CAS requires active_attempt_id and Attempt status RUNNING
def test_finalize_active_attempt_id_cas_and_running_status_enforced(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime)
    msg_id, item_id, gen = msgs[0]

    # Claim and authorize attempt
    claim_info = w.claim_item(item_id, gen)
    assert claim_info is not None
    token = claim_info["lease_token"]

    att_no = w.authorize_attempt(item_id, token, gen)
    assert att_no is not None

    with db_mgr.get_session() as s:
        it = s.get(Item, item_id)
        active_att_id = it.active_attempt_id
        assert active_att_id is not None
        att = s.get(Attempt, active_att_id)
        assert att.dispatch_generation == gen
        assert att.lease_token == token
        assert att.status == "RUNNING"

    # A) Attempt status corrupted to FAILED before finalize -> finalize must abort/rollback
    with db_mgr.get_session() as s:
        att = s.get(Attempt, active_att_id)
        att.status = "FAILED"
        s.commit()

    ok = w.finalize_execution_and_attempt(
        item_id=item_id,
        generation=gen,
        lease_token=token,
        target_item_status="SUCCEEDED",
        target_eval_status="succeeded",
        target_quality_conclusion="pass",
        current_attempt_id=active_att_id,
        attempt_updates={"status": "COMPLETED"},
        scores={"accuracy": 1.0},
    )
    assert ok is False

    # Verify item is still running and active_attempt_id was not cleared
    with db_mgr.get_session() as s:
        it = s.get(Item, item_id)
        assert it.execution_status == "running"
        assert it.active_attempt_id == active_att_id


# 14. Interleaved Resume and Retry Failed contracts on PARTIAL_FAILED
def test_interleaved_resume_and_retry_failed_contracts(setup_runtime):
    from app.state_machine import determine_allowed_actions

    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime, count=3)

    # Item 0: Succeeded, Item 1: Failed, Item 2: Cancelled
    with db_mgr.get_session() as s:
        it0 = s.get(Item, msgs[0][1])
        it0.execution_status = "succeeded"
        it0.eval_status = "succeeded"
        it0.quality_conclusion = "pass"

        it1 = s.get(Item, msgs[1][1])
        it1.execution_status = "failed"
        it1.eval_status = "skipped"
        it1.quality_conclusion = "fail"

        it2 = s.get(Item, msgs[2][1])
        it2.execution_status = "cancelled"
        it2.eval_status = "skipped"
        it2.quality_conclusion = "unknown"
        s.commit()

    # Reconcile Launch -> converges to PARTIAL_FAILED
    rec.reconcile_all_active_launches()
    with db_mgr.get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.status == "PARTIAL_FAILED"

    # Allowed actions must include BOTH resume and retry_failed
    actions = determine_allowed_actions(
        launch_status="PARTIAL_FAILED",
        cancel_requested_at=None,
        counts={"succeeded": 1, "failed": 1, "cancelled": 1, "queued": 0, "running": 0, "retry_wait": 0},
    )
    assert "resume" in actions["allowed"]
    assert "retry_failed" in actions["allowed"]

    # 1. Trigger Retry Failed -> advances item 1 to generation 2, resets completed_at, launch goes to QUEUED
    res_retry = orch.retry_failed_items(lid)
    assert res_retry.status == "QUEUED"
    assert res_retry.cancel_requested_at is None

    with db_mgr.get_session() as s:
        it1 = s.get(Item, msgs[1][1])
        assert it1.dispatch_generation == 2
        assert it1.execution_status == "queued"
        assert it1.eval_status == "pending"
        assert it1.completed_at is None

        # Item 2 remains cancelled
        it2 = s.get(Item, msgs[2][1])
        assert it2.execution_status == "cancelled"

    # Simulate item 1 succeeding in gen 2
    with db_mgr.get_session() as s:
        it1 = s.get(Item, msgs[1][1])
        it1.execution_status = "succeeded"
        it1.eval_status = "succeeded"
        it1.quality_conclusion = "pass"
        s.commit()

    # Reconcile -> now succeeded=2, cancelled=1, fails=0 -> converges to CANCELLED
    rec.reconcile_all_active_launches()
    with db_mgr.get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.status == "CANCELLED"

    # 2. Trigger Resume -> advances item 2 to generation 2, launch goes to QUEUED
    res_resume = orch.resume_launch(lid)
    assert res_resume.status == "QUEUED"
    assert res_resume.cancel_requested_at is None

    with db_mgr.get_session() as s:
        it2 = s.get(Item, msgs[2][1])
        assert it2.dispatch_generation == 2
        assert it2.execution_status == "queued"
        assert it2.eval_status == "pending"
        assert it2.completed_at is None

    # Simulate item 2 succeeding in gen 2
    with db_mgr.get_session() as s:
        it2 = s.get(Item, msgs[2][1])
        it2.execution_status = "succeeded"
        it2.eval_status = "succeeded"
        it2.quality_conclusion = "pass"
        s.commit()

    # Reconcile -> all succeeded -> COMPLETED + pass
    rec.reconcile_all_active_launches()
    with db_mgr.get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.status == "COMPLETED"
        assert launch.quality_conclusion == "pass"


def test_lease_renewal_before_expiry_prevents_stale_reconciler_reclaim(setup_runtime, monkeypatch):
    """Reconciler scans expired record, but worker successfully renews lease before reconciler writes back.
    Reconciler's CAS must fail, leaving worker ownership and attempt intact.
    """
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime, count=1)
    item_id = msgs[0][1]

    now = datetime.now(UTC)
    # 1. Setup item as running with lease expired 10 seconds ago
    with db_mgr.get_session() as s:
        it = s.get(Item, item_id)
        it.execution_status = "running"
        it.lease_owner = "worker-1"
        it.lease_token = "token-1"
        it.dispatch_generation = 1
        it.lease_expires_at = now - timedelta(seconds=10)
        att = Attempt(
            id="att-1",
            item_execution_id=item_id,
            attempt_no=1,
            status="RUNNING",
            request_phase="MAY_HAVE_BEEN_SENT",
            started_at=now - timedelta(seconds=20),
        )
        s.add(att)
        it.active_attempt_id = "att-1"
        s.commit()

    # 2. Reconciler starts scanning, finds item. But right before writeback, worker successfully renews lease
    from app.db_models import ExperimentLaunchRecord
    from sqlalchemy.orm import Session

    orig_get = Session.get
    renewed = False

    def hooked_get(self, entity, ident, *args, **kwargs):
        nonlocal renewed
        res = orig_get(self, entity, ident, *args, **kwargs)
        if not renewed and entity == ExperimentLaunchRecord:
            renewed = True
            # Worker successfully renewed lease before Reconciler's CAS update
            it_db = orig_get(self, Item, item_id)
            if it_db:
                it_db.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
                self.flush()
        return res

    monkeypatch.setattr(Session, "get", hooked_get)

    reclaimed = rec.reconcile_expired_leases()
    # CAS must have rejected the update because lease_expires_at <= now is False!
    assert reclaimed == 0

    with db_mgr.get_session() as s:
        it = s.get(Item, item_id)
        att = s.get(Attempt, "att-1")
        assert it.execution_status == "running"
        assert att.status == "RUNNING"
        assert att.error_type is None


def test_clock_timestamp_lock_wait_expiration_fails_on_postgres():
    """Real PostgreSQL integration test verifying that clock_timestamp() catches lease expiry occurring
    during row lock contention, which transaction start time (now()) would have missed.
    """
    import os
    import time
    from pathlib import Path

    from app.db import MigrationRunner
    from sqlalchemy import create_engine, text

    pg_url = os.getenv("TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("TEST_POSTGRES_URL is not configured; skipping real PostgreSQL lock-wait clock_timestamp test")

    root_dir = Path(__file__).resolve().parents[1]
    engine = create_engine(pg_url)
    runner = MigrationRunner(engine=engine, migrations_dir=root_dir / "migrations")
    runner.apply_all()

    with engine.connect() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE execution_attempts, experiment_item_executions, "
                "experiment_launches, agent_versions, agents CASCADE;"
            )
        )
        conn.commit()

        # Seed agent, version, launch, item
        conn.execute(text("INSERT INTO agents (id, name, status) VALUES ('ag-pg', 'AG', 'active');"))
        conn.execute(
            text(
                "INSERT INTO agent_versions (id, agent_id, version, spec_digest, endpoint) "
                "VALUES ('ag-v1', 'ag-pg', 'v1', 'dig', 'http://localhost:8080/invoke');"
            )
        )
        conn.execute(
            text(
                "INSERT INTO experiment_launches (id, agent_id, agent_version, dataset_name, dataset_version, status, manifest) "
                "VALUES ('l-pg', 'ag-pg', 'v1', 'ds', 'v1', 'RUNNING', '{}');"
            )
        )
        # Item with lease expiring in 1.5 seconds
        conn.execute(
            text(
                "INSERT INTO experiment_item_executions "
                "(id, launch_id, dataset_item_id, execution_status, dispatch_generation, lease_owner, lease_token, lease_expires_at) "
                "VALUES ('item-pg-1', 'l-pg', '0', 'running', 1, 'worker-pg', 'tok-pg', clock_timestamp() + interval '1.5 second');"
            )
        )
        conn.commit()

    import threading

    lock_acquired_event = threading.Event()
    worker_started_txn = threading.Event()
    worker_finished = threading.Event()
    worker_cas_result = {}

    def holder_thread():
        # Connection 1: Holds lock for 2.5 seconds, ensuring lease expires while held
        with engine.connect() as conn1:
            conn1.execute(text("BEGIN;"))
            conn1.execute(
                text("SELECT id FROM experiment_item_executions WHERE id = 'item-pg-1' FOR UPDATE;")
            )
            lock_acquired_event.set()
            # Wait until worker transaction has started and is blocking on FOR UPDATE
            worker_started_txn.wait(timeout=5)
            # Sleep past the 1.5 second lease expiry
            time.sleep(2.0)
            conn1.execute(text("COMMIT;"))

    def worker_thread():
        lock_acquired_event.wait(timeout=5)
        with engine.connect() as conn2:
            # Transaction 2 begins. Notice: transaction start time now() is frozen at this point
            conn2.execute(text("BEGIN;"))
            worker_started_txn.set()

            # Two-phase row lock: block until conn1 releases
            conn2.execute(
                text("SELECT id FROM experiment_item_executions WHERE id = 'item-pg-1' FOR UPDATE;")
            )

            # Check: now() vs clock_timestamp()
            res_now = conn2.execute(
                text("SELECT now() <= lease_expires_at FROM experiment_item_executions WHERE id = 'item-pg-1';")
            ).scalar()

            res_clock = conn2.execute(
                text("SELECT clock_timestamp() <= lease_expires_at FROM experiment_item_executions WHERE id = 'item-pg-1';")
            ).scalar()

            # CAS update using clock_timestamp()
            cas_res = conn2.execute(
                text(
                    "UPDATE experiment_item_executions "
                    "SET execution_status = 'succeeded' "
                    "WHERE id = 'item-pg-1' "
                    "  AND lease_token = 'tok-pg' "
                    "  AND dispatch_generation = 1 "
                    "  AND lease_expires_at > clock_timestamp();"
                )
            )
            conn2.execute(text("COMMIT;"))
            worker_cas_result["now_valid"] = res_now
            worker_cas_result["clock_valid"] = res_clock
            worker_cas_result["cas_rowcount"] = cas_res.rowcount
            worker_finished.set()

    t1 = threading.Thread(target=holder_thread)
    t2 = threading.Thread(target=worker_thread)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert worker_finished.is_set()
    # Key proof: now() still thought the lease was valid (snapshot time), but clock_timestamp() correctly saw it expired!
    assert worker_cas_result["now_valid"] is True
    assert worker_cas_result["clock_valid"] is False
    assert worker_cas_result["cas_rowcount"] == 0  # CAS correctly rejected!



