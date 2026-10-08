from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

DASHBOARD = "https://cloud.langfuse.example.com"


class FakeLangfuseClient:
    """Langfuse client double whose Dataset Run identity is explicit."""

    def __init__(self, *, project_ids=("proj-real",), dataset_id="ds-real", run_id="run-real",
                 sdk_run_url="http://langfuse-web:3000/project/poc-project/datasets/banking-agent-regression/runs/run-real",
                 project_error: Exception | None = None):
        self.id = dataset_id
        self.run_id = run_id
        self.sdk_run_url = sdk_run_url
        self.project_ids = project_ids
        self.project_error = project_error
        self._projects = MagicMock()
        self._projects.get.side_effect = self._get_projects
        self.api = SimpleNamespace(projects=self._projects, datasets=SimpleNamespace(get_run=self._get_run))
        self.flushed = 0

    def _get_projects(self, **_kwargs):
        if self.project_error is not None:
            raise self.project_error
        return SimpleNamespace(data=[SimpleNamespace(id=pid) for pid in self.project_ids])

    def _get_run(self, dataset_name, run_name, **_kwargs):
        return SimpleNamespace(id=self.run_id, dataset_id=self.id)

    def get_dataset(self, *_args, **_kwargs):
        client = self

        class _Dataset:
            id = client.id

            async def _noop(self, **_kw):
                return None

            def run_experiment(self, *, name, task, **_kwargs):
                async def _drive():
                    for i in range(1, 7):
                        item = SimpleNamespace(
                            id=f"00000000-0000-4000-8000-00000000000{i}",
                            input={"user_message": "test"},
                            expected_output={"expected_intent": "transaction_investigation"},
                        )
                        await task(item=item)

                async def _runner():
                    await _drive()
                    return SimpleNamespace(
                        experiment_id=client.run_id,
                        dataset_run_id=client.run_id,
                        dataset_run_url=client.sdk_run_url,
                    )

                return _runner()

        return _Dataset()

    def flush(self):
        self.flushed += 1


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

    lf = FakeLangfuseClient()
    patched_settings = dataclasses.replace(settings, argus_langfuse_dashboard_url=DASHBOARD)

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)), \
         patch("app.execution.get_langfuse_client_safe", return_value=lf), \
         patch("app.execution.settings", patched_settings):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200
        run_data = r_run.json()
        assert run_data["status"] == "COMPLETED"
        assert run_data["langfuse_sync_status"] == "SYNCED"
        assert run_data["langfuse_experiment_id"] == "run-real"
        # Public dashboard identity: real project id, real dataset id, no internal host.
        assert run_data["langfuse_experiment_url"] == (
            f"{DASHBOARD}/project/proj-real/datasets/ds-real/runs/run-real"
        )
        assert run_data["links"]["langfuse_experiment"] == run_data["langfuse_experiment_url"]
        assert "langfuse-web" not in run_data["langfuse_experiment_url"]
        assert "poc-project" not in run_data["langfuse_experiment_url"]
        assert "banking-agent-regression" not in run_data["langfuse_experiment_url"]

    # Check that item executions and attempts are saved in DB
    r_items = client.get(f"/api/v1/experiment-launch-items?launch_id={launch_id}")
    assert r_items.status_code == 200
    items = r_items.json()
    assert len(items) == 6
    for item in items:
        assert item["execution_status"] == "succeeded"
        assert item["attempt_count"] == 1
        assert item["final_attempt_http_status"] == 200


def test_unified_execution_keeps_synced_when_link_resolution_fails(client):
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "Link Failure Test",
        },
    )
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
    # Ambiguous project identity: the resolver must refuse to guess.
    lf = FakeLangfuseClient(project_ids=("proj-a", "proj-b"))
    patched_settings = dataclasses.replace(settings, argus_langfuse_dashboard_url=DASHBOARD)

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)), \
         patch("app.execution.get_langfuse_client_safe", return_value=lf), \
         patch("app.execution.settings", patched_settings):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200
        run_data = r_run.json()

    assert run_data["status"] == "COMPLETED"
    assert run_data["langfuse_sync_status"] == "SYNCED"
    assert run_data["langfuse_experiment_id"] == "run-real"
    # A link failure is not a sync failure, and the internal host URL is never persisted.
    assert run_data["langfuse_experiment_url"] is None
    assert run_data["links"]["langfuse_experiment"] is None
    assert run_data["langfuse_sync_error"] is None


def test_unified_execution_without_dashboard_does_not_persist_an_sdk_link(client):
    """未配置 dashboard 时不落 SDK 链接，补偿器才能在配置后重建。

    SDK 的 `dataset_run_url` 由 LANGFUSE_BASE_URL 派生：自托管部署里它是容器内地址
    （http://langfuse-web:3000/...），浏览器根本打不开。更关键的是，一旦落库，补链
    只挑 URL 为 NULL 的 launch、且对已有安全 URL 直接返回 UNCHANGED，这个链接就会
    永久留下，注释承诺的"配置 dashboard 后重建"永远不会发生。
    """
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "No Dashboard Test",
        },
    )
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
    lf = FakeLangfuseClient(sdk_run_url="http://langfuse-web:3000/project/poc-project/datasets/ds/runs/run-real")
    patched_settings = dataclasses.replace(settings, argus_langfuse_dashboard_url=None)

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)), \
         patch("app.execution.get_langfuse_client_safe", return_value=lf), \
         patch("app.execution.settings", patched_settings):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200
        run_data = r_run.json()

    assert run_data["langfuse_sync_status"] == "SYNCED"
    # 真实 Run ID 必须保留：这是配置 dashboard 后无重跑补齐链接的全部依据
    assert run_data["langfuse_experiment_id"] == "run-real"
    assert run_data["langfuse_experiment_url"] is None
    assert run_data["links"]["langfuse_experiment"] is None


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
        assert run_data["quality_conclusion"] == "unknown"
        assert run_data["langfuse_sync_status"] == "FAILED"
        assert "failed to start" in run_data["langfuse_sync_error"]


