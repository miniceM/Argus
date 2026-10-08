from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from app import main
from app.credentials import CredentialService
from app.db_models import CredentialSecretRecord, ExecutionAttemptRecord, ExperimentItemExecutionRecord
from app.registry import AgentRegistry
from app.secret_providers import CredentialUnavailable
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture
def credentials(setup_runtime, tmp_path, monkeypatch):
    key = tmp_path / "keyring.json"
    key.write_text(json.dumps({"active_key_id": "key-1", "keys": {"key-1": base64.b64encode(b"a" * 32).decode()}}))
    key.chmod(0o600)
    admin = tmp_path / "admin-token"
    admin.write_text("example-admin-token")
    admin.chmod(0o600)
    monkeypatch.setenv("ARGUS_MANAGED_SECRET_KEY_FILE", str(key))
    monkeypatch.setenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE", str(admin))
    monkeypatch.setattr(main, "db_manager", setup_runtime[0])
    registry = AgentRegistry(setup_runtime[0])
    monkeypatch.setattr(main, "registry", registry)
    client = TestClient(main.app)
    client.headers["Authorization"] = "Bearer example-admin-token"
    return client, CredentialService(setup_runtime[0]), registry, key


def create(client, **changes):
    payload = {"name": "生产 Agent", "environment": "production", "provider": "managed", "secret": "example-first-token"}
    payload.update(changes)
    return client.post("/api/v1/credentials", json=payload)


def test_managed_lifecycle_never_returns_or_stores_plaintext(credentials, setup_runtime):
    client, service, registry, _ = credentials
    response = create(client)
    assert response.status_code == 201, response.text
    record = response.json()
    assert "example-first-token" not in response.text and "secret" not in record
    credential_id = record["id"]
    registry.create_version("test-agent", "secured", "https://agent.example/invoke", credential_id=credential_id, environment="production")
    assert client.get(f"/api/v1/credentials/{credential_id}/usage").json()["versions"][0]["agent_id"] == "test-agent"
    resolved = service.resolve(credential_id)
    assert resolved.token == "example-first-token" and resolved.version == 1
    assert "example-first-token" not in repr(resolved)
    rotated = client.post(f"/api/v1/credentials/{credential_id}/rotate", json={"secret": "example-second-token"})
    assert rotated.status_code == 200 and rotated.json()["version"] == 2
    assert service.resolve(credential_id).token == "example-second-token"
    assert registry.get_version("test-agent", "secured").credential_id == credential_id
    assert client.delete(f"/api/v1/credentials/{credential_id}", params={"confirm_name": record["name"]}).status_code == 409
    assert client.post(f"/api/v1/credentials/{credential_id}/disable", json={"confirm_name": record["name"]}).status_code == 409
    assert client.post(f"/api/v1/credentials/{credential_id}/disable", json={"confirm_name": record["name"], "force": True}).status_code == 200
    with pytest.raises(CredentialUnavailable):
        service.resolve(credential_id)
    with setup_runtime[0].get_session() as session:
        rows = session.scalars(select(CredentialSecretRecord)).all()
        assert len(rows) == 2
        assert all("example-" not in json.dumps(row.envelope) for row in rows)
    contents = Path(setup_runtime[0].engine.url.database).read_bytes()
    assert b"example-first-token" not in contents and b"example-second-token" not in contents


def test_sensitive_writes_require_admin_authorization_and_configured_key(credentials, monkeypatch):
    client, _, _, _ = credentials
    client.headers.pop("Authorization")
    assert create(client).status_code == 401
    client.headers["Authorization"] = "Bearer wrong"
    assert create(client).status_code == 403
    client.headers["Authorization"] = "Bearer example-admin-token"
    monkeypatch.delenv("ARGUS_MANAGED_SECRET_KEY_FILE")
    assert create(client).status_code == 503
    monkeypatch.delenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE")
    assert create(client).status_code == 503


@pytest.mark.parametrize("value", ["", "two parts", "line\nbreak", {"private": "never-echo-this"}])
def test_invalid_secret_errors_never_echo_the_input(credentials, value):
    client = credentials[0]
    response = create(client, secret=value)
    assert response.status_code == 422
    assert "never-echo-this" not in response.text and "two parts" not in response.text


