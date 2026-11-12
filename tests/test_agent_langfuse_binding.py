"""#255 纠偏：一个 Agent 配置一次，所有版本共享；Launch 保留冻结路由。"""
from __future__ import annotations

import copy

import pytest
from app.manifest import LaunchService
from app.secret_providers import CredentialUnavailable
from test_langfuse_connections import binding

pytest_plugins = ["test_credential_resources"]


def configure(client, connection, agent_id="test-agent"):
    return client.put("/api/v1/agents/langfuse-connection", params={"id": agent_id},
                      json={"langfuse_connection": connection})


def launch(registry, version="v1", **kwargs):
    return LaunchService(registry.db_manager, registry).create_launch(
        "test-agent", version, "bound-dataset", evaluator_ids=["intent_match"],
        dataset_snapshot={"source": "langfuse", "dataset_id": "ds", "dataset_version": "2026-11-12T00:00:00Z",
                          "items_count": 1, "snapshot_digest": "fixture",
                          "items": [{"id": "one", "input": {}, "expected_output": {"intent": "ok"}}]}, **kwargs)


def test_agent_binding_requires_admin_and_is_visible_in_agent_usage(credentials):
    client, _, registry, connection = binding(credentials)
    client.headers.pop("Authorization")
    assert configure(client, connection).status_code == 401
    assert registry.get_agent("test-agent").langfuse_connection is None
    client.headers["Authorization"] = "Bearer wrong"
    assert configure(client, connection).status_code == 403
    client.headers["Authorization"] = "Bearer example-admin-token"
    response = configure(client, connection)
    assert response.status_code == 200, response.text
    assert response.json()["langfuse_connection"] == connection
    assert client.get("/api/v1/agents", params={"id": "test-agent"}).json()["langfuse_connection"] == connection
    assert "sk-example" not in response.text
    usage = client.get(f"/api/v1/credentials/{connection['credential_id']}/usage").json()
    assert usage["agents"] == [{"agent_id": "test-agent", "is_current": True}]
    assert usage["versions"] == []
    assert client.delete(f"/api/v1/credentials/{connection['credential_id']}",
                         params={"confirm_name": "project-b"}).status_code == 409
    assert client.post(f"/api/v1/credentials/{connection['credential_id']}/disable",
                       json={"confirm_name": "project-b"}).status_code == 409
    assert client.post(f"/api/v1/credentials/{connection['credential_id']}/disable",
                       json={"confirm_name": "project-b", "force": True}).status_code == 200


def test_all_agent_versions_use_one_binding_without_version_configuration(credentials, monkeypatch):
    client, _, registry, connection = binding(credentials)
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    before = registry.get_version("test-agent", "v1").spec_digest
    assert configure(client, connection).status_code == 200
    client.headers.pop("Authorization")
    response = client.post("/api/v1/agent-versions", json={"agent_id": "test-agent", "version": "v-new",
                          "endpoint": "https://agent.example/invoke"})
    assert response.status_code == 201, response.text
    assert "langfuse_connection" not in response.json()
    for version in ("v1", "v-new"):
        created = launch(registry, version)
        assert created.manifest["langfuse_connection"] == {**connection, "source": "BOUND", "credential_version": 1}
        assert registry.get_version("test-agent", version).langfuse_connection is None
    assert registry.get_version("test-agent", "v1").spec_digest == before


def test_rebinding_agent_changes_future_launches_but_not_history(credentials, monkeypatch):
    client, _, registry, connection = binding(credentials)
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    assert configure(client, connection).status_code == 200
    original = launch(registry, idempotency_key="same-request")
    frozen = copy.deepcopy(original.manifest)
    _, _, _, next_connection = binding(credentials, "project-c")
    assert configure(client, next_connection).status_code == 200
    newer = launch(registry)
    assert newer.manifest["langfuse_connection"]["project_id"] == "project-c"
    assert launch(registry, idempotency_key="same-request").id == original.id
    assert LaunchService(registry.db_manager, registry).get_launch(original.id).manifest == frozen
    from app.langfuse_connection import resolve_manifest_connection
    assert resolve_manifest_connection(registry.db_manager, frozen).project_id == "project-b"
    usage = client.get(f"/api/v1/credentials/{connection['credential_id']}/usage").json()
    assert usage["agents"] == [{"agent_id": "test-agent", "is_current": False}]
    assert client.delete(f"/api/v1/credentials/{connection['credential_id']}",
                         params={"confirm_name": "project-b"}).status_code == 409
    client.headers.pop("Authorization")
    assert configure(client, None).status_code == 401
    client.headers["Authorization"] = "Bearer example-admin-token"
    assert configure(client, None).status_code == 200
    assert launch(registry).manifest["langfuse_connection"]["source"] == "GLOBAL_ENV"
    assert LaunchService(registry.db_manager, registry).get_launch(original.id).manifest == frozen


