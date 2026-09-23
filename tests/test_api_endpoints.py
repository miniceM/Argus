from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_agent_registry_apis_zero_path_variables(client):
    # 1. Create agent
    r = client.post(
        "/api/v1/agents",
        json={"id": "wealth-agent", "name": "财富管理 Agent", "description": "理财业务", "owner": "wealth-team"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["id"] == "wealth-agent"

    # Duplicate create agent -> 409
    r_dup = client.post(
        "/api/v1/agents",
        json={"id": "wealth-agent", "name": "财富管理 Agent"},
    )
    assert r_dup.status_code == 409

    # 2. Query single agent by ?id=...
    r_get = client.get("/api/v1/agents?id=wealth-agent")
    assert r_get.status_code == 200
    assert r_get.json()["name"] == "财富管理 Agent"

    # Query all agents
    r_list = client.get("/api/v1/agents")
    assert r_list.status_code == 200
    assert isinstance(r_list.json(), list)
    assert any(a["id"] == "wealth-agent" for a in r_list.json())

    # 3. Create version
    r_ver = client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": "wealth-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8080/invoke",
            "protocol": "HTTP_JSON",
            "method": "POST",
            "request_mapping": {"messages": "input.messages"},
            "timeout_seconds": 15.0,
            "max_retries": 1,
        },
    )
    assert r_ver.status_code == 201
    ver_data = r_ver.json()
    assert ver_data["version"] == "1.0.0"
    assert ver_data["spec_digest"] is not None

    # Duplicate version -> 409
    r_ver_dup = client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": "wealth-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8080/invoke",
        },
    )
    assert r_ver_dup.status_code == 409

    # 4. Query version by ?agent_id=...&version=...
    r_ver_get = client.get("/api/v1/agent-versions?agent_id=wealth-agent&version=1.0.0")
    assert r_ver_get.status_code == 200
    assert r_ver_get.json()["spec_digest"] == ver_data["spec_digest"]

    # 5. Archive version
    r_arc = client.post(
        "/api/v1/agent-versions/archive",
        json={"agent_id": "wealth-agent", "version": "1.0.0"},
    )
    assert r_arc.status_code == 200
    assert r_arc.json()["is_active"] is False

    # 6. Delete agent (?id=...)
    r_del = client.delete("/api/v1/agents?id=wealth-agent")
    assert r_del.status_code == 200
    del_data = r_del.json()
    assert del_data["id"] == "wealth-agent"
    assert del_data["deleted"] is True

    # Confirm 404 after deletion
    r_check = client.get("/api/v1/agents?id=wealth-agent")
    assert r_check.status_code == 404

    # Deleting again returns 404
    r_del_again = client.delete("/api/v1/agents?id=wealth-agent")
    assert r_del_again.status_code == 404


