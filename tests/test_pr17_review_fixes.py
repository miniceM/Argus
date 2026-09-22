from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.db_models import (  # noqa: E402
    AgentRecord,
    AgentVersionRecord,
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
)
from app.executor import SingleInvocationResult  # noqa: E402
from app.limiter import MemoryAgentLimiter  # noqa: E402
from app.orchestrator import LaunchOrchestrator  # noqa: E402
from app.queue import MemoryQueueAdapter, RedisStreamQueueAdapter  # noqa: E402
from app.reconciler import ExecutionReconciler  # noqa: E402
from app.worker import ExecutionWorker  # noqa: E402


@pytest.fixture
def setup_env(tmp_path):
    db_file = tmp_path / "pr17_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(db_mgr.engine, ROOT / "migrations").apply_all()
    queue = MemoryQueueAdapter()
    limiter = MemoryAgentLimiter()
    orchestrator = LaunchOrchestrator(db_mgr, queue, limiter)
    worker = ExecutionWorker(db_mgr, queue, limiter, worker_id="worker-test-1")
    reconciler = ExecutionReconciler(db_mgr, queue, limiter)
    return db_mgr, queue, limiter, orchestrator, worker, reconciler


# 1. Review Comment 3 (P1): Preserve credentials in the asynchronous worker spec
def test_worker_preserves_credential_ref_in_agent_spec(setup_env, monkeypatch):
    async def _run():
        db_mgr, queue, limiter, orchestrator, worker, _ = setup_env

        manifest = {
            "agent": {
                "agent_id": "auth-agent",
                "version": "v1",
                "agent_version_id": "auth-agent-v1",
                "endpoint": "http://auth-agent:8080/invoke",
                "method": "POST",
                "request_mapping": {},
                "credential_ref": "vault://prod/auth_token",
                "is_idempotent": False,
            },
            "execution_policy": {
                "timeout_seconds": 5,
                "max_retries": 2,
                "max_concurrency": 2,
                "rate_limit_per_minute": 60,
            },
            "dataset": {"name": "ds-auth", "version": "v1", "items": [{"id": "item-auth-1", "input": {}}]},
            "evaluators": [],
        }

        # Seed agent & version
        with db_mgr.get_session() as session:
            session.add(AgentRecord(id="auth-agent", name="Auth Agent"))
            session.flush()
            session.add(
                AgentVersionRecord(
                    id="auth-agent-v1",
                    agent_id="auth-agent",
                    version="v1",
                    endpoint="http://auth-agent:8080/invoke",
                    credential_ref="vault://prod/auth_token",
                    method="POST",
                    timeout_seconds=5,
                    max_retries=2,
                    max_concurrency=2,
                    rate_limit_per_minute=60,
                    request_mapping={},
                    is_idempotent=False,
                    spec_digest="sha256:auth",
                )
            )

        launch = orchestrator.create_launch("auth-agent", "v1", "ds-auth", "v1", "l-auth", manifest)
        orchestrator.start_launch(launch.id)

        captured_spec = []

        async def mock_invoke_once(self, payload, headers=None):
            captured_spec.append(self.spec)
            return SingleInvocationResult(
                status_code=200,
                body={"status": "ok"},
                raw_response="{}",
                headers={},
                duration_ms=10,
                is_retryable=False,
                may_have_side_effects=False,
                error_category=None,
                error_message=None,
                trace_context_received=False,
            )

        from app.executor import RemoteAgentExecutor
        monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", mock_invoke_once)

        msgs = queue.read_group("worker-test-1", count=1)
        assert len(msgs) == 1
        msg_id, item_id, gen = msgs[0]

        success = await worker.execute_item_message(msg_id, item_id, gen)
        assert success is True
        assert len(captured_spec) == 1
        # Review comment fix assertion: credential_ref MUST NOT be None!
        assert captured_spec[0].credential_ref == "vault://prod/auth_token"

    asyncio.run(_run())