def test_wrong_environment_and_disabled_credentials_cannot_be_bound(credentials):
    client, _, registry, _ = credentials
    record = create(client).json()
    with pytest.raises(ValueError):
        registry.create_version("test-agent", "wrong-env", "https://agent.example/invoke", credential_id=record["id"], environment="staging")
    assert client.post(f"/api/v1/credentials/{record['id']}/disable", json={"confirm_name": record["name"]}).status_code == 200
    with pytest.raises(ValueError):
        registry.create_version("test-agent", "disabled", "https://agent.example/invoke", credential_id=record["id"], environment="production")


def test_master_key_rotation_rewraps_without_changing_secret_versions(credentials, monkeypatch):
    client, service, _, path = credentials
    record = create(client).json()
    keys = json.loads(path.read_text())
    keys["keys"]["key-2"] = base64.b64encode(b"b" * 32).decode()
    keys["active_key_id"] = "key-2"
    path.write_text(json.dumps(keys))
    assert client.post("/api/v1/credentials/rewrap").status_code == 200
    keys["keys"].pop("key-1")
    path.write_text(json.dumps(keys))
    resolved = service.resolve(record["id"])
    assert resolved.token == "example-first-token" and resolved.version == 1
    path.chmod(0o644)
    with pytest.raises(CredentialUnavailable):
        service.resolve(record["id"])


def test_credential_failure_finishes_worker_before_dispatch(setup_runtime, monkeypatch):
    from test_issue_49_cross_issue_acceptance import _binding, _manifest, _policy

    manager, queue, _, orchestrator, worker, reconciler = setup_runtime
    binding = _binding("intent_match")
    manifest = _manifest(binding.to_payload(), policy_payload=_policy([binding]).to_payload())
    manifest["agent"]["credential_ref"] = "env://DEMO_AUTH_TOKEN"
    monkeypatch.delenv("DEMO_AUTH_TOKEN", raising=False)
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", "credential-failure", manifest)
    orchestrator.start_launch(launch.id)
    message = queue.read_group("credential-failure", count=1)[0]
    with patch("app.worker.get_langfuse_client_safe", return_value=None), patch("app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock) as invoke:
        assert asyncio.run(worker.execute_item_message(*message))
        assert invoke.await_count == 0
    reconciler.reconcile_launch_states()
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        attempt = session.scalars(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id == item.id)).one()
        assert item.execution_status == "failed" and "CREDENTIAL_UNAVAILABLE" in item.execution_error
        assert attempt.status == "FAILED" and attempt.request_phase == "PREPARED"
        assert attempt.error_type == "CREDENTIAL_UNAVAILABLE"
        assert item.lease_token is None