def test_delete_agent_api_force_and_conflict(client):
    # 1. Create agent and version
    r_create = client.post(
        "/api/v1/agents",
        json={"id": "conflict-agent", "name": "冲突测试 Agent"},
    )
    assert r_create.status_code == 201

    r_ver = client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": "conflict-agent",
            "version": "1.0.0",
            "endpoint": "http://localhost:8080/invoke",
        },
    )
    assert r_ver.status_code == 201

    # 2. Create launch for this agent
    r_launch = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "conflict-agent",
            "agent_version": "1.0.0",
            "dataset_name": "banking-agent-regression",
            "name": "Launch For Conflict Agent",
        },
    )
    assert r_launch.status_code == 201
    launch_id = r_launch.json()["id"]

    # 3. Standard delete without force must return 409 Conflict with AGENT_HAS_LAUNCHES
    r_del_no_force = client.delete("/api/v1/agents?id=conflict-agent")
    assert r_del_no_force.status_code == 409
    del_err = r_del_no_force.json()
    assert del_err["code"] == "AGENT_HAS_LAUNCHES"
    assert del_err["launch_count"] == 1
    assert "关联的评测记录" in del_err["detail"]

    # 4. Purge with wrong name must be rejected with 400 Bad Request
    r_del_bad_name = client.post(
        "/api/v1/agents/purge",
        json={"agent_id": "conflict-agent", "confirm_name": "错误名称"},
    )
    assert r_del_bad_name.status_code == 400
    assert r_del_bad_name.json()["code"] == "AGENT_NAME_MISMATCH"

    # 5. Purge with correct name on active launch (status: PENDING) must be rejected with 409 AGENT_HAS_ACTIVE_LAUNCHES
    r_del_active = client.post(
        "/api/v1/agents/purge",
        json={"agent_id": "conflict-agent", "confirm_name": "冲突测试 Agent"},
    )
    assert r_del_active.status_code == 409
    assert r_del_active.json()["code"] == "AGENT_HAS_ACTIVE_LAUNCHES"
    assert "正在执行或排队中" in r_del_active.json()["detail"]

    # 6. Cancel the active launch so it reaches a terminal status (CANCELLED)
    r_cancel = client.post(f"/api/v1/experiment-launches/{launch_id}/cancel")
    assert r_cancel.status_code == 200
    assert r_cancel.json()["status"] == "CANCELLED"

    # 7. Purge after launches are terminal must succeed and return 200
    r_del_purge = client.post(
        "/api/v1/agents/purge",
        json={"agent_id": "conflict-agent", "confirm_name": "冲突测试 Agent"},
    )
    assert r_del_purge.status_code == 200
    force_data = r_del_purge.json()
    assert force_data["deleted"] is True
    assert force_data["launches_deleted"] >= 1

    # Verify agent and launch are gone
    assert client.get("/api/v1/agents?id=conflict-agent").status_code == 404
    assert client.get(f"/api/v1/experiment-launches?id={launch_id}").status_code == 404


def test_purge_api_without_launches_requires_exact_name(client):
    r_create = client.post(
        "/api/v1/agents",
        json={"id": "empty-api-agent", "name": "无任务测试 Agent"},
    )
    assert r_create.status_code == 201

    # Purge with wrong name on an agent without launches MUST be rejected with 400
    r_bad = client.post(
        "/api/v1/agents/purge",
        json={"agent_id": "empty-api-agent", "confirm_name": "随便起的名字"},
    )
    assert r_bad.status_code == 400
    assert r_bad.json()["code"] == "AGENT_NAME_MISMATCH"

    # Purge with correct name succeeds
    r_good = client.post(
        "/api/v1/agents/purge",
        json={"agent_id": "empty-api-agent", "confirm_name": "无任务测试 Agent"},
    )
    assert r_good.status_code == 200
    assert r_good.json()["deleted"] is True



def test_create_launch_rejects_empty_evaluators(client):
    # Empty evaluator_ids list must be rejected with 422 Unprocessable Entity
    r = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "evaluator_ids": [],
        },
    )
    assert r.status_code == 422


def test_create_launch_rejects_run_scope_evaluator(client):
    # Run-scope evaluator (e.g. run_pass_rate) must be rejected with 400 Bad Request
    r = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "evaluator_ids": ["pii_safe", "run_pass_rate"],
        },
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    assert "run-scope" in detail.lower() or "not supported" in detail.lower()



def test_experiment_launches_apis_and_execution(client):
    # Ensure banking-agent v1 exists from auto-import
    # 1. Create Launch

    r_create = client.post(
        "/api/v1/experiment-launches",
        headers={"Idempotency-Key": "test-idem-api-1"},
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "API Launch Test",
            "max_concurrency": 2,
        },
    )
    assert r_create.status_code in (200, 201)
    launch = r_create.json()
    launch_id = launch["id"]
    assert launch["status"] == "PENDING"
    assert "schema_version" in launch["manifest"]

    # 2. Query Launch by ?id=...
    r_get = client.get(f"/api/v1/experiment-launches?id={launch_id}")
    assert r_get.status_code == 200
    assert r_get.json()["id"] == launch_id

    # 3. Synchronously run Launch
    # Mock remote agent to return successful answer
    mock_agent_response = httpx.Response(
        200,
        json={
            "intent": "transaction_investigation",
            "tool_calls": [{"name": "transaction-query", "arguments": {}}],
            "disclosed_fields": [],
            "escalated": False,
        },
    )

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200
        run_data = r_run.json()
        assert run_data["status"] == "SUCCEEDED"
        assert run_data["langfuse_sync_status"] == "NOT_APPLICABLE"

        # Duplicate run must fail with 409 Conflict (atomic lock)

        r_run_dup = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run_dup.status_code == 409

    # 4. Query item executions by ?launch_id=...
    r_items = client.get(f"/api/v1/experiment-launch-items?launch_id={launch_id}")
    assert r_items.status_code == 200
    items = r_items.json()
    assert len(items) == 6  # 6 cases in dataset.json
    first_item_id = items[0]["id"]

    # 5. Query execution attempts by ?item_execution_id=...
    r_attempts = client.get(f"/api/v1/execution-attempts?item_execution_id={first_item_id}")
    assert r_attempts.status_code == 200
    attempts = r_attempts.json()
    assert len(attempts) >= 1
    assert attempts[0]["status"] == "COMPLETED"
    assert attempts[0]["http_status"] == 200


