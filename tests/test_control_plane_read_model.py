from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.evaluators import default_evaluator_registry  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


def test_evaluator_registry_list_specs_public():
    specs = default_evaluator_registry.list_specs()
    assert isinstance(specs, list)
    assert len(specs) >= 4
    spec_map = {s["id"]: s for s in specs}
    assert "intent_match" in spec_map
    assert spec_map["intent_match"]["scope"] == "item"
    assert spec_map["intent_match"]["threshold"] == 1.0
    assert "description" in spec_map["intent_match"]
    assert len(spec_map["intent_match"]["description"]) > 0


def test_api_evaluators_endpoint(client):
    res = client.get("/api/v1/evaluators")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    ids = [item["id"] for item in data]
    assert "intent_match" in ids
    assert "pii_safe" in ids
    for item in data:
        assert "id" in item
        assert "version" in item
        assert "scope" in item
        assert "threshold" in item
        assert "description" in item


def test_system_info_endpoint(client, monkeypatch):
    monkeypatch.setenv("ARGUS_ENVIRONMENT", "staging")
    monkeypatch.setenv("ARGUS_BUILD_ID", "commit-abc1234")
    res = client.get("/api/v1/system/info")
    assert res.status_code == 200
    info = res.json()
    assert info["service"] == "argus-control-plane"
    assert "version" in info
    assert info["environment"] == "staging"
    assert info["build_id"] == "commit-abc1234"


def test_agent_summary_read_model(client):
    # Register an agent
    agent_id = "test-agent-summary"
    r1 = client.post(
        "/api/v1/agents",
        json={"id": agent_id, "name": "Agent Summary Test", "owner": "core-team"},
    )
    assert r1.status_code == 201

    # Before adding versions: version_count=0, latest_version=None
    r_list = client.get("/api/v1/agents")
    assert r_list.status_code == 200
    agents = r_list.json()
    matched = next((a for a in agents if a["id"] == agent_id), None)
    assert matched is not None
    assert matched["version_count"] == 0
    assert matched["latest_version"] is None

    # Create version 1.0.0
    client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": agent_id,
            "version": "1.0.0",
            "endpoint": "http://127.0.0.1:18081/invoke",
            "protocol": "HTTP_JSON",
            "method": "POST",
            "request_mapping": {"query": "input.user_message"},
        },
    )

    # Create version 2.0.0
    client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": agent_id,
            "version": "2.0.0",
            "endpoint": "http://127.0.0.1:18082/invoke",
            "protocol": "HTTP_JSON",
            "method": "POST",
            "request_mapping": {"query": "input.user_message"},
        },
    )

    r_list2 = client.get("/api/v1/agents")
    matched2 = next((a for a in r_list2.json() if a["id"] == agent_id), None)
    assert matched2 is not None
    assert matched2["version_count"] == 2
    assert matched2["latest_version"] == "2.0.0"

    # Archive version 2.0.0; latest active version should fallback to 1.0.0
    client.post(
        "/api/v1/agent-versions/archive",
        json={"agent_id": agent_id, "version": "2.0.0"},
    )
    r_list3 = client.get("/api/v1/agents")
    matched3 = next((a for a in r_list3.json() if a["id"] == agent_id), None)
    assert matched3 is not None
    assert matched3["version_count"] == 2
    assert matched3["latest_version"] == "1.0.0"


def test_item_summary_attempt_metrics(client):
    # 1. Create a launch
    r_create = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "name": "Item Summary Test Launch",
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

    with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_agent_response)):
        r_run = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch_id})
        assert r_run.status_code == 200

    # 2. Query items
    r_items = client.get(f"/api/v1/experiment-launch-items?launch_id={launch_id}")
    assert r_items.status_code == 200
    items = r_items.json()
    assert len(items) > 0
    for item in items:
        assert "attempt_count" in item
        assert item["attempt_count"] >= 1
        assert "final_attempt_http_status" in item
        assert item["final_attempt_http_status"] == 200
        assert "final_attempt_latency_ms" in item
        assert item["final_attempt_latency_ms"] >= 0


def test_experiment_launches_query_filters(client):
    r_all = client.get("/api/v1/experiment-launches?limit=2&offset=0")
    assert r_all.status_code == 200
    data = r_all.json()
    assert isinstance(data, list)
    assert len(data) <= 2
