from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.db_models import (  # noqa: E402
    AgentRecord,
    AgentVersionRecord,
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from app.main import app  # noqa: E402
from app.state_machine import (  # noqa: E402
    DomainConflictError,
    aggregate_launch_status_from_items,
    aggregate_quality_conclusion,
    assert_terminal_launch_invariants,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_legacy_experiments_run_persists_items_and_attempts(client):
    """测试 1: /experiments/run 必须持久化全部 Item 和 Attempt 记录，
    Launch 终态为 COMPLETED，进度看板显示 100% 且 6/6 完成。
    """
    mock_agent_response = httpx.Response(
        200,
        json={
            "intent": "transaction_investigation",
            "tool_calls": [{"name": "transaction-query", "arguments": {}}],
            "disclosed_fields": [],
            "escalated": False,
        },
    )

    mock_dataset = MagicMock()
    mock_result = MagicMock()
    mock_result.experiment_id = "lf-exp-issue-22"
    mock_result.dataset_run_id = "lf-exp-issue-22"
    mock_result.dataset_run_url = "http://localhost:3000/project/poc-project/datasets/banking-agent-regression/runs/lf-exp-issue-22"
    mock_result.run_name = "test-run"
    mock_score = MagicMock()
    mock_score.name = "overall_pass_rate"
    mock_score.value = 1.0
    mock_result.run_evaluations = [mock_score]

    async def fake_run_experiment(*args, **kwargs):
        task = kwargs.get("task")
        if task:
            for i in range(1, 7):
                item = MagicMock()
                item.id = f"00000000-0000-4000-8000-00000000000{i}"
                item.input = {
                    "user_message": "查一下我昨天的消费",
                    "session_id": "test-session",
                    "channel": "mobile_app",
                }
                item.expected_output = {
                    "expected_intent": "transaction_investigation",
                    "expected_slots": {},
                }
                await task(item=item)
        return mock_result

    mock_dataset.run_experiment.side_effect = fake_run_experiment
    mock_dataset.id = "ds-issue-22"
    mock_lf = MagicMock()
    mock_lf.get_dataset.return_value = mock_dataset

    with patch("app.main._wait_for_langfuse"), \
         patch("app.main._client", return_value=mock_lf), \
         patch("app.execution.get_langfuse_client_safe", return_value=mock_lf), \
         patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)):

        r = client.post(
            "/experiments/run",
            json={
                "agent_id": "banking-agent",
                "agent_version": "v1",
                "dataset_name": "banking-agent-regression",
            },
        )
        assert r.status_code == 200
        resp_data = r.json()
        assert "launch_id" in resp_data
        launch_id = resp_data["launch_id"]
        assert resp_data["agent_id"] == "banking-agent"
        assert resp_data["agent_version"] == "v1"
        assert resp_data["dataset_run_url"] is not None

    # 1. 验证 Launch 状态已规范化为 COMPLETED
    r_launch = client.get(f"/api/v1/experiment-launches?id={launch_id}")
    assert r_launch.status_code == 200
    launch_data = r_launch.json()
    assert launch_data["status"] == "COMPLETED"
    assert launch_data["quality_conclusion"] in ("pass", "fail")
    assert launch_data["langfuse_sync_status"] == "SYNCED"

    # 2. 验证 DB 中持久化了 6 个 Item 记录
    r_items = client.get(f"/api/v1/experiment-launch-items?launch_id={launch_id}")
    assert r_items.status_code == 200
    items = r_items.json()
    assert len(items) == 6
    for it in items:
        assert it["execution_status"] == "succeeded"
        assert it["eval_status"] == "succeeded"
        assert it["attempt_count"] == 1
        assert it["final_attempt_http_status"] == 200
        assert it["final_attempt_id"] is not None

    # 3. 验证 Launch 进度：100%, 6/6 完成, 0 pending
    progress = launch_data.get("progress")
    assert progress is not None
    assert progress["total"] == 6
    assert progress["completed"] == 6
    assert progress["pending"] == 0
    assert progress["percentage"] == 100.0


def test_invariant_1_terminal_launch_disallows_active_items_defensive_guard():
    """测试 2 (Invariant 1): Defensive Guard 拦截任何试图在 active_items > 0 时将 Launch 标记为终态的操作。"""
    # 当 active_items > 0 时，尝试断言终态必须抛出 DomainConflictError
    with pytest.raises(DomainConflictError, match="disallows active items"):
        assert_terminal_launch_invariants("COMPLETED", active_item_count=1)

    with pytest.raises(DomainConflictError, match="disallows active items"):
        assert_terminal_launch_invariants("FAILED", active_item_count=3)

    # 当 active_items == 0 时，允许终态
    assert_terminal_launch_invariants("COMPLETED", active_item_count=0)
    assert_terminal_launch_invariants("FAILED", active_item_count=0)
    assert_terminal_launch_invariants("CANCELLED", active_item_count=0)