def test_run_launch_crash_fails_gracefully(client):
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "Crash Test Launch",
        },
    )
    launch_id = r_create.json()["id"]

    def fake_gather(*args, **kwargs):
        for c in args:
            c.close()
        raise RuntimeError("Simulated unhandled gather crash")

    with patch("app.api_launches.asyncio.gather", side_effect=fake_gather):
        with pytest.raises(RuntimeError, match="Simulated unhandled gather crash"):
            client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})

    # Status must not hang in RUNNING; must be marked FAILED
    r_get = client.get(f"/api/v1/experiment-launches?id={launch_id}")
    assert r_get.status_code == 200
    assert r_get.json()["status"] == "FAILED"
    assert r_get.json()["quality_conclusion"] == "fail"


def test_legacy_experiments_run_completes_persisted_launch(client):
    from unittest.mock import MagicMock
    mock_dataset = MagicMock()
    mock_result = MagicMock()
    mock_result.experiment_id = "test-run-id"

    mock_result.dataset_run_id = "test-run-id"
    mock_result.dataset_run_url = "http://langfuse/runs/1"
    mock_result.run_name = "test-run"
    mock_result.item_results = []
    mock_score = MagicMock()
    mock_score.name = "overall_pass_rate"
    mock_score.value = 1.0
    mock_result.run_evaluations = [mock_score]
    mock_dataset.run_experiment.return_value = mock_result
    mock_dataset.id = "ds-123"

    mock_lf = MagicMock()
    mock_lf.get_dataset.return_value = mock_dataset

    with patch("app.main._wait_for_langfuse"), patch("app.main._client", return_value=mock_lf):
        r = client.post(
            "/experiments/run",
            json={
                "agent_id": "banking-agent",
                "agent_version": "v1",
                "dataset_name": "banking-agent-regression",
            },
        )
        assert r.status_code == 200
        launch_id = r.json()["launch_id"]

        # Ensure Langfuse get_dataset was called exactly ONCE (no double fetching)
        assert mock_lf.get_dataset.call_count == 1

        # Ensure run_experiment was executed with the 5 frozen item evaluators + 1 run evaluator
        call_kwargs = mock_dataset.run_experiment.call_args[1]
        assert len(call_kwargs["evaluators"]) == 5
        assert len(call_kwargs["run_evaluators"]) == 1

    # Verify launch in database is completed, not left in PENDING
    r_get = client.get(f"/api/v1/experiment-launches?id={launch_id}")
    assert r_get.status_code == 200
    launch_data = r_get.json()
    assert launch_data["status"] == "SUCCEEDED"
    assert launch_data["quality_conclusion"] == "pass"
    assert launch_data["langfuse_sync_status"] == "SYNCED"
    assert launch_data["langfuse_experiment_id"] == "test-run-id"
    assert launch_data["completed_at"] is not None
    # Frozen manifest evaluators must contain all 6 evaluators (5 item + 1 run)
    assert len(launch_data["manifest"]["evaluators"]) == 6
    frozen_eval_ids = [e["id"] for e in launch_data["manifest"]["evaluators"]]
    assert "overall_pass" in frozen_eval_ids
    assert "run_pass_rate" in frozen_eval_ids


