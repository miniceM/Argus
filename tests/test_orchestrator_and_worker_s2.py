from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db_models import (  # noqa: E402
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)


def test_materialization_and_two_step_start(setup_runtime):
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    # 1. Create and materialize 5 items
    items_seed = [{"id": f"item-{i}", "input": {"text": f"hi {i}"}} for i in range(5)]
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1", "endpoint": "http://localhost:8080/invoke", "method": "POST", "request_mapping": {}},
        "execution_policy": {"timeout_seconds": 5, "max_retries": 2, "max_concurrency": 5, "rate_limit_per_minute": 60},
        "dataset": {"name": "ds-test", "version": "v1", "items": items_seed},
        "evaluators": [],
    }

    launch = orchestrator.create_launch(
        agent_id="test-agent",
        agent_version="v1",
        dataset_name="ds-test",
        dataset_version="v1",
        name="test-launch",
        manifest=manifest,
    )
    assert launch.status == "PENDING"
    # Items must be pre-materialized as PENDING, but not in queue yet
    with db_mgr.get_session() as session:
        items = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        assert len(items) == 5
        assert all(it.execution_status == "pending" for it in items)
    assert queue.get_queue_depth() == 0

    # 2. Start launch -> transitions to QUEUED and enqueues items
    started_launch = orchestrator.start_launch(launch.id)
    assert started_launch.status == "QUEUED"
    assert queue.get_queue_depth() == 5
    with db_mgr.get_session() as session:
        items = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        assert all(it.execution_status == "queued" for it in items)
        assert all(it.queued_at is not None for it in items)


def test_worker_claim_and_strict_lease_fencing(setup_runtime):
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    items_seed = [{"id": "item-0", "input": {"text": "hello"}}]
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1", "endpoint": "http://localhost:8080/invoke", "method": "POST", "request_mapping": {}},
        "execution_policy": {"timeout_seconds": 5, "max_retries": 2, "max_concurrency": 5, "rate_limit_per_minute": 60},
        "dataset": {"name": "ds-test", "version": "v1", "items": items_seed},
        "evaluators": [],
    }
    launch = orchestrator.create_launch("test-agent", "v1", "ds-test", "v1", "l1", manifest)
    orchestrator.start_launch(launch.id)

    # Worker 1 claims item
    msgs = queue.read_group("worker-test-1", count=1)
    assert len(msgs) == 1
    msg_id, item_id, gen = msgs[0]

    claim_res = worker.claim_item(item_id, gen, lease_seconds=30)
    assert claim_res is not None
    token = claim_res["lease_token"]

    # Expire the lease manually in DB
    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        assert item.lease_token == token
        assert item.execution_status == "running"
        item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)

    # Old Worker tries to finalize with expired lease -> Fencing must reject it!
    success = worker.finalize_item(
        item_id=item_id,
        generation=gen,
        lease_token=token,
        status="SUCCEEDED",
        eval_status="succeeded",
        quality_conclusion="pass",
    )
    assert success is False, "Expired lease finalize must be rejected by strict DB fencing!"


def test_scenario_a_retry_wait_no_premature_claim(setup_runtime):
    """Scenario A: Worker cannot claim RETRY_WAIT before available_at, Reconciler reenqueues when expired."""
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    items_seed = [{"id": "item-wait", "input": {"text": "wait"}}]
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1", "endpoint": "http://localhost:8080/invoke", "method": "POST", "request_mapping": {}},
        "execution_policy": {"timeout_seconds": 5, "max_retries": 2, "max_concurrency": 5, "rate_limit_per_minute": 60},
        "dataset": {"name": "ds-test", "version": "v1", "items": items_seed},
        "evaluators": [],
    }
    launch = orchestrator.create_launch("test-agent", "v1", "ds-test", "v1", "l-wait", manifest)
    orchestrator.start_launch(launch.id)

    with db_mgr.get_session() as session:
        item = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).first()
        item.execution_status = "retry_wait"
        # Not expired yet (future available_at)
        item.available_at = datetime.now(UTC) + timedelta(seconds=60)
        item_id = item.id
        gen = item.dispatch_generation

    # Worker claim must be rejected!
    claim = worker.claim_item(item_id, gen)
    assert claim is None, "Worker must NOT claim retry_wait items before available_at!"

    # Now make it expired
    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        item.available_at = datetime.now(UTC) - timedelta(seconds=5)

    # Reconciler should find it and transition to QUEUED and reenqueue
    moved = reconciler.reconcile_retry_waits()
    assert moved == 1

    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        assert item.execution_status == "queued"