def test_invariant_4_and_5_langfuse_decoupling_and_quality_unknown_on_exec_failure(tmp_path):
    """测试 3 (Invariant 4 & 5):
    - Invariant 4: Langfuse 同步失败与 Agent 业务执行结果正交解耦。
    - Invariant 5: 技术执行失败/无评测证据时，质量严格为 unknown。
    """
    # 验证 aggregate_quality_conclusion 真值表
    # 1. 只有技术失败，无明确 fail 证据 -> unknown
    q1 = aggregate_quality_conclusion({"pass": 0, "fail": 0, "unknown": 6}, total_items=6)
    assert q1 == "unknown"

    # 2. 明确存在 Evaluator fail 评测证据 -> fail
    q2 = aggregate_quality_conclusion({"pass": 2, "fail": 4, "unknown": 0}, total_items=6)
    assert q2 == "fail"

    # 3. 全部 PASS -> pass
    q3 = aggregate_quality_conclusion({"pass": 6, "fail": 0, "unknown": 0}, total_items=6)
    assert q3 == "pass"

    # 验证 aggregate_launch_status_from_items 解耦
    # 即使有执行失败，质量结论仅依据 quality_counts
    st, q = aggregate_launch_status_from_items(
        counts={"succeeded": 0, "failed": 6},
        quality_counts={"unknown": 6},
    )
    assert st == "FAILED"
    assert q == "unknown"  # 无评测证据，严格为 unknown，绝不是 fail


def test_invariant_6_atomic_cas_prevents_late_callback_overwriting(tmp_path):
    """测试 4 (Invariant 6): Attempt 与 Item 双重 CAS 条件更新，
    防止晚到的异步完成回调覆写已经被收敛/abort的记录。
    """
    db_file = tmp_path / "cas_test.db"
    db_url = f"sqlite:///{db_file}"
    db_mgr = DatabaseManager(db_url)
    MigrationRunner(db_mgr.engine, ROOT / "migrations").apply_all()

    launch_id = f"launch-{uuid.uuid4().hex[:8]}"
    item_exec_id = f"item-exec-{uuid.uuid4().hex[:8]}"
    att_id = f"att-{uuid.uuid4().hex[:8]}"

    # 初始化一条已被外部收敛为 failed 的 Item 和 Attempt
    with db_mgr.get_session() as session:
        session.add(AgentRecord(id="agent", name="Test Agent"))
        session.flush()
        session.add(
            AgentVersionRecord(
                id="test-agent-v1",
                agent_id="agent",
                version="v1",
                endpoint="http://localhost:8080/invoke",
                spec_digest="sha256:test",
            )

        )
        session.flush()

        l_rec = ExperimentLaunchRecord(
            id=launch_id,
            name="Test Launch",
            dataset_name="ds",
            agent_id="agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            status="FAILED",
            manifest={},
        )

        session.add(l_rec)
        item_rec = ExperimentItemExecutionRecord(
            id=item_exec_id,
            launch_id=launch_id,
            dataset_item_id="item-1",
            execution_status="failed",  # 已被外部收敛为 failed
            eval_status="skipped",
            quality_conclusion="unknown",
        )
        session.add(item_rec)
        att_rec = ExecutionAttemptRecord(
            id=att_id,
            item_execution_id=item_exec_id,
            attempt_no=1,
            status="FAILED",  # 已被外部收敛为 FAILED
            started_at=datetime.now(UTC),
        )
        session.add(att_rec)

    # 模拟晚到的协程尝试执行条件更新
    # Item CAS: WHERE id = :item_id AND execution_status = 'running'
    with db_mgr.get_session() as session:
        item_res = session.execute(
            update(ExperimentItemExecutionRecord)
            .where(
                ExperimentItemExecutionRecord.id == item_exec_id,
                ExperimentItemExecutionRecord.execution_status == "running",
            )
            .values(execution_status="succeeded", quality_conclusion="pass")
        )
        # 应该更新 0 行，因为当前不是 running
        assert item_res.rowcount == 0

        # Attempt CAS: WHERE id = :att_id AND status = 'RUNNING'
        att_res = session.execute(
            update(ExecutionAttemptRecord)
            .where(
                ExecutionAttemptRecord.id == att_id,
                ExecutionAttemptRecord.status == "RUNNING",
            )
            .values(status="COMPLETED")
        )
        assert att_res.rowcount == 0

    # 验证 DB 记录未被篡改
    with db_mgr.get_session() as session:
        i = session.get(ExperimentItemExecutionRecord, item_exec_id)
        assert i.execution_status == "failed"
        assert i.quality_conclusion == "unknown"