def test_authenticated_real_http_worker_uses_rotation_without_rewriting_manifest(credentials, setup_runtime, monkeypatch):
    """完整冻结 Manifest → Worker → 普通 HTTP Agent → Evaluator；不 mock 调用。"""
    import copy
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.db_models import ExperimentLaunchRecord
    from app.manifest import LaunchService
    from app.result_snapshots import create_result_snapshot

    client, service, registry, _ = credentials
    manager, queue, _, orchestrator, worker, reconciler = setup_runtime
    monkeypatch.delenv("ARGUS_VAULT_ADDR", raising=False)
    monkeypatch.delenv("ARGUS_VAULT_ENABLED", raising=False)
    expected = ["example-first-token"]
    calls = []

    class BusinessAgent(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.headers["Authorization"], self.headers["traceparent"], body))
            ok = self.headers["Authorization"] == "Bearer " + expected[0]
            response = json.dumps({"intent": "refund"} if ok else {"error": "unauthorized"}).encode()
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), BusinessAgent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        credential_id = create(client).json()["id"]
        version = registry.create_version("test-agent", "http-secured", f"http://127.0.0.1:{server.server_port}/invoke", credential_id=credential_id, environment="production", max_retries=0, request_mapping={"query": "input.text"})
        svc = LaunchService(manager, registry)
        launch = svc.create_launch(agent_id="test-agent", agent_version="http-secured", dataset_name="test", evaluator_ids=["intent_match"], dataset_snapshot={
            "dataset_id": "ds", "dataset_version": "v1", "content_digest": "sha256:fixture", "items": [{"id": "1", "input": {"text": "退款"}, "expected_output": {"expected_intent": "refund"}}],
        })
        frozen = copy.deepcopy(launch.manifest)
        assert frozen["agent"]["credential_id"] == credential_id and "example-first-token" not in json.dumps(frozen)
        # 在冻结后、派发前轮换：逻辑身份与规格摘要不变，调用使用最新修订。
        assert client.post(f"/api/v1/credentials/{credential_id}/rotate", json={"secret": "example-second-token"}).status_code == 200
        expected[0] = "example-second-token"
        orchestrator.start_launch(launch.id)
        message = queue.read_group("managed-real-http", count=1)[0]
        with patch("app.worker.get_langfuse_client_safe", return_value=None):
            assert asyncio.run(worker.execute_item_message(*message))
        reconciler.reconcile_launch_states()
        assert len(calls) == 1 and calls[0][0] == "Bearer example-second-token"
        assert calls[0][1].startswith("00-") and calls[0][2] == {"query": "退款"}
        with manager.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, message[1])
            attempt = session.scalars(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id == item.id)).one()
            assert item.execution_status == "succeeded" and item.quality_conclusion == "pass"
            assert (attempt.credential_id, attempt.credential_version, attempt.credential_provider) == (credential_id, 2, "managed")
            persisted = session.get(ExperimentLaunchRecord, launch.id)
            assert persisted.manifest == frozen
            create_result_snapshot(session, persisted)
            session.commit()
        assert registry.get_version("test-agent", "http-secured").spec_digest == version.spec_digest
        assert service.resolve(credential_id).version == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ciphertext_is_bound_to_identity_and_fails_closed_on_key_loss(credentials):
    from app.credentials import ManagedSecretProvider

    client, service, _, key = credentials
    record = create(client).json()
    provider = ManagedSecretProvider()
    envelope = provider.encrypt(record["id"], 1, "example-first-token")
    for identity, version in [("other-credential", 1), (record["id"], 2)]:
        with pytest.raises(CredentialUnavailable):
            provider.decrypt(identity, version, envelope)
    envelope["ciphertext"] = base64.b64encode(b"tampered").decode()
    with pytest.raises(CredentialUnavailable):
        provider.decrypt(record["id"], 1, envelope)
    key.unlink()
    with pytest.raises(CredentialUnavailable):
        service.resolve(record["id"])


def test_unused_credential_delete_keeps_redacted_audit(credentials, setup_runtime):
    from app.db_models import CredentialAuditRecord

    client = credentials[0]
    record = create(client).json()
    assert client.delete(f"/api/v1/credentials/{record['id']}", params={"confirm_name": "wrong"}).status_code == 400
    assert client.delete(f"/api/v1/credentials/{record['id']}", params={"confirm_name": record["name"]}).status_code == 200
    with setup_runtime[0].get_session() as session:
        assert session.scalars(select(CredentialSecretRecord)).all() == []
        rows = session.scalars(select(CredentialAuditRecord)).all()
        assert {row.action for row in rows} == {"CREATE", "DELETE"}
        assert all(row.actor.startswith("credential-admin:") and "example-" not in row.actor for row in rows)


def test_corrupted_envelope_aborts_key_rewrap_without_partial_updates(credentials, setup_runtime):
    client, _, _, path = credentials
    create(client)
    second = create(client, name="第二个凭据").json()
    with setup_runtime[0].get_session() as session:
        row = session.get(CredentialSecretRecord, (second["id"], 1))
        row.envelope = {"algorithm": "AES-256-GCM"}
        session.commit()
    keys = json.loads(path.read_text())
    keys["keys"]["key-2"] = base64.b64encode(b"b" * 32).decode()
    keys["active_key_id"] = "key-2"
    path.write_text(json.dumps(keys))
    response = client.post("/api/v1/credentials/rewrap")
    assert response.status_code == 503
    with setup_runtime[0].get_session() as session:
        rows = session.scalars(select(CredentialSecretRecord)).all()
        assert rows[0].envelope["key_id"] == "key-1"


