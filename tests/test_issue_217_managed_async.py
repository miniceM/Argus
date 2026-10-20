"""#217：托管评分异步等待、可靠恢复与执行/评测解耦。"""

from __future__ import annotations

import asyncio
import copy
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/eval-runner"))

from app.db_models import EvaluationResultRecord  # noqa: E402
from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.managed_score_recovery import (  # noqa: E402
    WORK_TYPE_MANAGED,
    recover_managed_scores,
    submit_managed_collection,
)
from test_issue_216_managed_scores import Remote, score  # noqa: E402


def _item(db, item_id):
    with db.get_session() as session:
        session.expire_all()
        return session.get(Item, item_id)


def _managed_manifest():
    from app.managed_evaluation import freeze_managed_evaluation
    from app.runner_identity import current_runner_identity

    contract = {
        "project_id": "project-215",
        "environment": "production",
        "target": {"name": "remote-agent-http", "type": "TOOL"},
        "scores": [
            {
                "name": "correctness",
                "data_type": "boolean",
                "required": True,
                "rule_id": "rule-215",
                "evaluator_id": "judge-215",
            }
        ],
    }
    rules = [
        {
            "evaluator_id": "correctness",
            "result_type": "boolean",
            "operator": "==",
            "expected_value": True,
            "required": True,
        }
    ]
    frozen_contract, policy = freeze_managed_evaluation(
        contract, environment="production", policy_rules=rules
    )
    return {
        "schema_version": "1.0",
        "agent": {
            "agent_id": "test-agent",
            "version": "v1",
            "agent_version_id": "test-agent-v1",
            "endpoint": "http://localhost/invoke",
            "method": "POST",
            "request_mapping": {},
            "is_idempotent": False,
            "environment": "production",
        },
        "execution_policy": {
            "timeout_seconds": 5,
            "max_retries": 0,
            "max_concurrency": 1,
            "rate_limit_per_minute": 60,
        },
        "dataset": {
            "items": [{"id": "0", "input": {}, "expected_output": {"answer": "ok"}}]
        },
        "evaluators": [],
        "quality_policy": policy.to_payload(),
        "evaluation_source": "langfuse_managed",
        "managed_evaluation": frozen_contract,
        "measurement_digest": frozen_contract["acceptance_digest"],
        "comparison": {"environment": "production"},
        "runner": current_runner_identity().model_dump(),
    }


def _run_managed_http(setup_runtime, manifest, body):
    from app.executor import SingleInvocationResult

    db_mgr, queue, limiter, orchestrator, worker, _rec = setup_runtime
    launch = orchestrator.create_launch(
        "test-agent", "v1", "ds", "v1", f"m217-{id(manifest)}-{datetime.now(UTC).microsecond}", manifest
    )
    orchestrator.start_launch(launch.id)
    msg = queue.read_group("m217", count=1)[0]

    async def _run():
        with patch("app.worker.get_langfuse_client_safe", return_value=None):
            with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock:
                mock.return_value = SingleInvocationResult(
                    status_code=200, body=body, raw_response="{}",
                    headers={}, duration_ms=3, trace_context_received=True,
                    error_category=None, error_message=None,
                    is_retryable=False, may_have_side_effects=False,
                )
                ok = await worker.execute_item_message(*msg)
                return ok, mock.await_count, launch.id, msg[1]

    ok, calls, launch_id, item_id = asyncio.run(_run())
    return db_mgr, queue, limiter, orchestrator, worker, launch_id, item_id, calls, ok


def _observation_for(db_mgr, item_id):
    from app.db_models import ExecutionCheckpointRecord
    from sqlalchemy import select

    with db_mgr.get_session() as session:
        row = session.get(Item, item_id)
        trace_id, obs_id = row.trace_id, row.observation_id
        if not trace_id:
            trace_id = f"trace-{item_id[:8]}"
            row.trace_id = trace_id
        if not obs_id:
            obs_id = f"obs-{item_id[:8]}"
            row.observation_id = obs_id
        cp = session.scalar(
            select(ExecutionCheckpointRecord).where(
                ExecutionCheckpointRecord.item_execution_id == item_id
            )
        )
        assert cp is not None
        cp.trace_id = trace_id
        cp.observation_id = obs_id
        session.commit()
        return (
            {
                "id": obs_id, "traceId": trace_id, "projectId": "project-215",
                "name": "remote-agent-http", "type": "TOOL", "environment": "production",
                "startTime": datetime.now(UTC).isoformat(),
                "input": {"agent": "test-agent", "request": cp.input_payload},
                "output": cp.agent_output,
            },
            trace_id,
            obs_id,
        )


