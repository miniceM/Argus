from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from app.executor import RemoteAgentExecutor
from app.registry import AgentVersionSpec
from app.security import resolve_credential, validate_credential_ref


def test_environment_credentials_require_explicit_development_mode(monkeypatch):
    monkeypatch.delenv("ARGUS_SECRET_MODE", raising=False)
    monkeypatch.setenv("ARGUS_DB_MODE", "prod")
    monkeypatch.setenv("DEMO_AUTH_TOKEN", "example-agent-token")
    with pytest.raises(ValueError, match="development"):
        validate_credential_ref("env://DEMO_AUTH_TOKEN")
    with pytest.raises(ValueError, match="development"):
        resolve_credential("env://DEMO_AUTH_TOKEN")


def test_explicit_development_mode_keeps_whitelisted_environment_support(monkeypatch):
    monkeypatch.setenv("ARGUS_DB_MODE", "prod")
    monkeypatch.setenv("ARGUS_SECRET_MODE", "development")
    monkeypatch.setenv("DEMO_AUTH_TOKEN", "example-agent-token")
    assert resolve_credential("env://DEMO_AUTH_TOKEN") == "example-agent-token"
    with pytest.raises(ValueError, match="whitelist"):
        resolve_credential("env://UNLISTED_TOKEN")


@pytest.mark.parametrize("value", ["", " spaced ", "line\nbreak", "非ASCII凭据"])
def test_invalid_environment_value_cannot_enter_headers_or_errors(monkeypatch, value):
    monkeypatch.setenv("DEMO_AUTH_TOKEN", value)
    with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE") as exc:
        resolve_credential("env://DEMO_AUTH_TOKEN")
    if value:
        assert value not in str(exc.value)


def test_missing_credential_is_not_an_anonymous_request(monkeypatch):
    monkeypatch.delenv("DEMO_AUTH_TOKEN", raising=False)
    spec = AgentVersionSpec(
        agent_id="secret-agent", version="v1", endpoint="https://agent.example/invoke",
        method="POST", timeout_seconds=2, max_retries=0, rate_limit_per_minute=60,
        request_mapping={}, max_concurrency=1, is_idempotent=True,
        credential_ref="env://DEMO_AUTH_TOKEN",
    )
    calls = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
            executor = RemoteAgentExecutor(spec, client=client)
            for once in (True, False):
                with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE"):
                    if once:
                        await executor.invoke_once({}, {})
                    else:
                        await executor.invoke({}, {}, client_transport=client._transport)
            no_secret = RemoteAgentExecutor(replace(spec, credential_ref=None), client=client)
            assert no_secret.spec.credential_ref is None

    asyncio.run(run())
    assert calls == []


@pytest.fixture
def vault_config(monkeypatch, tmp_path):
    jwt = tmp_path / "service-account-token"
    jwt.write_text("example-service-account-jwt")
    monkeypatch.setenv("ARGUS_VAULT_ADDR", "https://vault.example")
    monkeypatch.setenv("ARGUS_VAULT_ROLE", "argus-worker")
    monkeypatch.setenv("ARGUS_VAULT_JWT_PATH", str(jwt))
    monkeypatch.setenv("ARGUS_VAULT_KV_MOUNT", "secret")
    return jwt


def test_vault_uses_workload_identity_and_reads_rotated_value(vault_config, monkeypatch):
    from app.secret_providers import VaultKubernetesProvider

    requests = []
    values = iter(["agent-token-one", "agent-token-two"])

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/auth/kubernetes/login":
            assert json.loads(request.content) == {"role": "argus-worker", "jwt": "example-service-account-jwt"}
            assert "X-Vault-Token" not in request.headers
            return httpx.Response(200, json={"auth": {"client_token": "example-short-lived-vault-token"}})
        assert request.url.path == "/v1/secret/data/agents/banking"
        assert request.headers["X-Vault-Token"] == "example-short-lived-vault-token"
        return httpx.Response(200, json={"data": {"data": {"token": next(values)}}})

    monkeypatch.setattr(VaultKubernetesProvider, "_transport", httpx.MockTransport(handler))
    validate_credential_ref("vault://secret/agents/banking#token")
    assert resolve_credential("vault://secret/agents/banking#token") == "agent-token-one"
    assert resolve_credential("vault://secret/agents/banking#token") == "agent-token-two"
    assert len(requests) == 4


@pytest.mark.parametrize("ref", [
    "vault://other/agents/a#token", "vault://secret/../a#token", "vault://secret/a?token=raw",
    "vault://secret/a#", "vault://user:pass@secret/a#token", "vault://secret/%2e%2e/a#token",
])
def test_vault_rejects_invalid_or_unapproved_references(vault_config, ref):
    with pytest.raises(ValueError):
        validate_credential_ref(ref)


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_vault_failure_does_not_expose_remote_body_or_send_agent_request(vault_config, monkeypatch, status):
    from app.secret_providers import VaultKubernetesProvider

    def handler(request):
        return httpx.Response(status, text="private-vault-token-in-error", headers={"Location": "https://other.example"})

    monkeypatch.setattr(VaultKubernetesProvider, "_transport", httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE") as exc:
        resolve_credential("vault://secret/agents/a#token")
    assert "private-vault-token" not in str(exc.value)


def test_vault_requires_tls_and_workload_configuration(vault_config, monkeypatch):
    monkeypatch.setenv("ARGUS_VAULT_ADDR", "http://vault.example")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_credential_ref("vault://secret/agents/a#token")
    monkeypatch.setenv("ARGUS_VAULT_ADDR", "https://vault.example")
    monkeypatch.delenv("ARGUS_VAULT_ROLE")
    with pytest.raises(ValueError, match="not supported"):
        validate_credential_ref("vault://secret/agents/a#token")


def test_vault_missing_jwt_and_missing_secret_are_sanitized(vault_config, monkeypatch):
    from app.secret_providers import VaultKubernetesProvider

    Path(vault_config).unlink()
    with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE"):
        resolve_credential("vault://secret/agents/a#token")
    Path(vault_config).write_text("example-jwt")
    monkeypatch.setattr(VaultKubernetesProvider, "_transport", httpx.MockTransport(
        lambda request: httpx.Response(200, json={"auth": {"client_token": "example"}, "data": {"data": {}}})
    ))
    with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE"):
        resolve_credential("vault://secret/agents/a#token")