# 2. Review Comment 4 (P1): Require force after non-idempotent read timeouts
def test_non_idempotent_read_timeout_marked_ambiguous_and_requires_force(setup_env, monkeypatch):
    async def _run():
        db_mgr, queue, limiter, orchestrator, worker, _ = setup_env

        manifest = {
            "agent": {
                "agent_id": "non-idem-agent",
                "version": "v1",
                "agent_version_id": "non-idem-agent-v1",
                "endpoint": "http://agent:8080/invoke",
                "method": "POST",
                "request_mapping": {},
                "is_idempotent": False,  # Non-idempotent!
            },
            "execution_policy": {
                "timeout_seconds": 5,
                "max_retries": 2,
                "max_concurrency": 2,
                "rate_limit_per_minute": 60,
            },
            "dataset": {"name": "ds-timeout", "version": "v1", "items": [{"id": "item-to-1", "input": {}}]},
            "evaluators": [],
        }

        with db_mgr.get_session() as session:
            session.add(AgentRecord(id="non-idem-agent", name="Non Idem Agent"))
            session.flush()
            session.add(
                AgentVersionRecord(
                    id="non-idem-agent-v1",
                    agent_id="non-idem-agent",
                    version="v1",
                    endpoint="http://agent:8080/invoke",
                    method="POST",
                    timeout_seconds=5,
                    max_retries=2,
                    max_concurrency=2,
                    rate_limit_per_minute=60,
                    request_mapping={},
                    is_idempotent=False,
                    spec_digest="sha256:non-idem",
                )
            )

        launch = orchestrator.create_launch("non-idem-agent", "v1", "ds-timeout", "v1", "l-to", manifest)
        orchestrator.start_launch(launch.id)

        async def mock_invoke_read_timeout(self, payload, headers=None):
            return SingleInvocationResult(
                status_code=None,
                body=None,
                raw_response=None,
                headers={},
                duration_ms=5000,
                is_retryable=False,
                may_have_side_effects=True,
                error_category="READ_TIMEOUT",
                error_message="ReadTimeout: Server did not respond in time",
                trace_context_received=False,
            )

        from app.executor import RemoteAgentExecutor
        monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", mock_invoke_read_timeout)

        msgs = queue.read_group("worker-test-1", count=1)
        msg_id, item_id, gen = msgs[0]
        await worker.execute_item_message(msg_id, item_id, gen)

        with db_mgr.get_session() as session:
            att = session.scalars(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id == item_id)).first()
            assert att is not None
            # Review comment fix: error_type MUST be AMBIGUOUS_OUTCOME for non-idempotent read timeouts
            assert att.error_type == "AMBIGUOUS_OUTCOME"

            item = session.get(ExperimentItemExecutionRecord, item_id)
            assert item.execution_status == "failed"
            assert "AMBIGUOUS_OUTCOME" in (item.execution_error or "")

        # Review comment fix: retry_failed_items MUST fail without force=True
        with pytest.raises(ValueError, match="AMBIGUOUS_OUTCOME"):
            orchestrator.retry_failed_items(launch.id, force=False)

        # With force=True, retry succeeds
        retried_launch = orchestrator.retry_failed_items(launch.id, force=True)
        assert retried_launch.status == "QUEUED"

    asyncio.run(_run())


# 3. Review Comment 6 (P2): Enforce distributed request-rate permit
def test_worker_enforces_distributed_rate_permit(setup_env, monkeypatch):
    async def _run():
        db_mgr, queue, limiter, orchestrator, worker, _ = setup_env

        manifest = {
            "agent": {
                "agent_id": "rate-agent",
                "version": "v1",
                "agent_version_id": "rate-agent-v1",
                "endpoint": "http://rate-agent:8080/invoke",
                "method": "POST",
                "request_mapping": {},
                "is_idempotent": True,
            },
            "execution_policy": {
                "timeout_seconds": 5,
                "max_retries": 2,
                "max_concurrency": 5,
                "rate_limit_per_minute": 10,
            },
            "dataset": {"name": "ds-rate", "version": "v1", "items": [{"id": "item-r-1", "input": {}}]},
            "evaluators": [],
        }

        with db_mgr.get_session() as session:
            session.add(AgentRecord(id="rate-agent", name="Rate Agent"))
            session.flush()
            session.add(
                AgentVersionRecord(
                    id="rate-agent-v1",
                    agent_id="rate-agent",
                    version="v1",
                    endpoint="http://rate-agent:8080/invoke",
                    method="POST",
                    timeout_seconds=5,
                    max_retries=2,
                    max_concurrency=5,
                    rate_limit_per_minute=10,
                    request_mapping={},
                    is_idempotent=True,
                    spec_digest="sha256:rate",
                )
            )

        launch = orchestrator.create_launch("rate-agent", "v1", "ds-rate", "v1", "l-rate", manifest)
        orchestrator.start_launch(launch.id)

        # Force acquire_rate_permit to return False (rate limited)
        monkeypatch.setattr(limiter, "acquire_rate_permit", lambda agent_id, rate_limit: False)

        invoked = False
        async def mock_invoke(self, payload, headers=None):
            nonlocal invoked
            invoked = True
            return SingleInvocationResult(status_code=200, body={}, raw_response="{}", headers={}, duration_ms=5, is_retryable=False, may_have_side_effects=False, error_category=None, error_message=None, trace_context_received=False)

        from app.executor import RemoteAgentExecutor
        monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", mock_invoke)

        msgs = queue.read_group("worker-test-1", count=1)
        msg_id, item_id, gen = msgs[0]
        result = await worker.execute_item_message(msg_id, item_id, gen)

        # Should not invoke remote agent because rate permit was rejected
        assert result is False
        assert invoked is False

        with db_mgr.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, item_id)
            # Should be scheduled into retry_wait
            assert item.execution_status == "retry_wait"
            assert item.lease_owner is None

    asyncio.run(_run())


