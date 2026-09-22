import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402
from app.db_models import LangfuseSyncTaskRecord as Task  # noqa: E402
from app.langfuse_sync import LangfuseOutboxSyncer, aggregate_launch_sync_status  # noqa: E402
from test_pr17_review_regressions import create_launch_helper  # noqa: E402


def prepare(env, status="COMPLETED", task_status="PENDING", source="seed"):
    db = env[0]
    lid, msgs = create_launch_helper(env, count=2 if status == "PARTIAL_FAILED" else 1)
    iid = msgs[0][1]
    with db.get_session() as s:
        s.get(Launch, lid).status = status
        if status == "PARTIAL_FAILED":
            s.get(Item, msgs[1][1]).execution_status = "failed"
        it = s.get(Item, iid)
        it.execution_status = "succeeded"
        it.trace_id = "a" * 32
        s.add(
            Task(
                id="audit",
                launch_id=lid,
                item_id=iid,
                dataset_item_id="0",
                dispatch_generation=it.dispatch_generation,
                task_type="FULL_EVAL_SYNC",
                trace_id=it.trace_id,
                dataset_run_name="audit-run",
                dataset_version="2026-01-01T00:00:00Z",
                scores_payload={"_dataset_source": source, "quality": 1},
                status=task_status,
                next_retry_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    return db, lid


# 1. PARTIAL_FAILED 终态能正常完成同步
def test_partial_failed_finishes_sync(setup_runtime):
    db, lid = prepare(setup_runtime, status="PARTIAL_FAILED", task_status="SYNCED")
    assert aggregate_launch_sync_status(db, lid) == "SYNCED"


# 2. 真实 Dataset Run ID 持久化至 Launch
def test_remote_run_id_persisted(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    lf = MagicMock()
    lf.api.dataset_run_items.create.return_value.dataset_run_id = "remote-run-id"
    assert LangfuseOutboxSyncer(db, lf).process_batch() == 1
    with db.get_session() as s:
        assert s.get(Launch, lid).langfuse_experiment_id == "remote-run-id"


# 3. 关联调用后租约过期，立即停止发送 scores
def test_expired_link_stops_scores(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    lf = MagicMock()

    def expire(**kwargs):
        with db.get_session() as s:
            s.get(Task, "audit").lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        return MagicMock(dataset_run_id="remote")

    lf.api.dataset_run_items.create.side_effect = expire
    sync = LangfuseOutboxSyncer(db, lf)
    assert sync.process_batch() == 0
    lf.api.scores.create.assert_not_called()


# 4. 未配置客户端时标记为 NOT_APPLICABLE
def test_unconfigured_is_not_applicable(setup_runtime):
    db, lid = prepare(setup_runtime)
    LangfuseOutboxSyncer(db, None).process_batch()
    assert aggregate_launch_sync_status(db, lid) == "NOT_APPLICABLE"


# 5. 客户端暂时初始化失败（抛异常）进入重试退避，绝不降级为 SKIPPED
def test_client_init_temporary_failure_retries_not_skipped(setup_runtime):
    db, lid = prepare(setup_runtime)

    def failing_provider():
        raise ConnectionError("Langfuse temporary network glitch")

    syncer = LangfuseOutboxSyncer(db, failing_provider)
    processed = syncer.process_batch()
    assert processed == 0

    with db.get_session() as s:
        t = s.get(Task, "audit")
        assert t.status == "PENDING"
        assert t.last_error and "temporary network glitch" in t.last_error
        assert t.attempts == 1


# 6. 旧代次 Run ID 不覆盖当前 Launch 关联；多个不一致 Run ID 报错
def test_old_generation_run_id_does_not_override_launch(setup_runtime):
    db = setup_runtime[0]
    lid, msgs = create_launch_helper(setup_runtime, count=1)
    iid = msgs[0][1]

    with db.get_session() as s:
        launch = s.get(Launch, lid)
        launch.status = "COMPLETED"
        it = s.get(Item, iid)
        it.execution_status = "succeeded"
        it.dispatch_generation = 2  # Current effective generation is 2

        # Old generation 1 task with old run id
        s.add(
            Task(
                id="task-gen1",
                launch_id=lid,
                item_id=iid,
                dataset_item_id="0",
                dispatch_generation=1,
                task_type="FULL_EVAL_SYNC",
                trace_id="trace-gen1",
                dataset_run_name="audit-run",
                dataset_version="2026-01-01T00:00:00Z",
                scores_payload={"_dataset_run_id": "old-run-id-gen1"},
                status="SYNCED",
            )
        )
        # Current generation 2 task with new run id
        s.add(
            Task(
                id="task-gen2",
                launch_id=lid,
                item_id=iid,
                dataset_item_id="0",
                dispatch_generation=2,
                task_type="FULL_EVAL_SYNC",
                trace_id="trace-gen2",
                dataset_run_name="audit-run",
                dataset_version="2026-01-01T00:00:00Z",
                scores_payload={"_dataset_run_id": "new-run-id-gen2"},
                status="SYNCED",
            )
        )

    res = aggregate_launch_sync_status(db, lid)
    assert res == "SYNCED"

    with db.get_session() as s:
        launch = s.get(Launch, lid)
        assert launch.langfuse_experiment_id == "new-run-id-gen2"


# 7. 领取与失败写回只对 attempts 计数一次
def test_claim_and_failure_count_attempts_only_once(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    lf = MagicMock()
    lf.api.dataset_run_items.create.side_effect = RuntimeError("Sync network failure")

    syncer = LangfuseOutboxSyncer(db, lf)
    processed = syncer.process_batch()
    assert processed == 0

    with db.get_session() as s:
        t = s.get(Task, "audit")
        assert t.status == "PENDING"
        # Claimed once -> attempts = 1, failure write-back must NOT increment to 2
        assert t.attempts == 1


# 8. 第 max_attempts 次领取成功依然能更新为 SYNCED
def test_max_attempt_success_still_transitions_to_synced(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    with db.get_session() as s:
        t = s.get(Task, "audit")
        t.attempts = 4  # Next claim will be the 5th attempt (max_attempts = 5)

    lf = MagicMock()
    lf.api.dataset_run_items.create.return_value.dataset_run_id = "run-success-at-limit"

    syncer = LangfuseOutboxSyncer(db, lf, max_attempts=5)
    processed = syncer.process_batch()
    assert processed == 1

    with db.get_session() as s:
        t = s.get(Task, "audit")
        assert t.status == "SYNCED"
        assert t.attempts == 5


# 9. 预期任务未全部物化前不得提前收敛
def test_missing_tasks_prevent_premature_sync_convergence(setup_runtime):
    db = setup_runtime[0]
    lid, msgs = create_launch_helper(setup_runtime, count=2)
    with db.get_session() as s:
        launch = s.get(Launch, lid)
        launch.status = "COMPLETED"
        # Item 0 has a task that is SKIPPED
        s.add(
            Task(
                id="task-0",
                launch_id=lid,
                item_id=msgs[0][1],
                dataset_item_id="0",
                dispatch_generation=1,
                task_type="FULL_EVAL_SYNC",
                trace_id="trace-0",
                dataset_run_name="run",
                dataset_version="v1",
                scores_payload={},
                status="SKIPPED",
            )
        )
        # Item 1 has no task generated yet!

    # Must stay SYNCING because item 1 task is missing
    res = aggregate_launch_sync_status(db, lid)
    assert res == "SYNCING"


# 10. Worker 心跳使用 asyncio.to_thread 且在 lease_lost 时熔断
def test_heartbeat_loop_runs_in_thread_and_respects_lease_lost(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime, count=1)
    msg = msgs[0]

    async def _test():
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
            # Mock renew_lease in thread to return False (lease lost)
            mock_to_thread.return_value = False
            w.heartbeat_interval_seconds = 0.05
            with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock_inv:
                async def _slow_inv(*args, **kwargs):
                    await asyncio.sleep(0.1)
                    from app.executor import SingleInvocationResult

                    return SingleInvocationResult(
                        status_code=200,
                        body={"answer": "ok"},
                        raw_response="{}",
                        headers={},
                        duration_ms=100,
                        trace_context_received=True,
                        error_category=None,
                        error_message=None,
                        is_retryable=False,
                        may_have_side_effects=False,
                    )

                mock_inv.side_effect = _slow_inv
                # Execute message: heartbeat will detect lease lost
                res = await w.execute_item_message(*msg)
                assert res is False
                assert mock_to_thread.called

    asyncio.run(_test())


# 11. 父 Observation 扩围且在 HTTP 调用后立即释放并发许可
def test_parent_observation_scope_covers_evaluator(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
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
        "dataset": {"items": [{"id": "0", "input": {}}]},
        "evaluators": [{"id": "intent_match", "scope": "item"}],
    }
    launch = orch.create_launch("test-agent", "v1", "ds", "v1", "review-eval", manifest)
    orch.start_launch(launch.id)
    msgs = q.read_group("review-eval", count=1)
    msg = msgs[0]

    parent_obs_active_during_eval = False
    permit_released_before_eval = False

    mock_parent_obs = MagicMock()

    class MockObservationContext:
        def __init__(self, obs):
            self.obs = obs

        def __enter__(self):
            self.obs.is_active = True
            return self.obs

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.obs.is_active = False

    mock_lf = MagicMock()

    def mock_start_obs(*args, **kwargs):
        name = kwargs.get("name", "")
        if "eval_item_execution" in name:
            return MockObservationContext(mock_parent_obs)
        return MockObservationContext(MagicMock())

    mock_lf.start_as_current_observation.side_effect = mock_start_obs
    mock_lf.get_current_trace_id.return_value = "trace-123"
    mock_lf.get_current_observation_id.return_value = "obs-123"

    async def _test():
        nonlocal parent_obs_active_during_eval, permit_released_before_eval
        with patch("app.worker.get_langfuse_client_safe", return_value=mock_lf):
            with patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as mock_inv:
                from app.executor import SingleInvocationResult

                mock_inv.return_value = SingleInvocationResult(
                    status_code=200,
                    body={"answer": "ok"},
                    raw_response='{"answer": "ok"}',
                    headers={},
                    duration_ms=10,
                    trace_context_received=True,
                    error_category=None,
                    error_message=None,
                    is_retryable=False,
                    may_have_side_effects=False,
                )

                # Spy on limiter.release_concurrency_permit
                orig_release = lim.release_concurrency_permit

                def spy_release(*args, **kwargs):
                    nonlocal permit_released_before_eval
                    permit_released_before_eval = True
                    return orig_release(*args, **kwargs)

                lim.release_concurrency_permit = spy_release

                # Spy on default_evaluator_registry
                from app.evaluators import default_evaluator_registry

                orig_get = default_evaluator_registry.get_evaluator_fn

                def spy_get_fn(*args, **kwargs):
                    nonlocal parent_obs_active_during_eval
                    if getattr(mock_parent_obs, "is_active", False):
                        parent_obs_active_during_eval = True
                    return orig_get(*args, **kwargs)

                default_evaluator_registry.get_evaluator_fn = spy_get_fn

                res = await w.execute_item_message(*msg)
                assert res is True
                assert permit_released_before_eval is True
                # Evaluator was called while parent observation was active
                assert parent_obs_active_during_eval is True
                # And parent observation exited after all work
                assert mock_parent_obs.is_active is False

    asyncio.run(_test())