def test_versions_cannot_override_agent_project(credentials):
    client, _, _, connection = binding(credentials)
    response = client.post("/api/v1/agent-versions", json={"agent_id": "test-agent", "version": "override",
        "endpoint": "https://agent.example/invoke", "langfuse_connection": connection})
    assert response.status_code == 422
    assert "sk-example" not in response.text


def test_agent_connection_is_validated_and_launch_environment_stays_scoped(credentials, monkeypatch):
    client, _, registry, connection = binding(credentials)
    for change in ({"base_url": "https://other.example"}, {"base_url": "https://lf.example/path"},
                   {"credential_id": "missing"}, {"project_id": " "}):
        assert configure(client, {**connection, **change}).status_code in (400, 422)
        assert registry.get_agent("test-agent").langfuse_connection is None
    assert configure(client, connection, "missing-agent").status_code == 404
    assert configure(client, connection).status_code == 200
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    registry.create_version("test-agent", "staging", "https://agent.example/invoke", environment="staging")
    with pytest.raises(CredentialUnavailable):
        launch(registry, "staging", environment="staging")


def test_agent_yaml_binding_is_shared_and_version_overrides_are_rejected(credentials, tmp_path, monkeypatch):
    import json

    _, _, registry, connection = binding(credentials)
    path = tmp_path / "agents.yaml"
    path.write_text(json.dumps({"agents": {"yaml-agent": {"langfuse_connection": connection, "versions": {
        "v1": {"endpoint": "https://agent.example/v1"}, "v2": {"endpoint": "https://agent.example/v2"}}}}}))
    assert registry.import_yaml(path)["created"] == ["yaml-agent:v1", "yaml-agent:v2"]
    assert registry.get_agent("yaml-agent").langfuse_connection == connection
    assert registry.get_version("yaml-agent", "v2").langfuse_connection is None
    path.write_text(json.dumps({"agents": {"invalid": {"versions": {"v1": {
        "endpoint": "https://agent.example/v1", "langfuse_connection": connection}}}}}))
    with pytest.raises(ValueError):
        registry.import_yaml(path)
    assert registry.get_agent("invalid") is None


def test_legacy_version_binding_cannot_silently_fall_back_to_global(credentials):
    from app.db_models import AgentVersionRecord
    from sqlalchemy import select

    _, _, registry, connection = binding(credentials)
    with registry.db_manager.get_session() as session:
        version = session.scalar(select(AgentVersionRecord).where(AgentVersionRecord.agent_id == "test-agent",
                                                                 AgentVersionRecord.version == "v1"))
        version.langfuse_connection = connection
    with pytest.raises(ValueError, match="explicit Agent connection"):
        launch(registry)


def test_project_change_while_resolving_dataset_cannot_commit_mixed_launch(credentials, monkeypatch):
    from app.db_models import ExperimentLaunchRecord
    from sqlalchemy import select

    client, _, registry, connection = binding(credentials)
    _, _, _, next_connection = binding(credentials, "project-c")
    assert configure(client, connection).status_code == 200
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)

    def fetch(*_, **__):
        from types import SimpleNamespace
        assert configure(client, next_connection).status_code == 200
        return SimpleNamespace(id="ds-b", items=[])

    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.get_dataset", fetch)
    with pytest.raises(ValueError, match="changed during Launch creation"):
        LaunchService(registry.db_manager, registry).create_launch("test-agent", "v1", "bound-dataset")
    with registry.db_manager.get_session() as session:
        assert session.scalar(select(ExperimentLaunchRecord.id)) is None


def test_register_agent_with_connection_and_delete_retains_no_orphan_refs(credentials):
    client, _, _, connection = binding(credentials)
    payload = {"id": "new-agent", "name": "New Agent", "langfuse_connection": connection}
    client.headers.pop("Authorization")
    assert client.post("/api/v1/agents", json=payload).status_code == 401
    client.headers["Authorization"] = "Bearer example-admin-token"
    response = client.post("/api/v1/agents", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["langfuse_connection"] == connection
    assert client.delete("/api/v1/agents", params={"id": "new-agent"}).status_code == 200
    assert client.get(f"/api/v1/credentials/{connection['credential_id']}/usage").json()["agents"] == []


def test_managed_idempotency_replays_original_project_after_agent_rebinding(credentials, monkeypatch):
    from test_issue_215_managed_evaluation import contract, rules

    client, _, registry, connection = binding(credentials)
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    assert configure(client, connection).status_code == 200
    dataset = launch(registry).manifest["dataset"]
    service = LaunchService(registry.db_manager, registry)
    options = {"evaluation_source": "langfuse_managed", "managed_evaluation": {**contract(), "project_id": "project-b"},
               "quality_policy_rules": rules(), "dataset_snapshot": dataset, "idempotency_key": "managed-replay"}
    original = service.create_launch("test-agent", "v1", "bound-dataset", **options)
    _, _, _, next_connection = binding(credentials, "project-c")
    assert configure(client, next_connection).status_code == 200
    assert service.create_launch("test-agent", "v1", "bound-dataset", **options).id == original.id