@pytest.mark.parametrize("vault_version", [7, None])
def test_optional_vault_resource_hides_mapping_and_records_real_kv_revision(credentials, tmp_path, monkeypatch, vault_version):
    import httpx
    from app.secret_providers import VaultKubernetesProvider

    client, service, _, _ = credentials
    monkeypatch.setenv("ARGUS_VAULT_ENABLED", "true")
    monkeypatch.setenv("ARGUS_VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("ARGUS_VAULT_ROLE", "argus-worker")
    jwt = tmp_path / "workload-jwt"
    jwt.write_text("example-service-account-jwt")
    monkeypatch.setenv("ARGUS_VAULT_JWT_PATH", str(jwt))
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"auth": {"client_token": "example-short-lived-vault-token"}})
        return httpx.Response(200, json={"data": {"data": {"token": "example-vault-agent-token"}, "metadata": {"version": vault_version}}})
    monkeypatch.setattr(VaultKubernetesProvider, "_transport", httpx.MockTransport(handler))
    response = create(client, provider="vault", secret=None, provider_ref="vault://secret/agents/private#token")
    assert response.status_code == 201
    assert "private" not in response.text and "provider_ref" not in response.json()
    if vault_version is None:
        with pytest.raises(CredentialUnavailable):
            service.resolve(response.json()["id"])
    else:
        resolved = service.resolve(response.json()["id"])
        assert resolved.token == "example-vault-agent-token" and resolved.version == 7
    monkeypatch.delenv("ARGUS_VAULT_ENABLED")
    assert create(client, provider="vault", secret=None, provider_ref="vault://secret/agents/private#token").status_code == 503
    with pytest.raises(CredentialUnavailable):
        service.resolve(response.json()["id"])