def _good_score(trace_id, obs_id, **patch):
    row = score(
        id="s217", projectId="project-215", name="correctness",
        dataType="BOOLEAN", value=True, source="EVAL", environment="production",
        timestamp=datetime.now(UTC).isoformat(),
        subject={"kind": "observation", "id": obs_id, "traceId": trace_id},
        metadata={
            "evaluation_rule_id": "rule-215", "evaluator_id": "judge-215",
            "job_execution_id": "job217", "target_trace_id": trace_id,
            "target_observation_id": obs_id,
        },
    )
    row.update(patch)
    return row


def _make_collection_due(db_mgr, item_id):
    """显式推进持久化退避时间；不绕过 Worker 的 available_at 门禁。"""
    from datetime import timedelta

    with db_mgr.get_session() as session:
        session.get(Item, item_id).available_at = datetime.now(UTC) - timedelta(seconds=1)
    return _item(db_mgr, item_id)


def test_http_success_decouple_pending_and_enqueue_managed(setup_runtime):
    manifest = _managed_manifest()
    db_mgr, queue, _, _, _, _, item_id, calls, ok = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    assert ok and calls == 1
    item = _item(db_mgr, item_id)
    assert item.execution_status == "succeeded"
    assert item.eval_status == "pending"
    assert item.quality_conclusion == "unknown"
    assert item.evaluation_status == "evaluating"
    assert item.evaluation_generation >= 1
    pending = queue.read_group("probe217", count=10)
    assert any(w == WORK_TYPE_MANAGED for _, _, _, w in pending)


def test_delayed_score_converges_without_recalling_agent(setup_runtime, monkeypatch):
    manifest = _managed_manifest()
    db_mgr, queue, _, _, _, _, item_id, calls, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    assert calls == 1
    item = _item(db_mgr, item_id)
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, []),
    )
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert outcome["claimed"] is True and outcome["finalized"] is False
    assert outcome["reason"] in ("SCORE_MISSING", "RETRY_SCHEDULED")
    retried = _item(db_mgr, item_id)
    assert retried.eval_status == "pending" and retried.quality_conclusion == "unknown"
    assert retried.evaluation_status == "evaluating"
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        assert not list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )
    good = _good_score(trace_id, obs_id, id="late217")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [good]),
    )
    item2 = _make_collection_due(db_mgr, item_id)
    outcome2 = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item2.evaluation_generation, worker_id="w1",
    )
    assert outcome2["finalized"] is True
    done = _item(db_mgr, item_id)
    assert done.eval_status == "succeeded"
    assert done.evaluation_status == "recovered"
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        rows = list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )
        assert len(rows) == 1 and rows[0].value is True


def test_rate_limit_retry_bounded_and_unknown(setup_runtime, monkeypatch):
    from app.langfuse_scores_client import ScoresReadError

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, _, _ = _observation_for(db_mgr, item_id)

    class Limited(Remote):
        def scores_for(self, *a):
            raise ScoresReadError("RATE_LIMITED", retry_after="1")

    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Limited(obs, [])
    )
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "2")
    item = _item(db_mgr, item_id)
    first = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert first["finalized"] is False
    second_item = _make_collection_due(db_mgr, item_id)
    second = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=second_item.evaluation_generation, worker_id="w1",
    )
    assert second["finalized"] is True and second.get("exhausted") is True
    final = _item(db_mgr, item_id)
    assert final.quality_conclusion == "unknown"
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        assert not list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )


def test_concurrent_workers_single_adoption(setup_runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    good = _good_score(trace_id, obs_id, id="race217")
    item = _item(db_mgr, item_id)
    gen = item.evaluation_generation
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [copy.deepcopy(good)]),
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(
            pool.map(
                lambda _: recover_managed_scores(
                    db_mgr, item_id=item_id, evaluation_generation=gen, worker_id="w"
                ),
                range(4),
            )
        )
    assert sum(1 for o in outcomes if o.get("finalized")) == 1
    assert sum(1 for o in outcomes if o.get("claimed") is False) >= 1
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        assert len(list(session.scalars(select(EvaluationResultRecord).where(
            EvaluationResultRecord.item_execution_id == item_id)))) == 1


