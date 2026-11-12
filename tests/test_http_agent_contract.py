"""Issue #249：认证/Headers/输出契约，不使用真实秘密。"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
from pathlib import Path

import httpx
import pytest
import test_credential_resources as credential_tests
from app.executor import RemoteAgentExecutor
from app.models import AgentVersionSpecValidator
from app.registry import AgentVersionSpec, compute_spec_digest, normalize_and_validate_spec
from pydantic import ValidationError

credentials = credential_tests.credentials
create = credential_tests.create


def spec(**overrides):
    return AgentVersionSpec(agent_id="test-agent", version="http", endpoint="https://agent.example/send",
                            method="POST", timeout_seconds=5, max_retries=0, rate_limit_per_minute=600,
                            request_mapping={}, **overrides)


@pytest.mark.parametrize("header", [
    {"name": "Host", "value": "evil.example"},
    {"name": "traceparent", "value": "fake"},
    {"name": "Content-Type", "value": "text/plain"},
    {"name": "X-Thing", "value": "bad\r\ninjected: yes"},
    {"name": "Bad Name", "value": "ok"},
    {"name": "Authorization", "value": "Bearer never-echo-this"},
    {"name": "X-API-Key", "value": "never-echo-this"},
    {"name": "Cookie", "value": "never-echo-this"},
    {"name": "X-App", "value": "one", "credential_id": "secret-id"},
])
def test_header_contract_rejects_transport_trace_and_plain_secrets(header):
    with pytest.raises(ValidationError):
        AgentVersionSpecValidator(endpoint="https://agent.example/send", http_headers=[header])


def test_case_insensitive_duplicate_and_auth_conflict():
    with pytest.raises(ValidationError):
        AgentVersionSpecValidator(endpoint="https://agent.example/send", http_headers=[
            {"name": "X-Tenant", "value": "one"}, {"name": "x-tenant", "value": "two"}])
    with pytest.raises(ValidationError):
        AgentVersionSpecValidator(endpoint="https://agent.example/send", credential_id="one",
                                 http_headers=[{"name": "authorization", "credential_id": "two"}])
    with pytest.raises(ValidationError):
        AgentVersionSpecValidator(endpoint="https://agent.example/send", http_auth={"scheme": "basic"})


def test_new_defaults_preserve_digest_but_configuration_changes_identity():
    old = normalize_and_validate_spec({"endpoint": "https://agent.example/send"})
    defaulted = dict(old, http_auth=None, http_headers=[], response_mapping=None)
    assert compute_spec_digest(old) == compute_spec_digest(defaulted)
    for change in [{"http_headers": [{"name": "X-Tenant", "value": "one"}]},
                   {"http_auth": {"scheme": "none"}}, {"response_mapping": {"answer_path": "reply"}}]:
        assert compute_spec_digest(old) != compute_spec_digest(dict(old, **change))


def test_header_validation_api_does_not_echo_secret(credentials):
    client = credentials[0]
    response = client.post("/api/v1/agent-versions", json={"agent_id": "test-agent", "version": "bad",
        "endpoint": "https://agent.example/send", "http_headers": [{"name": "Authorization", "value": "never-echo-this"}]})
    assert response.status_code == 422
    assert "never-echo-this" not in response.text


@pytest.mark.parametrize("kind,secret,auth,expected", [
    ("api_key", "test-key-value", {"scheme": "api_key", "header_name": "X-API-Key"}, ("X-API-Key", "test-key-value")),
    ("api_key", "test-key-value", {"scheme": "api_key", "header_name": "Authorization", "prefix": "Token "}, ("Authorization", "Token test-key-value")),
    ("basic_auth", {"username": "test user", "password": "test:pass value"}, {"scheme": "basic"},
     ("Authorization", "Basic " + base64.b64encode(b"test user:test:pass value").decode())),
])
def test_managed_auth_and_header_references_on_both_execution_paths(credentials, kind, secret, auth, expected):
    client, service, registry, _ = credentials
    response = create(client, type=kind, secret=secret, allowed_origins=["https://agent.example"])
    assert response.status_code == 201, response.text
    primary = response.json()
    extra = create(client, type="header_value", secret="test-extra-secret", allowed_origins=["https://agent.example"]).json()
    headers = [{"name": "X-Tenant", "value": "evaluation"}, {"name": "X-App-Secret", "credential_id": extra["id"]}]
    version = registry.create_version("test-agent", "secured", "https://agent.example/send", credential_id=primary["id"],
                                     http_auth=auth, http_headers=headers, response_mapping={"answer_path": "reply"})
    assert version.http_headers == headers
    assert service.usage(extra["id"])["versions"][0]["version"] == "secured"
    assert client.delete(f"/api/v1/credentials/{extra['id']}", params={"confirm_name": extra['name']}).status_code == 409
    observed = []

    def target(request):
        observed.append(request)
        assert request.headers[expected[0]] == expected[1]
        assert request.headers["x-app-secret"] == "test-extra-secret"
        assert request.headers["x-tenant"] == "evaluation"
        assert "example-admin-token" not in str(request.headers)
        return httpx.Response(200, json={"reply": "business answer", "sessionKey": "isolated"})

    async def run():
        executor = RemoteAgentExecutor(registry.get("test-agent", "secured"), credential_db_manager=service.manager)
        async with httpx.AsyncClient(transport=httpx.MockTransport(target)) as transport:
            result = await executor.invoke_once({}, {"traceparent": "trace"}, client=transport)
            assert result.body == {"reply": "business answer", "answer": "business answer", "sessionKey": "isolated"}
            assert len(executor.credential_usage) == 2
            assert all(entry["version"] == 1 for entry in executor.credential_usage)
        result = await executor.invoke({}, {"traceparent": "trace"}, client_transport=httpx.MockTransport(target))
        assert result.body["answer"] == "business answer"
        await executor._client.aclose()
    asyncio.run(run())
    assert len(observed) == 2


def test_type_origin_environment_and_admin_binding_enforced(credentials):
    client, _, registry, _ = credentials
    response = create(client, type="api_key", allowed_origins=["https://agent.example"])
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    for options in [dict(http_auth={"scheme": "basic"}, credential_id=cid),
                    dict(http_auth={"scheme": "api_key", "header_name": "X-Key"}, credential_id=cid, environment="staging")]:
        with pytest.raises(ValueError):
            registry.create_version("test-agent", "bad", "https://agent.example/send", **options)
    with pytest.raises(ValueError):
        registry.create_version("test-agent", "bad-origin", "https://evil.example/send", credential_id=cid,
                                http_auth={"scheme": "api_key", "header_name": "X-Key"})
    client.headers.pop("Authorization")
    response = client.post("/api/v1/agent-versions", json={"agent_id": "test-agent", "version": "bad-bind",
        "endpoint": "https://agent.example/send", "http_headers": [{"name": "X-Extra", "credential_id": cid}]})
    assert response.status_code == 401


@pytest.mark.parametrize("path", ["once", "retry"])
@pytest.mark.parametrize("response", [httpx.Response(302, headers={"Location": "https://evil.example/"}),
    httpx.Response(202, json={"reply": "not final"}), httpx.Response(200, json={"other": "missing"}),
    httpx.Response(200, json={"reply": {"not": "text"}})])
def test_redirect_async_and_invalid_output_fail_closed(path, response):
    async def run():
        executor = RemoteAgentExecutor(spec(response_mapping={"answer_path": "reply"}))
        transport = httpx.MockTransport(lambda _: response)
        if path == "once":
            async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
                result = await executor.invoke_once({}, {}, client=client)
                assert result.body is None and not result.is_retryable
                assert result.error_category == "INVALID_RESPONSE"
        else:
            with pytest.raises(RuntimeError, match="INVALID_RESPONSE"):
                await executor.invoke({}, {}, client_transport=transport)
        await executor._client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("path", ["once", "retry"])
def test_secret_echo_rejected_before_persistence(credentials, path):
    client, service, registry, _ = credentials
    cid = create(client).json()["id"]
    registry.create_version("test-agent", "echo", "https://agent.example/send", credential_id=cid)
    async def run():
        executor = RemoteAgentExecutor(registry.get("test-agent", "echo"), credential_db_manager=service.manager)
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"answer": "example-first-token"},
                                                                headers={"X-Echo": "example-first-token"}))
        if path == "once":
            async with httpx.AsyncClient(transport=transport) as http:
                result = await executor.invoke_once({}, {}, client=http)
                assert result.body is None and result.raw_response is None and result.headers == {}
                assert result.error_category == "SENSITIVE_RESPONSE"
                assert "example-first-token" not in repr(dataclasses.asdict(result))
        else:
            with pytest.raises(RuntimeError, match="SENSITIVE_RESPONSE") as err:
                await executor.invoke({}, {}, client_transport=transport)
            assert "example-first-token" not in str(err.value)
        await executor._client.aclose()
    asyncio.run(run())


def test_platform_eval_headers_are_reserved():
    with pytest.raises(ValidationError):
        AgentVersionSpecValidator(endpoint="https://agent.example/send", http_headers=[{"name": "X-Eval-Launch-Id", "value": "forged"}])


def test_header_rotation_disable_and_target_recheck_before_send(credentials):
    client, service, registry, _ = credentials
    extra = create(client, type="header_value", secret="first-header-token", allowed_origins=["https://agent.example"]).json()
    registry.create_version("test-agent", "header-only", "https://agent.example/send", max_retries=1,
                            http_headers=[{"name": "X-Extra", "credential_id": extra["id"]}])
    configuration = registry.get("test-agent", "header-only")
    revisions = []
    calls = []
    def target(request):
        calls.append(request.headers["x-extra"])
        if len(calls) == 1:
            client.post(f"/api/v1/credentials/{extra['id']}/rotate", json={"secret": "second-header-token"}).raise_for_status()
            return httpx.Response(500)
        return httpx.Response(200, json={"answer": "ok"})
    async def run():
        executor = RemoteAgentExecutor(configuration, credential_db_manager=service.manager)
        def authorize(_):
            revisions.append(copy_usage(executor.credential_usage))
            return "attempt"
        await executor.invoke({}, {}, client_transport=httpx.MockTransport(target), on_attempt_start=authorize)
        assert [u[0]["version"] for u in revisions] == [1, 2]
        for changed in [dataclasses.replace(configuration, endpoint="https://evil.example/send"),
                        dataclasses.replace(configuration, environment="staging")]:
            denied = RemoteAgentExecutor(changed, credential_db_manager=service.manager)
            with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE"):
                await denied.invoke({}, {}, client_transport=httpx.MockTransport(target))
            await denied._client.aclose()
        client.post(f"/api/v1/credentials/{extra['id']}/disable", json={"confirm_name": extra["name"], "force": True}).raise_for_status()
        with pytest.raises(ValueError, match="CREDENTIAL_UNAVAILABLE"):
            await executor.invoke({}, {}, client_transport=httpx.MockTransport(target))
        await executor._client.aclose()
    asyncio.run(run())
    assert calls == ["first-header-token", "second-header-token"]


def copy_usage(usage):
    return [dict(entry) for entry in usage]


@pytest.mark.parametrize("echo", ["fixture username", "fixture password", "fixture username:fixture password", base64.b64encode(b"fixture username:fixture password").decode()])
def test_basic_encoded_decoded_and_header_echo_are_not_retained(credentials, echo):
    client, service, registry, _ = credentials
    primary = create(client, type="basic_auth", secret={"username": "fixture username", "password": "fixture password"}, allowed_origins=["https://agent.example"]).json()
    registry.create_version("test-agent", "basic-echo", "https://agent.example/send", credential_id=primary["id"], http_auth={"scheme": "basic"})
    async def run():
        executor = RemoteAgentExecutor(registry.get("test-agent", "basic-echo"), credential_db_manager=service.manager)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401, headers={"X-Echo": echo}))) as http:
            result = await executor.invoke_once({}, {}, client=http)
            assert result.error_category == "SENSITIVE_RESPONSE"
            assert result.raw_response is None and result.headers == {} and not result.is_retryable
        await executor._client.aclose()
    asyncio.run(run())


def test_020_upgrade_keeps_old_identity_and_adds_header_fk(tmp_path):
    """从已发布 019 升级，不能依赖 ORM create_all 冒充迁移。"""
    import shutil

    from app.db import MigrationRunner
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    before = tmp_path / "before"
    before.mkdir()
    for source in sorted(migrations.glob("*.sql")):
        if source.name < "020":
            shutil.copyfile(source, before / source.name)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    MigrationRunner(engine, before).apply_all()
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("INSERT INTO agents (id,name) VALUES ('old-agent','Old')"))
        connection.execute(text("INSERT INTO agent_versions (id,agent_id,version,spec_digest,endpoint) VALUES ('old-version','old-agent','v1','unchanged-digest','https://agent.example/send')"))
        connection.execute(text("INSERT INTO credentials (id,name,environment,provider) VALUES ('header-secret','Header','production','managed')"))
    assert MigrationRunner(engine, migrations).apply_all() == ["020_http_agent_invocation.sql", "021_langfuse_connection.sql"]
    with engine.begin() as connection:
        row = connection.execute(text("SELECT spec_digest,http_auth,http_headers,response_mapping,langfuse_connection FROM agent_versions")).one()
        assert tuple(row) == ("unchanged-digest", None, "[]", None, None)
        connection.execute(text("INSERT INTO agent_version_credentials (agent_version_id,credential_id) VALUES ('old-version','header-secret')"))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("DELETE FROM credentials WHERE id='header-secret'"))
    engine.dispose()


@pytest.mark.parametrize("path", ["once", "retry"])
def test_concurrent_rotation_preserves_each_inflight_secret_filter(credentials, path):
    """共享 Executor 轮换后，旧请求的响应仍须按其实际发送的秘密过滤。"""
    client, service, registry, _ = credentials
    cid = create(client, secret="inflight-old-sensitive-token").json()["id"]
    registry.create_version("test-agent", "rotation-filter", "https://agent.example/send", credential_id=cid, max_retries=0)

    async def run():
        executor = RemoteAgentExecutor(registry.get("test-agent", "rotation-filter"), credential_db_manager=service.manager)
        started, release = asyncio.Event(), asyncio.Event()

        async def target(request):
            if request.headers["authorization"] == "Bearer inflight-old-sensitive-token":
                started.set()
                await release.wait()
                return httpx.Response(200, json={"answer": "inflight-old-sensitive-token"})
            return httpx.Response(200, json={"answer": "safe answer"})

        transport = httpx.MockTransport(target)
        async with httpx.AsyncClient(transport=transport) as http:
            async def invoke():
                if path == "once":
                    return await executor.invoke_once({}, {}, client=http)
                return await executor.invoke({}, {}, client_transport=transport)

            first = asyncio.create_task(invoke())
            await asyncio.wait_for(started.wait(), 5)
            assert client.post(f"/api/v1/credentials/{cid}/rotate", json={"secret": "inflight-new-sensitive-token"}).status_code == 200
            await executor.prepare_credential(force_refresh=True)
            assert (await invoke()).body == {"answer": "safe answer"}
            release.set()
            if path == "once":
                result = await first
                assert result.error_category == "SENSITIVE_RESPONSE" and result.body is None
            else:
                with pytest.raises(RuntimeError, match="SENSITIVE_RESPONSE"):
                    await first
        await executor._client.aclose()
    asyncio.run(run())