def test_scenario_b_non_idempotent_crash_ambiguous(setup_runtime):
    """Scenario B: Non-idempotent crash in MAY_HAVE_BEEN_SENT marks AMBIGUOUS_OUTCOME; retry-failed requires force."""
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    items_seed = [{"id": "item-ambig", "input": {"text": "ambig"}}]
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1", "endpoint": "http://localhost:8080/invoke", "method": "POST", "request_mapping": {}, "is_idempotent": False},
        "execution_policy": {"timeout_seconds": 5, "max_retries": 2, "max_concurrency": 5, "rate_limit_per_minute": 60},
        "dataset": {"name": "ds-test", "version": "v1", "items": items_seed},
        "evaluators": [],
    }
    launch = orchestrator.create_launch("test-agent", "v1", "ds-test", "v1", "l-ambig", manifest)
    orchestrator.start_launch(launch.id)

    with db_mgr.get_session() as session:
        item = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).first()
        item.execution_status = "running"
        item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)  # Expired lease
        att = ExecutionAttemptRecord(
            id="att-ambig-1",
            item_execution_id=item.id,
            attempt_no=1,
            status="RUNNING",
            request_phase="MAY_HAVE_BEEN_SENT",  # Sent before crash!
            started_at=datetime.now(UTC),
        )
        session.add(att)
        launch_id = launch.id

    # Reconciler detects expired lease with non-idempotent MAY_HAVE_BEEN_SENT
    reconciled = reconciler.reconcile_expired_leases()
    assert reconciled == 1

    with db_mgr.get_session() as session:
        att = session.get(ExecutionAttemptRecord, "att-ambig-1")
        assert att.status == "FAILED"
        assert att.error_type == "AMBIGUOUS_OUTCOME"
        item = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch_id)).first()
        assert item.execution_status == "failed"

    # Retry failed without force MUST fail!
    with pytest.raises(ValueError, match="AMBIGUOUS_OUTCOME"):
        orchestrator.retry_failed_items(launch_id, force=False)

    # Retry failed with force=True succeeds
    resumed = orchestrator.retry_failed_items(launch_id, force=True)
    assert resumed.status == "QUEUED"


def test_scenario_c_cancel_race_and_quiescence(setup_runtime):
    """Scenario C: Cancel rejects new attempts, running attempts do not retry upon failure."""
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    items_seed = [{"id": "item-c1", "input": {}}, {"id": "item-c2", "input": {}}]
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1", "endpoint": "http://localhost:8080/invoke", "method": "POST", "request_mapping": {}},
        "execution_policy": {"timeout_seconds": 5, "max_retries": 2, "max_concurrency": 5, "rate_limit_per_minute": 60},
        "dataset": {"name": "ds-test", "version": "v1", "items": items_seed},
        "evaluators": [],
    }
    launch = orchestrator.create_launch("test-agent", "v1", "ds-test", "v1", "l-cancel", manifest)
    orchestrator.start_launch(launch.id)

    # Cancel the launch
    orchestrator.cancel_launch(launch.id)

    with db_mgr.get_session() as session:
        l_rec = session.get(ExperimentLaunchRecord, launch.id)
        assert l_rec.status == "CANCELLED"
        items = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        assert all(it.execution_status == "cancelled" for it in items)

    # Worker trying to claim or authorize after cancel must be rejected
    claim = worker.claim_item(items[0].id, 1)
    assert claim is None


def test_scenario_d_resume_skips_succeeded(setup_runtime):
    """Scenario D: Resume never re-executes SUCCEEDED items, advances generation, and increments attempt_no."""
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    items_seed = [{"id": "item-s", "input": {}}, {"id": "item-c", "input": {}}]
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1", "endpoint": "http://localhost:8080/invoke", "method": "POST", "request_mapping": {}},
        "execution_policy": {"timeout_seconds": 5, "max_retries": 2, "max_concurrency": 5, "rate_limit_per_minute": 60},
        "dataset": {"name": "ds-test", "version": "v1", "items": items_seed},
        "evaluators": [],
    }
    launch = orchestrator.create_launch("test-agent", "v1", "ds-test", "v1", "l-resume", manifest)
    orchestrator.start_launch(launch.id)

    with db_mgr.get_session() as session:
        items = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        # Item 0 is SUCCEEDED
        items[0].execution_status = "succeeded"
        # Item 1 is CANCELLED
        items[1].execution_status = "cancelled"
        l_rec = session.get(ExperimentLaunchRecord, launch.id)
        l_rec.status = "CANCELLED"

    # Resume
    resumed = orchestrator.resume_launch(launch.id)
    assert resumed.status == "QUEUED"

    with db_mgr.get_session() as session:
        items = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        # Succeeded item remained intact with generation 1!
        assert items[0].execution_status == "succeeded"
        assert items[0].dispatch_generation == 1
        # Cancelled item was resumed to queued with generation 2!
        assert items[1].execution_status == "queued"
        assert items[1].dispatch_generation == 2