def test_late_generation_cannot_overwrite(setup_runtime, monkeypatch):
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    item = _item(db_mgr, item_id)
    old_gen = item.evaluation_generation
    good = _good_score(trace_id, obs_id, id="new217", value=True)
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [copy.deepcopy(good)]),
    )
    assert recover_managed_scores(
        db_mgr, item_id=item_id, evaluation_generation=old_gen, worker_id="w1"
    )["finalized"] is True
    late = _good_score(trace_id, obs_id, id="late217", value=False)
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Remote(obs, [late])
    )
    late_outcome = recover_managed_scores(
        db_mgr, item_id=item_id, evaluation_generation=old_gen, worker_id="w2"
    )
    assert late_outcome["finalized"] is False
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        rows = list(session.scalars(select(EvaluationResultRecord).where(
            EvaluationResultRecord.item_execution_id == item_id)))
        assert len(rows) == 1 and rows[0].value is True


def test_managed_retry_never_calls_agent_and_releases_permit(setup_runtime, monkeypatch):
    manifest = _managed_manifest()
    db_mgr, _, limiter, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    calls = {"agent": 0, "permit": 0}
    orig_acquire = limiter.acquire_concurrency_permit
    monkeypatch.setattr(
        limiter, "acquire_concurrency_permit",
        lambda *a, **k: (calls.__setitem__("permit", calls["permit"] + 1), orig_acquire(*a, **k))[1],
    )
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock:
        async def _count(*a, **k):
            calls["agent"] += 1
            raise AssertionError("must not call agent")
        mock.side_effect = _count
        item = _item(db_mgr, item_id)
        obs, _, _ = _observation_for(db_mgr, item_id)
        monkeypatch.setattr(
            "app.managed_score_recovery.get_managed_score_client", lambda: Remote(obs, [])
        )
        recover_managed_scores(
            db_mgr, item_id=item_id,
            evaluation_generation=item.evaluation_generation, worker_id="w1",
        )
    assert calls == {"agent": 0, "permit": 0}


def test_cancel_discards_late_collection(setup_runtime, monkeypatch):
    manifest = _managed_manifest()
    db_mgr, _, _, orchestrator, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    orchestrator.cancel_launch(launch_id)
    good = _good_score(trace_id, obs_id, id="cancel217")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [good]),
    )
    item = _item(db_mgr, item_id)
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert outcome["finalized"] is True
    assert outcome.get("reason") == "EVALUATION_CANCELLED"
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        assert not list(session.scalars(select(EvaluationResultRecord).where(
            EvaluationResultRecord.item_execution_id == item_id)))


def test_fault_injection_5xx_timeout_auth(setup_runtime, monkeypatch):
    import httpx
    from app.langfuse_managed_scores import ScoresV3Client
    from app.langfuse_scores_client import ScoresReadError

    def _client(handler):
        return ScoresV3Client(
            "https://langfuse.example", "public", "secret",
            transport=httpx.MockTransport(handler),
        )

    def _raises(status):
        def _handler(request):
            return httpx.Response(status, text="upstream")
        with _client(_handler) as remote:
            with __import__("pytest").raises(ScoresReadError) as exc:
                remote.scores_for("t", "o", "s")
            return exc.value.code

    assert _raises(500) == "UPSTREAM_UNAVAILABLE"
    assert _raises(429) == "RATE_LIMITED"
    assert _raises(401) == "AUTHENTICATION_FAILED"

    def _timeout(request):
        raise httpx.ReadTimeout("t", request=request)

    with _client(_timeout) as remote:
        with __import__("pytest").raises(ScoresReadError) as exc:
            remote.scores_for("t", "o", "s")
        assert exc.value.code == "TIMEOUT"

    # 401 终态：不重试、UNKNOWN、无分数
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, _, _ = _observation_for(db_mgr, item_id)

    class Denied(Remote):
        def scores_for(self, *a):
            raise ScoresReadError("AUTHENTICATION_FAILED")

    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Denied(obs, [])
    )
    item = _item(db_mgr, item_id)
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert outcome["finalized"] is True and outcome["reason"] == "AUTHENTICATION_FAILED"
    done = _item(db_mgr, item_id)
    assert done.eval_status == "failed" and done.quality_conclusion == "unknown"
    assert done.evaluation_status == "failed"


