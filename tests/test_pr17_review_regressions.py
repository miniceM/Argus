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