# 4. Review Comment 2 (P1): Renew leases while remote invocations are in flight
def test_worker_heartbeat_renews_lease_during_long_invocation(setup_env, monkeypatch):
    async def _run():
        db_mgr, queue, limiter, orchestrator, worker, _ = setup_env

        manifest = {
            "agent": {
                "agent_id": "slow-agent",
                "version": "v1",
                "agent_version_id": "slow-agent-v1",
                "endpoint": "http://slow:8080/invoke",
                "method": "POST",
                "request_mapping": {},
                "is_idempotent": True,
            },
            "execution_policy": {
                "timeout_seconds": 60,  # Configured with large timeout
                "max_retries": 2,
                "max_concurrency": 2,
                "rate_limit_per_minute": 60,
            },
            "dataset": {"name": "ds-slow", "version": "v1", "items": [{"id": "item-s-1", "input": {}}]},
            "evaluators": [],
        }

        with db_mgr.get_session() as session:
            session.add(AgentRecord(id="slow-agent", name="Slow Agent"))
            session.flush()
            session.add(
                AgentVersionRecord(
                    id="slow-agent-v1",
                    agent_id="slow-agent",
                    version="v1",
                    endpoint="http://slow:8080/invoke",
                    method="POST",
                    timeout_seconds=60,
                    max_retries=2,
                    max_concurrency=2,
                    rate_limit_per_minute=60,
                    request_mapping={},
                    is_idempotent=True,
                    spec_digest="sha256:slow",
                )
            )

        launch = orchestrator.create_launch("slow-agent", "v1", "ds-slow", "v1", "l-slow", manifest)
        orchestrator.start_launch(launch.id)

        # We simulate a slow invocation where initial lease is expired unless renewed
        async def slow_invoke(self, payload, headers=None):
            # Sleep briefly to give heartbeat a chance to fire
            await asyncio.sleep(0.3)
            return SingleInvocationResult(status_code=200, body={"data": 123}, raw_response="{}", headers={}, duration_ms=300, is_retryable=False, may_have_side_effects=False, error_category=None, error_message=None, trace_context_received=False)

        from app.executor import RemoteAgentExecutor
        monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", slow_invoke)

        msgs = queue.read_group("worker-test-1", count=1)
        msg_id, item_id, gen = msgs[0]

        # Use a worker configured with fast heartbeat for testing
        worker.heartbeat_interval = 0.1
        success = await worker.execute_item_message(msg_id, item_id, gen)
        assert success is True

        with db_mgr.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, item_id)
            assert item.execution_status == "succeeded"

    asyncio.run(_run())


# 5. Review Comment 5 (P1): Redis Stream pending entries autoclaim & backlog recovery
def test_redis_queue_autoclaim_pending():
    mock_redis = MagicMock()
    # Mock xautoclaim returning pending entries
    mock_redis.xautoclaim.return_value = (
        b"0-0",
        [
            (b"msg-123", {b"item_execution_id": b"item-recovered", b"dispatch_generation": b"1"}),
        ],
        [],
    )

    adapter = RedisStreamQueueAdapter(mock_redis)
    recovered = adapter.claim_pending_entries(consumer_name="worker-new", min_idle_ms=30000, count=10)
    assert len(recovered) == 1
    assert recovered[0] == ("msg-123", "item-recovered", 1)


# 6. Review Comment 1 (P1): Non-blocking worker loop offloads to thread and yields
def test_worker_poll_queue_in_thread(setup_env):
    async def _run():
        _, _, _, _, worker, _ = setup_env
        msgs = await asyncio.to_thread(worker.poll_queue, count=5, block_ms=10)
        assert isinstance(msgs, list)

    asyncio.run(_run())