def test_reconciler_enqueues_due_and_skips_future_or_cancelled(setup_runtime):
    from datetime import timedelta

    manifest = _managed_manifest()
    db_mgr, queue, _, orchestrator, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    _observation_for(db_mgr, item_id)
    queue.read_group("drain217", count=100)
    # 缺分首轮后进入退避（available_at 未来）：到期前不入队
    item = _item(db_mgr, item_id)
    import app.managed_score_recovery as rec_mod
    from app.managed_score_recovery import get_managed_score_client  # noqa: F401
    from test_issue_216_managed_scores import Remote as _R  # noqa: F401

    obs, _, _ = _observation_for(db_mgr, item_id)
    import pytest as _pt

    _mp = _pt.MonkeyPatch()
    _mp.setattr(rec_mod, "get_managed_score_client", lambda: Remote(obs, []))
    try:
        out = recover_managed_scores(
            db_mgr, item_id=item_id,
            evaluation_generation=item.evaluation_generation, worker_id="w1",
        )
    finally:
        _mp.undo()
    assert out["reason"] == "RETRY_SCHEDULED"
    waiting = _item(db_mgr, item_id)
    exp = waiting.available_at
    assert exp is not None
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    assert exp > datetime.now(UTC)
    assert orchestrator is not None
    from app.reconciler import ExecutionReconciler

    rec = ExecutionReconciler(db_mgr, queue, setup_runtime[2])
    assert rec.reconcile_managed_collections() == 0
    # 到期后入队 MANAGED
    with db_mgr.get_session() as session:
        row = session.get(Item, item_id)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert rec.reconcile_managed_collections() == 1
    msgs = queue.read_group("due217", count=10)
    assert any(m[1] == item_id and m[3] == WORK_TYPE_MANAGED for m in msgs)
    # 取消后永不入队
    orchestrator.cancel_launch(launch_id)
    with db_mgr.get_session() as session:
        row = session.get(Item, item_id)
        row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        row.evaluation_lease_token = None
        session.commit()
    queue.read_group("drain2", count=100)
    assert rec.reconcile_managed_collections() == 0


def test_managed_http_acks_invocation_message(setup_runtime):
    # 执行消息已终态必须确认，否则 Redis 会重投并再次调用 Agent。
    manifest = _managed_manifest()
    _db, queue, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    rest = [m for m in queue._queue if m[0] not in queue._acked]
    assert len(rest) == 1
    assert rest[0][1] == item_id and rest[0][3] == WORK_TYPE_MANAGED


def test_lease_expiry_crash_recovers_and_converges(setup_runtime, monkeypatch):
    from datetime import timedelta

    from app.db_models import EvaluationAttemptRecord

    manifest = _managed_manifest()
    db_mgr, queue, _, _, worker, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    item = _item(db_mgr, item_id)
    gen = item.evaluation_generation
    queue.read_group("drain-crash", count=100)
    # 模拟崩溃：认领后失去心跳，租约过期，running attempt 残留。
    with db_mgr.get_session() as session:
        row = session.get(Item, item_id)
        row.evaluation_lease_owner = "crashed"
        row.evaluation_lease_token = "dead-token"
        row.evaluation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        session.add(
            EvaluationAttemptRecord(
                id=f"ea_crash{item_id[:8].replace('-', '')}",
                item_execution_id=item_id,
                launch_id=launch_id,
                evaluation_generation=gen,
                status="running",
                target_bindings=["correctness"],
                reused_output_digest="sha256:crash",
                worker_id="crashed",
                started_at=datetime.now(UTC),
            )
        )
        session.commit()
    from app.reconciler import ExecutionReconciler

    rec = ExecutionReconciler(db_mgr, queue, setup_runtime[2])
    assert rec.reconcile_expired_evaluation_leases() == 1
    resumed = _make_collection_due(db_mgr, item_id)
    assert resumed.evaluation_generation > item.evaluation_generation
    assert rec.reconcile_managed_collections() == 1
    msgs = queue.read_group("crash217", count=10)
    managed = [m for m in msgs if m[1] == item_id and m[3] == WORK_TYPE_MANAGED]
    assert managed
    # 崩溃消耗旧代预算，新代接管；旧线程不可复活，新分到达后收敛，不调 Agent。
    good = _good_score(trace_id, obs_id, id="crash217")
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client",
        lambda: Remote(obs, [good]),
    )
    with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock:
        mock.side_effect = AssertionError("must not call agent")
        ok = asyncio.run(
            worker.execute_managed_message(managed[0][0], managed[0][1], managed[0][2])
        )
    assert ok is True
    done = _item(db_mgr, item_id)
    assert done.eval_status == "succeeded" and done.evaluation_status == "recovered"
    with db_mgr.get_session() as session:
        from sqlalchemy import select

        rows = list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )
        assert len(rows) == 1 and rows[0].value is True


