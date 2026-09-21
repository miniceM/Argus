from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_unified_execution_with_langfuse(client):
    # 1. Create a launch
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "Unified Execution Test",
        },
    )
    assert r_create.status_code == 201
    launch_id = r_create.json()["id"]

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
    mock_result.experiment_id = "lf-exp-999"
    mock_result.dataset_run_id = "lf-exp-999"
    mock_result.dataset_run_url = "http://localhost:3000/project/poc-project/datasets/banking-agent-regression/runs/lf-exp-999"

    async def fake_run_experiment(*args, **kwargs):
        task = kwargs.get("task")
        if task:
            for i in range(1, 7):
                item = MagicMock()
                item.id = f"00000000-0000-4000-8000-00000000000{i}"
                item.input = {"user_message": "test"}
                item.expected_output = {"expected_intent": "transaction_investigation"}
                await task(item=item)
        return mock_result

    mock_dataset.run_experiment.side_effect = fake_run_experiment
    mock_lf = MagicMock()
    mock_lf.get_dataset.return_value = mock_dataset

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)), \
         patch("app.execution.get_langfuse_client_safe", return_value=mock_lf):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200
        run_data = r_run.json()
        assert run_data["status"] == "SUCCEEDED"
        assert run_data["langfuse_sync_status"] == "SYNCED"
        assert run_data["langfuse_experiment_id"] == "lf-exp-999"
        assert run_data["langfuse_experiment_url"] == "http://localhost:3000/project/poc-project/datasets/banking-agent-regression/runs/lf-exp-999"
        assert run_data["links"]["langfuse_experiment"] == run_data["langfuse_experiment_url"]

    # Check that item executions and attempts are saved in DB
    r_items = client.get(f"/api/v1/experiment-launch-items?launch_id={launch_id}")
    assert r_items.status_code == 200
    items = r_items.json()
    assert len(items) == 6
    for item in items:
        assert item["execution_status"] == "succeeded"
        assert item["attempt_count"] == 1
        assert item["final_attempt_http_status"] == 200


def test_unified_execution_langfuse_run_experiment_failure_marks_launch_failed(client):
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "Langfuse Error Test",
        },
    )
    assert r_create.status_code == 201
    launch_id = r_create.json()["id"]

    mock_dataset = MagicMock()
    mock_dataset.run_experiment.side_effect = RuntimeError("Langfuse experiment failed to start")
    mock_lf = MagicMock()
    mock_lf.get_dataset.return_value = mock_dataset

    with patch("app.execution.get_langfuse_client_safe", return_value=mock_lf):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200
        run_data = r_run.json()
        assert run_data["status"] == "FAILED"
        assert run_data["quality_conclusion"] == "fail"
        assert run_data["langfuse_sync_status"] == "FAILED"
        assert "failed to start" in run_data["langfuse_sync_error"]

