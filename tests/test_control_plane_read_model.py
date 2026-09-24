from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

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
    assert spec_map["intent_match"]["default_selected"] is True
    assert spec_map["intent_match"]["composed_of"] == []
    assert spec_map["overall_pass"]["default_selected"] is False
    assert set(spec_map["overall_pass"]["composed_of"]) == {
        "escalation_match",
        "intent_match",
        "pii_safe",
        "required_tool_match",
    }


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
        assert "default_selected" in item
        assert "composed_of" in item
        assert isinstance(item["default_selected"], bool)
        assert isinstance(item["composed_of"], list)


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


def test_agent_launch_summary_matches_launch_list_api(client):
    from app.db_models import ExperimentLaunchRecord
    from app.main import db_manager
    from sqlalchemy import delete

    agent_id = f"issue-23-api-{uuid4().hex}"
    agent_name = "Issue 23 API Cross-check"
    created = False
    try:
        agent_response = client.post(
            "/api/v1/agents",
            json={"id": agent_id, "name": agent_name},
        )
        assert agent_response.status_code == 201
        created = True

        version_response = client.post(
            "/api/v1/agent-versions",
            json={
                "agent_id": agent_id,
                "version": "v1",
                "endpoint": "http://127.0.0.1:18081/invoke",
            },
        )
        assert version_response.status_code == 201
        version_id = version_response.json()["id"]

        statuses = ("SUCCEEDED", "SUCCEEDED", "PENDING", "PARTIAL_FAILED", "COMPLETED")
        with db_manager.get_session() as session:
            session.add_all(
                [
                    ExperimentLaunchRecord(
                        id=f"issue-23-api-launch-{index}",
                        name=f"Issue 23 API Launch {index}",
                        agent_id=agent_id,
                        agent_version="v1",
                        agent_version_id=version_id,
                        dataset_name="banking-reg",
                        status=status,
                        manifest={},
                    )
                    for index, status in enumerate(statuses)
                ]
            )
            session.commit()

        agent_detail = client.get(f"/api/v1/agents?id={agent_id}")
        agent_list = client.get("/api/v1/agents")
        launch_list = client.get(f"/api/v1/experiment-launches?agent_id={agent_id}")
        assert agent_detail.status_code == agent_list.status_code == launch_list.status_code == 200

        detail_summary = agent_detail.json()
        list_summary = next(item for item in agent_list.json() if item["id"] == agent_id)
        launches = launch_list.json()
        assert len(launches) == detail_summary["launch_count"] == list_summary["launch_count"] == 5

        terminal_statuses = {"SUCCEEDED", "COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}
        launch_list_active_count = sum(launch["status"] not in terminal_statuses for launch in launches)
        assert launch_list_active_count == detail_summary["active_launch_count"] == list_summary["active_launch_count"] == 1
        assert [launch["status"] for launch in launches].count("PENDING") == 1
    finally:
        if created:
            with db_manager.get_session() as session:
                session.execute(delete(ExperimentLaunchRecord).where(ExperimentLaunchRecord.agent_id == agent_id))
                session.commit()
            cleanup_response = client.delete(f"/api/v1/agents?id={agent_id}")
            assert cleanup_response.status_code == 200


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


def test_langfuse_dashboard_url_validation_contract():
    import app.config as config

    validator = getattr(config, "validate_langfuse_dashboard_url", None)
    assert callable(validator), "dashboard URL validator must be implemented in app.config"

    accepted = {
        " http://localhost:13000 ": "http://localhost:13000",
        "https://observability.example.com/langfuse": "https://observability.example.com/langfuse",
        "https://observability.example.com/langfuse/v1/": "https://observability.example.com/langfuse/v1/",
        "https://observability.example.com/%E4%B8%AD%E6%96%87/": "https://observability.example.com/%E4%B8%AD%E6%96%87/",
        "http://127.0.0.1:8080": "http://127.0.0.1:8080",
        "http://[::1]:8080/langfuse": "http://[::1]:8080/langfuse",
    }
    for value, expected in accepted.items():
        assert validator(value) == expected

    rejected = [
        None,
        "",
        "   ",
        "/langfuse",
        "//observability.example.com/langfuse",
        "javascript:alert(1)",
        "ftp://observability.example.com",
        "http:///langfuse",
        "http://user:password@observability.example.com",
        "https://observability.example.com/path?token=secret",
        "https://observability.example.com/path#fragment",
        "https://observability.example.com:invalid",
        "https://observability.example.com:65536",
        "https://observability.example.com:/path",
        "https://[not-ipv6]:8080/path",
        "https://observability.example.com/path with space",
        "https://observability.example.com/path\\suffix",
        "https://observability.example.com/path\nmalicious",
    ]
    for value in rejected:
        assert validator(value) is None, f"expected URL to be rejected: {value!r}"


def test_invalid_dashboard_url_logs_without_disclosing_configured_value(monkeypatch, caplog):
    import logging

    import app.config as config

    secret_url = "https://user:top-secret@observability.example.com/?token=top-secret"
    monkeypatch.setenv("ARGUS_LANGFUSE_DASHBOARD_URL", secret_url)
    caplog.set_level(logging.WARNING, logger="app.config")

    assert config._load_langfuse_dashboard_url() is None
    assert "ARGUS_LANGFUSE_DASHBOARD_URL" in caplog.text
    assert secret_url not in caplog.text
    assert "top-secret" not in caplog.text


def test_system_info_exposes_only_validated_langfuse_dashboard_url(client, monkeypatch):
    from types import SimpleNamespace

    import app.api_system as api_system

    monkeypatch.setattr(
        api_system,
        "settings",
        SimpleNamespace(
            runner_version="0.2.0",
            environment="staging",
            build_id="commit-abc1234",
            langfuse_base_url="http://langfuse-web:3000",
            argus_langfuse_dashboard_url="https://observability.example.com/langfuse/",
        ),
    )

    res = client.get("/api/v1/system/info")
    assert res.status_code == 200
    info = res.json()
    assert info == {
        "service": "argus-control-plane",
        "version": "0.2.0",
        "build_id": "commit-abc1234",
        "environment": "staging",
        "langfuse_dashboard_url": "https://observability.example.com/langfuse/",
    }


def test_system_info_dashboard_url_is_null_when_unconfigured_even_if_internal_url_exists(client, monkeypatch):
    from types import SimpleNamespace

    import app.api_system as api_system

    monkeypatch.setattr(
        api_system,
        "settings",
        SimpleNamespace(
            runner_version="0.2.0",
            environment="local",
            build_id="dev",
            langfuse_base_url="http://langfuse-web:3000",
            argus_langfuse_dashboard_url=None,
        ),
    )

    res = client.get("/api/v1/system/info")
    assert res.status_code == 200
    assert res.json()["langfuse_dashboard_url"] is None
    assert res.json()["environment"] == "local"