def test_client_unconfigured_terminal_unknown(setup_runtime, monkeypatch):
    # 未配置 Langfuse 客户端：终态失败、质量 UNKNOWN，不伪造分数、不重试。
    manifest = _managed_manifest()
    db_mgr, _, _, _, _, _, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: None
    )
    item = _item(db_mgr, item_id)
    outcome = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert outcome["finalized"] is True
    assert outcome.get("reason") in ("CLIENT_NOT_CONFIGURED", "CLIENT_CONFIG_INVALID")
    done = _item(db_mgr, item_id)
    assert done.eval_status == "failed" and done.quality_conclusion == "unknown"
    assert done.evaluation_status == "failed"


def test_submit_guards_and_idempotency(setup_runtime):
    from app.db_models import ExperimentLaunchRecord

    manifest = _managed_manifest()
    db_mgr, queue, _, _, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    queued = len(queue._queue)
    # 已在 evaluating：幂等返回，不重复入队。
    dup = submit_managed_collection(db_mgr, queue, item_id=item_id, worker_id="w")
    assert dup["already_running"] is True
    assert len(queue._queue) == queued
    # 非 Managed 来源：拒绝提交。
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.manifest = {"version": "v2"}
        session.commit()
    assert submit_managed_collection(db_mgr, queue, item_id=item_id, worker_id="w") is None
    assert len(queue._queue) == queued


def test_manual_retry_resubmits_managed_pending(setup_runtime, monkeypatch):
    manifest = _managed_manifest()
    db_mgr, queue, _, orchestrator, _, launch_id, item_id, _, _ = _run_managed_http(
        setup_runtime, manifest, {"answer": "ok"}
    )
    _observation_for(db_mgr, item_id)
    queue.read_group("drain3", count=100)
    obs, trace_id, obs_id = _observation_for(db_mgr, item_id)
    # 首轮缺分进入退避；手动重提应直接入队新代际
    monkeypatch.setattr(
        "app.managed_score_recovery.get_managed_score_client", lambda: Remote(obs, [])
    )
    item = _item(db_mgr, item_id)
    first = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=item.evaluation_generation, worker_id="w1",
    )
    assert first["reason"] == "RETRY_SCHEDULED"
    before = _item(db_mgr, item_id).evaluation_generation
    # 将退避提前到期后手动重提（幂等：evaluating 中则已在运行）
    result = orchestrator.retry_failed_evaluations(launch_id)
    assert item_id in result["already_running"]
    assert _item(db_mgr, item_id).evaluation_generation == before
    # 终态失败后可手动重提新一轮
    monkeypatch.setenv("ARGUS_MANAGED_SCORE_MAX_ATTEMPTS", "1")
    # 构造终态：先将当前代际耗尽
    cur = _make_collection_due(db_mgr, item_id)
    last = recover_managed_scores(
        db_mgr, item_id=item_id,
        evaluation_generation=cur.evaluation_generation, worker_id="w1",
    )
    assert last["finalized"] is True
    assert _item(db_mgr, item_id).evaluation_status == "failed"
    result2 = orchestrator.retry_failed_evaluations(launch_id)
    assert item_id in result2["submitted"]
    msgs = queue.read_group("manual217", count=20)
    assert any(m[1] == item_id and m[3] == WORK_TYPE_MANAGED for m in msgs)