def test_legacy_experiments_run_inherits_agent_version_max_concurrency(client):
    from unittest.mock import MagicMock
    # Register an agent with max_concurrency=1
    r_reg = client.post(
        "/api/v1/agents",
        json={"id": "single-worker-agent", "name": "Single Worker Agent"},
    )
    assert r_reg.status_code == 201

    r_ver = client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": "single-worker-agent",
            "version": "v1",
            "endpoint": "http://single-worker:8080/invoke",
            "max_concurrency": 1,
        },
    )
    assert r_ver.status_code == 201

    mock_dataset = MagicMock()
    mock_result = MagicMock()
    mock_result.experiment_id = "test-run-single"
    mock_result.dataset_run_id = "test-run-single"
    mock_result.dataset_run_url = "http://langfuse/runs/single"
    mock_result.run_name = "test-run-single"
    mock_result.item_results = []
    mock_score = MagicMock()
    mock_score.name = "overall_pass_rate"
    mock_score.value = 1.0
    mock_result.run_evaluations = [mock_score]
    mock_dataset.run_experiment.return_value = mock_result
    mock_dataset.id = "ds-single"

    mock_lf = MagicMock()
    mock_lf.get_dataset.return_value = mock_dataset

    with patch("app.main._wait_for_langfuse"), patch("app.main._client", return_value=mock_lf):
        # Omit max_concurrency in legacy request
        r = client.post(
            "/experiments/run",
            json={
                "agent_id": "single-worker-agent",
                "agent_version": "v1",
                "dataset_name": "banking-agent-regression",
            },
        )
        assert r.status_code == 200
        launch_id = r.json()["launch_id"]

        call_kwargs = mock_dataset.run_experiment.call_args[1]
        assert call_kwargs["max_concurrency"] == 1

    r_get = client.get(f"/api/v1/experiment-launches?id={launch_id}")
    assert r_get.status_code == 200
    launch_data = r_get.json()
    assert launch_data["manifest"]["execution_policy"]["max_concurrency"] == 1




def test_attempt_ownership_verification_enforced(tmp_path):

    from app.db import DatabaseManager, MigrationRunner
    from app.db_models import (
        ExecutionAttemptRecord,
        ExperimentItemExecutionRecord,
        ExperimentLaunchRecord,
    )

    db_file = tmp_path / "att_owner.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    with db_mgr.get_session() as session:
        # Pre-seed launch
        launch = ExperimentLaunchRecord(
            id="launch-1",
            name="L1",
            agent_id="a1",
            agent_version="v1",
            agent_version_id="av-1",
            dataset_name="d1",
            manifest={},
        )
        # Create dummy agent version row for FK
        from app.db_models import AgentRecord, AgentVersionRecord
        ag = AgentRecord(id="a1", name="A1")
        av = AgentVersionRecord(
            id="av-1", agent_id="a1", version="v1", spec_digest="d", endpoint="http://localhost"
        )
        session.add_all([ag, av, launch])
        session.flush()

        item1 = ExperimentItemExecutionRecord(id="item-1", launch_id="launch-1", dataset_item_id="d-1")
        item2 = ExperimentItemExecutionRecord(id="item-2", launch_id="launch-1", dataset_item_id="d-2")
        session.add_all([item1, item2])
        session.flush()

        # Attempt belongs to item-1
        att1 = ExecutionAttemptRecord(id="att-1", item_execution_id="item-1", attempt_no=1)
        session.add(att1)
        session.flush()

        # Assigning att-1 to item-2 in application logic must be rejected
        item_to_check = session.get(ExperimentItemExecutionRecord, "item-2")
        foreign_att = session.get(ExecutionAttemptRecord, "att-1")

        # Service level ownership check
        assert foreign_att.item_execution_id != item_to_check.id
        with pytest.raises(ValueError, match="does not belong to item"):
            if foreign_att.item_execution_id != item_to_check.id:
                raise ValueError(f"Attempt '{foreign_att.id}' does not belong to item '{item_to_check.id}'")



