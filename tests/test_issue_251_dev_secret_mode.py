"""Issue #251：显式开发测试模式（免 Keyring/Admin Token 快速联调）与非敏感凭据排障原因码。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from app import main
from app.credentials import CredentialService, ManagedSecretProvider
from app.db_models import CredentialAuditRecord, CredentialSecretRecord
from app.langfuse_connection import resolve_connection
from app.manifest import LaunchService
from app.registry import AgentRegistry
from app.secret_providers import CredentialUnavailable
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture
def runtime_client(setup_runtime, monkeypatch):
    manager = setup_runtime[0]
    monkeypatch.setattr(main, "db_manager", manager)
    registry = AgentRegistry(manager)
    monkeypatch.setattr(main, "registry", registry)
    launch_svc = LaunchService(manager, registry)
    monkeypatch.setattr(main, "launch_service", launch_svc)
    monkeypatch.setattr(main, "orchestrator", setup_runtime[3])
    return TestClient(main.app), manager, CredentialService(manager), registry


def test_dev_secret_mode_runs_full_authenticated_and_langfuse_flow_without_files(runtime_client, monkeypatch):
    """显式 ARGUS_SECRET_MODE=development 且未配置密钥/管理授权文件时，免手工配置跑通全流程且仍加密落库。"""
    client, manager, service, registry = runtime_client
    monkeypatch.setenv("ARGUS_SECRET_MODE", "development")
    monkeypatch.delenv("ARGUS_MANAGED_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE", raising=False)

    info = client.get("/api/v1/system/info")
    assert info.status_code == 200
    assert info.json()["credential_admin_required"] is False
    assert info.json()["secret_dev_mode"] is True

    # 无需 Authorization 头即可创建凭据（Agent Bearer 与 Langfuse Basic Auth）
    agent_cred_resp = client.post(
        "/api/v1/credentials",
        json={
            "name": "Dev Agent Token",
            "environment": "staging",
            "provider": "managed",
            "type": "bearer_token",
            "allowed_origins": ["https://agent.dev.example"],
            "secret": "dev-agent-secret-token",
        },
    )
    assert agent_cred_resp.status_code == 201, agent_cred_resp.text
    agent_cred_id = agent_cred_resp.json()["id"]
    assert "dev-agent-secret-token" not in agent_cred_resp.text

    lf_cred_resp = client.post(
        "/api/v1/credentials",
        json={
            "name": "Dev Langfuse Key",
            "environment": "staging",
            "provider": "managed",
            "type": "basic_auth",
            "allowed_origins": ["https://lf.dev.example"],
            "secret": {"username": "pk-lf-dev-123", "password": "sk-lf-dev-456"},
        },
    )
    assert lf_cred_resp.status_code == 201, lf_cred_resp.text
    lf_cred_id = lf_cred_resp.json()["id"]
    assert "sk-lf-dev-456" not in lf_cred_resp.text

    # 数据库仍然保存加密密文，不含明文；审计记录明确标注 dev-bypass
    with manager.get_session() as session:
        secrets = session.scalars(select(CredentialSecretRecord)).all()
        assert len(secrets) == 2
        assert all("dev-agent-secret-token" not in json.dumps(row.envelope) for row in secrets)
        assert all("sk-lf-dev-456" not in json.dumps(row.envelope) for row in secrets)
        audits = session.scalars(select(CredentialAuditRecord)).all()
        assert any(a.actor == "credential-admin:dev-bypass" for a in audits)
    db_bytes = Path(manager.engine.url.database).read_bytes()
    assert b"dev-agent-secret-token" not in db_bytes
    assert b"sk-lf-dev-456" not in db_bytes

    # 新建 provider 实例模拟重启后仍可稳定解密
    reloaded = service.resolve(agent_cred_id, "bearer_token", "https://agent.dev.example/invoke", "staging")
    assert reloaded.token == "dev-agent-secret-token"
    assert isinstance(ManagedSecretProvider(), ManagedSecretProvider)

    # 无需 Authorization 头即可注册 Agent、绑定 Langfuse 项目与创建带凭据的 AgentVersion
    reg_resp = client.post(
        "/api/v1/agents",
        json={
            "id": "dev-auth-agent",
            "name": "Dev Auth Agent",
            "langfuse_connection": {
                "base_url": "https://lf.dev.example",
                "project_id": "proj-dev",
                "credential_id": lf_cred_id,
            },
        },
    )
    assert reg_resp.status_code == 201, reg_resp.text

    ver_resp = client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": "dev-auth-agent",
            "version": "v1",
            "endpoint": "https://agent.dev.example/invoke",
            "environment": "staging",
            "credential_id": agent_cred_id,
            "http_auth": {"scheme": "bearer"},
        },
    )
    assert ver_resp.status_code == 201, ver_resp.text

    # 拉取候选 Score 成功走绑定凭据
    with patch("app.langfuse_connection.BoundLangfuseClient.verify_project", return_value=None), patch(
        "app.api_launches.discover_managed_score_candidates",
        return_value=[
            {
                "rule_id": "rule-1",
                "evaluator_id": "eval-1",
                "evaluator_name": "Accuracy",
                "evaluator_type": "LLM_AS_A_JUDGE",
                "data_type": "NUMERIC",
                "score_name": "accuracy",
                "verified": True,
                "enabled": True,
                "sampling": 1.0,
            }
        ],
    ):
        cand = client.get(
            "/api/v1/langfuse/managed-score-candidates",
            params={"agent_id": "dev-auth-agent", "environment": "staging"},
        )
        assert cand.status_code == 200, cand.text
        assert cand.json()[0]["score_name"] == "accuracy"


def test_production_or_default_mode_remains_fail_closed(runtime_client, monkeypatch):
    """未显式设置 ARGUS_SECRET_MODE=development 或为 production 时，必须严格 fail-closed。"""
    client, _, _, _ = runtime_client
    monkeypatch.delenv("ARGUS_SECRET_MODE", raising=False)
    monkeypatch.delenv("ARGUS_MANAGED_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE", raising=False)

    info = client.get("/api/v1/system/info")
    assert info.status_code == 200
    assert info.json()["credential_admin_required"] is True
    assert info.json()["secret_dev_mode"] is False

    resp = client.post(
        "/api/v1/credentials",
        json={"name": "Prod Token", "environment": "production", "secret": "prod-secret"},
    )
    assert resp.status_code == 503
    assert resp.headers.get("X-Argus-Error-Code") == "CREDENTIAL_ADMIN_UNAVAILABLE"

    monkeypatch.setenv("ARGUS_SECRET_MODE", "production")
    with pytest.raises(CredentialUnavailable) as exc_info:
        ManagedSecretProvider()
    assert exc_info.value.reason == "KEYRING_MISSING"


def test_dev_mode_still_enforces_explicit_files_when_configured(runtime_client, tmp_path, monkeypatch):
    """即使在 ARGUS_SECRET_MODE=development 下，只要显式配置了文件，就必须严格校验不静默绕过。"""
    client, _, _, _ = runtime_client
    monkeypatch.setenv("ARGUS_SECRET_MODE", "development")

    admin = tmp_path / "admin-token"
    admin.write_text("real-admin-token")
    admin.chmod(0o600)
    monkeypatch.setenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE", str(admin))
    monkeypatch.delenv("ARGUS_MANAGED_SECRET_KEY_FILE", raising=False)

    info = client.get("/api/v1/system/info")
    assert info.status_code == 200
    assert info.json()["credential_admin_required"] is True

    # 未携带管理授权头必须被 401 拒绝
    unauth = client.post(
        "/api/v1/credentials",
        json={"name": "Need Auth", "environment": "staging", "secret": "some-token"},
    )
    assert unauth.status_code == 401

    # 显式指定了不存在的 keyring 文件路径时，不能回落内置 dev 密钥
    monkeypatch.setenv("ARGUS_MANAGED_SECRET_KEY_FILE", str(tmp_path / "missing-keyring.json"))
    with pytest.raises(CredentialUnavailable) as exc_info:
        ManagedSecretProvider()
    assert exc_info.value.reason == "KEYRING_MISSING"


def test_diagnostic_reasons_for_resolution_failures_without_leaking_secrets(runtime_client, tmp_path, monkeypatch):
    """各类凭据失败返回明确的非敏感原因码，且绝不泄露 Secret 原文。"""
    client, manager, service, _ = runtime_client
    monkeypatch.setenv("ARGUS_SECRET_MODE", "development")
    monkeypatch.delenv("ARGUS_MANAGED_SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE", raising=False)

    lf_cred_id = client.post(
        "/api/v1/credentials",
        json={
            "name": "Staging LF",
            "environment": "staging",
            "type": "basic_auth",
            "allowed_origins": ["https://lf.example"],
            "secret": {"username": "pk-secret-val", "password": "sk-secret-val"},
        },
    ).json()["id"]
    client.post(
        "/api/v1/agents",
        json={
            "id": "diag-agent",
            "name": "Diag Agent",
            "langfuse_connection": {
                "base_url": "https://lf.example",
                "project_id": "proj-1",
                "credential_id": lf_cred_id,
            },
        },
    )
    client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": "diag-agent",
            "version": "v1",
            "endpoint": "https://agent.example/invoke",
            "environment": "production",
        },
    )

    # 1) ENVIRONMENT_MISMATCH：凭据在 staging，请求在 production
    cand = client.get(
        "/api/v1/langfuse/managed-score-candidates",
        params={"agent_id": "diag-agent", "environment": "production"},
    )
    assert cand.status_code == 409
    assert cand.json()["detail"]["code"] == "CREDENTIAL_UNAVAILABLE"
    assert cand.json()["detail"]["reason"] == "ENVIRONMENT_MISMATCH"
    assert "ENVIRONMENT_MISMATCH" in cand.json()["detail"]["message"]
    assert "sk-secret-val" not in cand.text

    launch_err = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "diag-agent",
            "agent_version": "v1",
            "dataset_name": "ds",
            "environment": "production",
        },
    )
    assert launch_err.status_code == 400
    assert "CREDENTIAL_UNAVAILABLE" in launch_err.json()["detail"]
    assert "ENVIRONMENT_MISMATCH" in launch_err.json()["detail"]
    assert "sk-secret-val" not in launch_err.text

    # 2) ORIGIN_NOT_ALLOWED
    with pytest.raises(CredentialUnavailable) as exc_origin:
        service.resolve(lf_cred_id, "basic_auth", "https://other.example", "staging")
    assert exc_origin.value.reason == "ORIGIN_NOT_ALLOWED"

    # 3) CREDENTIAL_NOT_FOUND
    with pytest.raises(CredentialUnavailable) as exc_missing:
        service.resolve("does-not-exist", "basic_auth", "https://lf.example", "staging")
    assert exc_missing.value.reason == "CREDENTIAL_NOT_FOUND"

    # 4) MALFORMED_SECRET_FORMAT：Basic Auth 中含首尾空格，Langfuse 连接解析时应明确报格式错误
    bad_lf_id = client.post(
        "/api/v1/credentials",
        json={
            "name": "Spaced LF",
            "environment": "staging",
            "type": "basic_auth",
            "allowed_origins": ["https://lf.example"],
            "secret": {"username": " pk-with-space", "password": "sk-secret-val"},
        },
    ).json()["id"]
    with pytest.raises(CredentialUnavailable) as exc_fmt:
        resolve_connection(
            manager,
            {"base_url": "https://lf.example", "project_id": "proj-1", "credential_id": bad_lf_id},
            "staging",
        )
    assert exc_fmt.value.reason == "MALFORMED_SECRET_FORMAT"
    assert "pk-with-space" not in str(exc_fmt.value)

    # 5) CREDENTIAL_DISABLED
    assert client.post(
        f"/api/v1/credentials/{lf_cred_id}/disable",
        json={"confirm_name": "Staging LF", "force": True},
    ).status_code == 200
    with pytest.raises(CredentialUnavailable) as exc_disabled:
        service.resolve(lf_cred_id, "basic_auth", "https://lf.example", "staging")
    assert exc_disabled.value.reason == "CREDENTIAL_DISABLED"

    # 6) KEYRING_INSECURE_PERMISSIONS 与 DECRYPT_FAILED
    key = tmp_path / "keyring.json"
    key.write_text(json.dumps({"active_key_id": "other", "keys": {"other": base64.b64encode(b"x" * 32).decode()}}))
    key.chmod(0o644)
    monkeypatch.setenv("ARGUS_MANAGED_SECRET_KEY_FILE", str(key))
    with pytest.raises(CredentialUnavailable) as exc_perm:
        ManagedSecretProvider()
    assert exc_perm.value.reason == "KEYRING_INSECURE_PERMISSIONS"

    key.chmod(0o600)
    with pytest.raises(CredentialUnavailable) as exc_decrypt:
        service.resolve(bad_lf_id, "basic_auth", "https://lf.example", "staging")
    assert exc_decrypt.value.reason == "DECRYPT_FAILED"