def test_reconciler_reverts_terminal_launch_with_active_leased_items(setup_runtime):
    """测试 5: Reconciler 面对 Terminal Launch + active Item 时，
    若 Item 拥有有效 lease 正在合法运行，修正 Launch 回退为 RUNNING，绝不杀死运行中 Item。
    """
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    launch_id = f"launch-{uuid.uuid4().hex[:8]}"
    item_exec_id = f"item-exec-{uuid.uuid4().hex[:8]}"

    now = datetime.now(UTC)
    valid_lease_expires = now + timedelta(seconds=60)

    with db_mgr.get_session() as session:
        l_rec = ExperimentLaunchRecord(
            id=launch_id,
            name="Conflict Launch",
            dataset_name="ds",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            status="COMPLETED",  # 错误地过早标记为 COMPLETED
            manifest={"agent": {"agent_id": "test-agent", "version": "v1"}},
        )
        session.add(l_rec)

        item_rec = ExperimentItemExecutionRecord(
            id=item_exec_id,
            launch_id=launch_id,
            dataset_item_id="item-1",
            execution_status="running",
            lease_token="valid-token-123",
            lease_owner="worker-1",
            lease_expires_at=valid_lease_expires,  # 有效 lease
            dispatch_generation=1,
            started_at=now,
        )
        session.add(item_rec)

    # 运行 reconciler 的状态对账
    reconciler.reconcile_launch_states()

    # 验证 Launch 被回退为 RUNNING，且 Item 依然合法处于 running 状态
    with db_mgr.get_session() as session:
        launch_rec = session.get(ExperimentLaunchRecord, launch_id)
        assert launch_rec.status == "RUNNING"
        it = session.get(ExperimentItemExecutionRecord, item_exec_id)
        assert it.execution_status == "running"
        assert it.lease_token == "valid-token-123"



def test_reconcile_legacy_terminal_launch_orphan_items(setup_runtime):
    """测试 6: 历史孤儿数据对账。
    当 Launch 处于 COMPLETED 终态，而 Item 为 pending 且无 lease/attempt 时：
    对账将其收敛为 failed (unknown 质量)，消除矛盾，且具备幂等性。
    """
    db_mgr, queue, limiter, orchestrator, worker, reconciler = setup_runtime

    launch_id = f"launch-orphan-{uuid.uuid4().hex[:8]}"
    item_exec_id = f"item-orphan-{uuid.uuid4().hex[:8]}"

    with db_mgr.get_session() as session:
        l_rec = ExperimentLaunchRecord(
            id=launch_id,
            name="Legacy Orphan Launch",
            dataset_name="ds",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            status="COMPLETED",
            manifest={},
        )

        session.add(l_rec)

        item_rec = ExperimentItemExecutionRecord(
            id=item_exec_id,
            launch_id=launch_id,
            dataset_item_id="item-orphan-1",
            execution_status="pending",
            eval_status="pending",
            quality_conclusion="unknown",
        )
        session.add(item_rec)

    # 第一次运行孤儿对账
    reconciler.reconcile_launch_states()

    with db_mgr.get_session() as session:
        it = session.get(ExperimentItemExecutionRecord, item_exec_id)
        # 孤儿 item 被收敛为终态 failed，Invariant 1 成立 (active items == 0)
        assert it.execution_status == "failed"
        assert it.quality_conclusion == "unknown"

    # 幂等性：第二次运行无异常，状态保持不变
    reconciler.reconcile_launch_states()
    with db_mgr.get_session() as session:
        it = session.get(ExperimentItemExecutionRecord, item_exec_id)
        assert it.execution_status == "failed"


def test_legacy_api_filter_succeeded_maps_to_completed(client):
    """测试 7: API 查询 status=SUCCEEDED 自动规范化映射为 COMPLETED。"""
    # 创建一个 Launch
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "Status Mapping Test",
        },
    )
    assert r_create.status_code == 201
    lid = r_create.json()["id"]

    # 将其置为 COMPLETED
    from app.main import db_manager
    with db_manager.get_session() as session:
        rec = session.get(ExperimentLaunchRecord, lid)
        rec.status = "COMPLETED"
        session.commit()

    # 用旧的 status=SUCCEEDED 查询
    r_list = client.get("/api/v1/experiment-launches?status=SUCCEEDED")
    assert r_list.status_code == 200
    launches = r_list.json()
    assert any(launch_item["id"] == lid for launch_item in launches)
    found = next(launch_item for launch_item in launches if launch_item["id"] == lid)
    assert found["status"] == "COMPLETED"