@pytest.mark.parametrize("http_statuses", [[200], [401], [500, 200]])
def test_sync_api_attempts_record_the_exact_managed_revision(credentials, setup_runtime, monkeypatch, http_statuses):
    import httpx
    from app.manifest import LaunchService

    client, _, registry, _ = credentials
    manager = setup_runtime[0]
    credential_id = create(client).json()["id"]
    registry.create_version("test-agent", "sync-secured", "https://agent.example/invoke", credential_id=credential_id,
                            environment="production", max_retries=len(http_statuses) - 1)
    svc = LaunchService(manager, registry)
    monkeypatch.setattr(main, "launch_service", svc)
    monkeypatch.setattr(main, "orchestrator", setup_runtime[3])
    launch = svc.create_launch(agent_id="test-agent", agent_version="sync-secured", dataset_name="test", evaluator_ids=["intent_match"], dataset_snapshot={
        "source": "seed", "dataset_id": "ds", "dataset_version": "v1", "items": [{"id": "1", "input": {}, "expected_output": {"expected_intent": "refund"}}],
    })
    assert client.post(f"/api/v1/credentials/{credential_id}/rotate", json={"secret": "example-second-token"}).status_code == 200
    responses = [httpx.Response(code, json={"intent": "refund"}) for code in http_statuses]
    with patch("app.execution.get_langfuse_client_safe", return_value=None), patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=responses) as invoke:
        response = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch.id})
    assert response.status_code == 200, response.text
    assert invoke.await_count == len(http_statuses)
    assert all(call.kwargs["headers"]["Authorization"] == "Bearer example-second-token" for call in invoke.await_args_list)
    with manager.get_session() as session:
        attempts = session.scalars(select(ExecutionAttemptRecord).join(ExperimentItemExecutionRecord, ExecutionAttemptRecord.item_execution_id == ExperimentItemExecutionRecord.id).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        assert len(attempts) == len(http_statuses)
        assert all((attempt.credential_id, attempt.credential_version, attempt.credential_provider) == (credential_id, 2, "managed") for attempt in attempts)


def test_rewrap_audits_each_historical_secret_revision(credentials, setup_runtime):
    from app.db_models import CredentialAuditRecord

    client, _, _, path = credentials
    credential_id = create(client).json()["id"]
    assert client.post(f"/api/v1/credentials/{credential_id}/rotate", json={"secret": "example-second-token"}).status_code == 200
    keys = json.loads(path.read_text())
    keys["keys"]["key-2"] = base64.b64encode(b"b" * 32).decode()
    keys["active_key_id"] = "key-2"
    path.write_text(json.dumps(keys))
    assert client.post("/api/v1/credentials/rewrap").json()["rewrapped"] == 2
    with setup_runtime[0].get_session() as session:
        rows = session.scalars(select(CredentialAuditRecord).where(CredentialAuditRecord.action == "REWRAP")).all()
        assert sorted(row.version for row in rows) == [1, 2]


@pytest.mark.parametrize("langfuse", [False, True])
@pytest.mark.parametrize("change", ["item_rotate", "retry_rotate", "item_disable", "retry_disable"])
def test_sync_api_refreshes_credentials_for_each_future_attempt(credentials, setup_runtime, monkeypatch, langfuse, change):
    from types import SimpleNamespace

    import httpx
    from app.manifest import LaunchService

    client, service, registry, _ = credentials
    manager = setup_runtime[0]
    record = create(client).json()
    retry = change.startswith("retry")
    registry.create_version("test-agent", "sync-fresh", "https://agent.example/invoke", credential_id=record["id"],
                            environment="production", max_retries=1 if retry else 0, max_concurrency=1)
    svc = LaunchService(manager, registry)
    monkeypatch.setattr(main, "launch_service", svc)
    monkeypatch.setattr(main, "orchestrator", setup_runtime[3])
    items = [{"id": str(index), "input": {}, "expected_output": {"expected_intent": "refund"}}
             for index in range(1 if retry else 2)]
    launch = svc.create_launch(agent_id="test-agent", agent_version="sync-fresh", dataset_name="test", evaluator_ids=["intent_match"], dataset_snapshot={
        "source": "seed", "dataset_id": "ds", "dataset_version": "v1", "items": items,
    })

    class Dataset:
        async def run_experiment(self, *, task, **kwargs):
            for item in items:
                await task(item=SimpleNamespace(**item))
            return SimpleNamespace()

    fake_lf = SimpleNamespace(get_dataset=lambda *args, **kwargs: Dataset()) if langfuse else None
    sent = []

    async def invoke(*args, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        if len(sent) == 1:
            if change.endswith("rotate"):
                service.rotate(record["id"], "example-second-token", "credential-admin:test")
            else:
                assert client.post(f"/api/v1/credentials/{record['id']}/disable", json={"confirm_name": record["name"], "force": True}).status_code == 200
        return httpx.Response(500 if retry and len(sent) == 1 else 200, json={"intent": "refund"})

    with patch("app.execution.get_langfuse_client_safe", return_value=fake_lf), patch("httpx.AsyncClient.post", side_effect=invoke):
        response = client.post("/api/v1/experiment-launches/run", json={"launch_id": launch.id})
    assert response.status_code == 200, response.text
    expected = ["Bearer example-first-token"]
    if change.endswith("rotate"):
        expected.append("Bearer example-second-token")
    assert sent == expected
    with manager.get_session() as session:
        attempts = session.scalars(select(ExecutionAttemptRecord).join(ExperimentItemExecutionRecord, ExecutionAttemptRecord.item_execution_id == ExperimentItemExecutionRecord.id).where(ExperimentItemExecutionRecord.launch_id == launch.id).order_by(ExecutionAttemptRecord.started_at)).all()
        assert [attempt.credential_version for attempt in attempts] == ([1, 2] if change.endswith("rotate") else [1])
        assert all(attempt.credential_id == record["id"] and attempt.credential_provider == "managed" for attempt in attempts)
        if change.endswith("disable"):
            rows = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
            assert any(item.execution_status == "failed" for item in rows)
