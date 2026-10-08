from __future__ import annotations

import asyncio
from dataclasses import replace

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


@pytest.mark.parametrize("value", ["", " spaced ", "two parts", "line\nbreak", "非ASCII凭据"])
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


def test_fifty_credential_resolutions_are_not_queued_behind_the_default_thread_pool(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.credentials import CredentialService, ResolvedCredential

    # 所有 50 路解析都须进入 Provider 才能通过；使用真实截止预算而非缩放时钟。
    entered = threading.Barrier(50, timeout=4)
    def resolve(*args):
        entered.wait()
        return ResolvedCredential("example-concurrent-token", "credential-1", 1, "managed")
    monkeypatch.setattr(CredentialService, "resolve", resolve)
    spec = AgentVersionSpec(agent_id="test-agent", version="v1", endpoint="https://agent.example/invoke", credential_id="credential-1", max_concurrency=50, method="POST", timeout_seconds=5, max_retries=0, rate_limit_per_minute=600, request_mapping={})
    async def run():
        default_pool = ThreadPoolExecutor(max_workers=2)
        release = threading.Event()
        default_pool.submit(release.wait, 10)
        default_pool.submit(release.wait, 10)
        asyncio.get_running_loop().set_default_executor(default_pool)
        try:
            async with httpx.AsyncClient() as client:
                executors = [RemoteAgentExecutor(spec, client=client, credential_db_manager=object()) for _ in range(50)]
                results = await asyncio.gather(*(executor.prepare_credential() for executor in executors), return_exceptions=True)
                assert not any(isinstance(result, BaseException) for result in results)
                assert all(executor.resolved_credential.version == 1 for executor in executors)
        finally:
            release.set()
    asyncio.run(run())


def test_external_reference_stays_unsupported_even_with_vault_settings(monkeypatch):
    monkeypatch.setenv('ARGUS_VAULT_ADDR', 'https://vault.example')
    monkeypatch.setenv('ARGUS_VAULT_ROLE', 'argus-worker')
    with pytest.raises(ValueError, match='not supported'):
        validate_credential_ref('vault://secret/agents/a#token')
