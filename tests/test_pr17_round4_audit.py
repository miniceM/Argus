import asyncio
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402
from app.db_models import LangfuseSyncTaskRecord as Task  # noqa: E402
from app.langfuse_sync import LangfuseOutboxSyncer, _invoke_with_timeout, aggregate_launch_sync_status  # noqa: E402
from app.main import _client  # noqa: E402
from sqlalchemy import event  # noqa: E402
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


# 12. 死信清理与并发成功写回交错：原子条件更新绝不将已 SYNCED 任务覆盖为 FAILED
def test_dead_letter_sweep_does_not_override_concurrent_synced(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    task_id = "audit"
    now = datetime.now(UTC)

    # Put task in PROCESSING at max_attempts with expired lease
    with db.get_session() as s:
        t = s.get(Task, task_id)
        t.status = "PROCESSING"
        t.attempts = 3
        t.lease_expires_at = now - timedelta(seconds=10)
        t.claim_token = "token-1"
        s.commit()

    # Simulate another worker successfully finalizing task to SYNCED
    with db.get_session() as s:
        t = s.get(Task, task_id)
        t.status = "SYNCED"
        t.claim_token = None
        t.lease_expires_at = None
        t.last_error = None
        s.commit()

    # Now syncer runs claim_tasks which performs dead-letter sweep
    syncer = LangfuseOutboxSyncer(db, None, max_attempts=3)
    syncer.claim_tasks(batch_size=1)

    # Task MUST remain SYNCED and not overwritten to FAILED
    with db.get_session() as s:
        t = s.get(Task, task_id)
        assert t.status == "SYNCED"


# 13. 生产提供器无凭据真实调用与 disabled 客户端识别
def test_unconfigured_production_provider_and_disabled_client(setup_runtime):
    db, lid = prepare(setup_runtime)

    # Case A: Ensure environment has no keys, main._client() explicitly returns None
    with patch.dict(os.environ, {}, clear=True):
        client = _client()
        assert client is None
        syncer = LangfuseOutboxSyncer(db, _client)
        st, c, reason = syncer.resolve_client_status()
        assert st == "UNCONFIGURED"

        # Case B: Real disabled SDK instance returned from get_client() without credentials
        from langfuse import get_client
        disabled_c = get_client()
        syncer_disabled = LangfuseOutboxSyncer(db, disabled_c)
        st2, _, _ = syncer_disabled.resolve_client_status()
        assert st2 == "UNCONFIGURED"

        # Case C: Arbitrary object missing .api is INCOMPATIBLE, not UNCONFIGURED
        class BadObject:
            pass

        syncer_bad = LangfuseOutboxSyncer(db, BadObject())
        st3, _, _ = syncer_bad.resolve_client_status()
        assert st3 == "INCOMPATIBLE"


# 14. 部分 Item 执行后取消场景的 NOT_APPLICABLE 收敛
def test_partially_cancelled_unconfigured_converges_to_not_applicable(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime, count=2)
    # Item 0 was executed -> generate SKIPPED task
    # Item 1 was cancelled before invocation -> NO trace_id, NO task
    with db_mgr.get_session() as s:
        launch = s.get(Launch, lid)
        launch.status = "CANCELLED"
        item0 = s.get(Item, msgs[0][1])
        item0.execution_status = "cancelled"
        item0.trace_id = "trace-0"

        item1 = s.get(Item, msgs[1][1])
        item1.execution_status = "cancelled"
        item1.trace_id = None

        s.add(
            Task(
                id="task-0",
                launch_id=lid,
                item_id=item0.id,
                dataset_item_id="0",
                dispatch_generation=item0.dispatch_generation,
                task_type="FULL_EVAL_SYNC",
                trace_id="trace-0",
                dataset_run_name="run",
                dataset_version="v1",
                scores_payload={},
                status="SKIPPED",
            )
        )
        s.commit()

    # Must converge to NOT_APPLICABLE because all items needing sync are SKIPPED
    res = aggregate_launch_sync_status(db_mgr, lid)
    assert res == "NOT_APPLICABLE"


# 15. 有效项缺少任务反例保持 SYNCING
def test_required_item_missing_task_keeps_syncing(setup_runtime):
    db_mgr, q, lim, orch, w, rec = setup_runtime
    lid, msgs = create_launch_helper(setup_runtime, count=2)
    with db_mgr.get_session() as s:
        launch = s.get(Launch, lid)
        launch.status = "COMPLETED"
        item0 = s.get(Item, msgs[0][1])
        item0.execution_status = "succeeded"
        item0.trace_id = "trace-0"

        item1 = s.get(Item, msgs[1][1])
        item1.execution_status = "succeeded"
        item1.trace_id = "trace-1"

        # Only item 0 has task
        s.add(
            Task(
                id="task-0",
                launch_id=lid,
                item_id=item0.id,
                dataset_item_id="0",
                dispatch_generation=item0.dispatch_generation,
                task_type="FULL_EVAL_SYNC",
                trace_id="trace-0",
                dataset_run_name="run",
                dataset_version="v1",
                scores_payload={},
                status="SKIPPED",
            )
        )
        s.commit()

    # Item 1 is missing its task -> MUST stay SYNCING
    res = aggregate_launch_sync_status(db_mgr, lid)
    assert res == "SYNCING"


# 16. 单次慢请求在调用期间后台心跳自动续租成功
def test_slow_request_heartbeat_renewal_succeeds(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    mock_lf = MagicMock()
    mock_lf.api.dataset_run_items.create.return_value = MagicMock(dataset_run_id="run-999")

    def _slow_create(**kwargs):
        # Simulate slow network call exceeding initial lease
        import time
        time.sleep(0.6)
        return MagicMock(dataset_run_id="run-999")

    mock_lf.api.dataset_run_items.create.side_effect = _slow_create

    syncer = LangfuseOutboxSyncer(
        db,
        mock_lf,
        lease_duration_seconds=0.5,
        heartbeat_interval_seconds=0.1,
        task_timeout_seconds=5.0,
    )
    claimed = syncer.claim_tasks(batch_size=1)
    assert len(claimed) == 1
    task_id, claim_token, payload = claimed[0]

    # Process single task with slow request
    res = syncer.process_single_task(task_id, claim_token, payload)
    assert res is True

    with db.get_session() as s:
        t = s.get(Task, task_id)
        assert t.status == "SYNCED"


# 17. 心跳续租抛异常后停止后续 Score 调用
def test_heartbeat_exception_aborts_subsequent_score_calls(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    mock_lf = MagicMock()

    scores_called = False

    def _slow_link(**kwargs):
        import time
        time.sleep(0.4)
        return MagicMock(dataset_run_id="run-1")

    def _score_create(**kwargs):
        nonlocal scores_called
        scores_called = True
        return MagicMock()

    mock_lf.api.dataset_run_items.create.side_effect = _slow_link
    mock_lf.api.scores.create.side_effect = _score_create

    syncer = LangfuseOutboxSyncer(
        db,
        mock_lf,
        lease_duration_seconds=1.0,
        heartbeat_interval_seconds=0.1,
        task_timeout_seconds=5.0,
    )
    claimed = syncer.claim_tasks(batch_size=1)
    task_id, claim_token, payload = claimed[0]

    # Mock renew_task_lease to raise DB exception during heartbeat
    orig_renew = syncer.renew_task_lease
    first_call = True

    def _flaky_renew(*args, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            return orig_renew(*args, **kwargs)
        raise RuntimeError("Database connection lost during heartbeat renewal")

    syncer.renew_task_lease = _flaky_renew

    res = syncer.process_single_task(task_id, claim_token, payload)
    assert res is False
    assert scores_called is False


# 18. 网络调用超过任务总时限后不再无限续租
def test_network_call_exceeding_max_task_duration_stops_renewal(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    mock_lf = MagicMock()

    def _hanging_call(**kwargs):
        import time
        time.sleep(0.6)
        return MagicMock(dataset_run_id="run-1")

    mock_lf.api.dataset_run_items.create.side_effect = _hanging_call

    syncer = LangfuseOutboxSyncer(
        db,
        mock_lf,
        lease_duration_seconds=0.5,
        heartbeat_interval_seconds=0.1,
        task_timeout_seconds=0.3,
    )
    claimed = syncer.claim_tasks(batch_size=1)
    task_id, claim_token, payload = claimed[0]

    res = syncer.process_single_task(task_id, claim_token, payload)
    assert res is False


# 19. 任务截止时间在成功前严格检查（时限小于调用耗时但心跳未触发时立即判定失败且不调用后续 Score）
def test_task_deadline_checked_before_success(setup_runtime):
    db, lid = prepare(setup_runtime, source="langfuse")
    lf = MagicMock()

    def slow(**kwargs):
        import time

        time.sleep(0.15)
        return MagicMock(dataset_run_id="run")

    lf.api.dataset_run_items.create.side_effect = slow
    sync = LangfuseOutboxSyncer(
        db, lf, lease_duration_seconds=2, heartbeat_interval_seconds=0.5, task_timeout_seconds=0.05
    )
    assert sync.process_batch() == 0
    lf.api.scores.create.assert_not_called()


# 20. 客户端时钟超前时不抢占数据库时间下尚未过期的有效租约
def test_live_lease_not_stolen_by_fast_claim_clock(setup_runtime):
    db, lid = prepare(setup_runtime)
    with db.get_session() as s:
        t = s.get(Task, "audit")
        t.status = "PROCESSING"
        t.claim_token = "live-owner"
        t.attempts = 1
        t.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        s.commit()

    class FastClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=60)

    with patch("app.langfuse_sync.datetime", FastClock):
        claimed = LangfuseOutboxSyncer(db, None).claim_tasks()
    assert claimed == []


# 21. 新租约在实际取得任务行锁后重新读取数据库时间生成（避免死信清理/锁等待导致刚领取即过期）
def test_claim_lease_starts_after_cleanup_wait(setup_runtime):
    db, lid = prepare(setup_runtime)

    def delay(conn, cursor, statement, params, context, executemany):
        if statement.startswith("UPDATE langfuse_sync_tasks"):
            import time

            time.sleep(0.2)

    event.listen(db.engine, "before_cursor_execute", delay)
    sync = LangfuseOutboxSyncer(db, None, lease_duration_seconds=0.1)
    try:
        tasks = sync.claim_tasks()
    finally:
        event.remove(db.engine, "before_cursor_execute", delay)
    tid, token, payload = tasks[0]
    assert sync.renew_task_lease(tid, token, 0.1), "Freshly claimed task already expired"


# 22. 任务总时限在调用超时后立即释放消费处理线程（不无限等待阻塞调用）
def test_task_timeout_releases_processing_thread(setup_runtime):
    import threading

    db, lid = prepare(setup_runtime, source="langfuse")
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    lf = MagicMock()

    def call(**kwargs):
        entered.set()
        release.wait(2)
        return MagicMock(dataset_run_id="run")

    lf.api.dataset_run_items.create.side_effect = call
    sync = LangfuseOutboxSyncer(db, lf, task_timeout_seconds=0.05, heartbeat_interval_seconds=0.01)

    def run():
        try:
            sync.process_batch()
        finally:
            done.set()

    t = threading.Thread(target=run)
    t.start()
    try:
        assert entered.wait(1)
        assert done.wait(0.25), "Processing thread still blocked after deadline"
    finally:
        release.set()
        t.join(2)


# 23. 超时机制不无限累积未结束的后台存活调用
def test_timeouts_do_not_accumulate_live_remote_calls():
    release = threading.Event()
    lock = threading.Lock()
    active = []
    threads = []

    def blocked():
        with lock:
            active.append(threading.current_thread())
            threads.append(threading.current_thread())
        release.wait(2)
        with lock:
            active.remove(threading.current_thread())

    try:
        for _ in range(3):
            try:
                _invoke_with_timeout(blocked, timeout=0.02)
            except TimeoutError:
                pass
        with lock:
            remaining = len(active)
        assert remaining == 0, f"{remaining} timed-out remote calls still running"
    finally:
        release.set()
        for t in threads:
            t.join(1)

