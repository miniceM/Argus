from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.registry import AgentRegistry, compute_spec_digest  # noqa: E402
from app.security import validate_credential_ref, validate_endpoint_url  # noqa: E402


def test_endpoint_security_validation():
    # URL embedded credentials must be rejected
    with pytest.raises(ValueError, match="embedded credentials"):
        validate_endpoint_url("http://admin:secret@demo-agent:8080/invoke")

    # Sensitive query parameter must be rejected
    with pytest.raises(ValueError, match="sensitive query parameters"):
        validate_endpoint_url("http://demo-agent:8080/invoke?token=secret123")

    # Clean URL accepted
    validate_endpoint_url("http://demo-agent:8080/invoke")
    validate_endpoint_url("https://api.example.com/v1/agent")


def test_credential_ref_validation(monkeypatch):
    monkeypatch.setenv("ARGUS_ALLOWED_CREDENTIAL_ENVS", "DEMO_AUTH_TOKEN,TEST_KEY")

    # Allowed env
    validate_credential_ref("env://DEMO_AUTH_TOKEN")
    validate_credential_ref("env://TEST_KEY")

    # Disallowed env
    with pytest.raises(ValueError, match="not in allowed credential whitelist"):
        validate_credential_ref("env://SYSTEM_ROOT_PASSWORD")

    # Unsupported schemes that cannot be resolved must be rejected
    with pytest.raises(ValueError, match="not supported"):
        validate_credential_ref("vault://secret/my-token")

    with pytest.raises(ValueError, match="not supported"):
        validate_credential_ref("k8s-secret://argus-ns/token")

    # Invalid scheme
    with pytest.raises(ValueError, match="Unsupported credential reference scheme"):
        validate_credential_ref("plain://my-secret-token")


def test_spec_digest_deterministic():
    spec1 = {
        "endpoint": "http://demo:8080/invoke",
        "method": "POST",
        "protocol": "HTTP_JSON",
        "request_mapping": {"messages": "input.messages"},
        "timeout_seconds": 10.0,
        "max_retries": 2,
    }
    spec2 = {
        "max_retries": 2,
        "timeout_seconds": 10.0,
        "request_mapping": {"messages": "input.messages"},
        "protocol": "HTTP_JSON",
        "method": "POST",
        "endpoint": "http://demo:8080/invoke",
    }
    digest1 = compute_spec_digest(spec1)
    digest2 = compute_spec_digest(spec2)
    assert digest1 == digest2
    assert len(digest1) == 64


def test_agent_and_version_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_ALLOWED_CREDENTIAL_ENVS", "DEMO_AUTH_TOKEN")
    db_file = tmp_path / "registry_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    # Register Agent
    agent = registry.create_agent(
        agent_id="test-agent",
        name="Test Agent",
        description="A test agent",
        owner="qa-team",
    )
    assert agent.id == "test-agent"

    # Register Version
    version = registry.create_version(
        agent_id="test-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
        method="POST",
        protocol="HTTP_JSON",
        request_mapping={"messages": "input.messages"},
        credential_ref="env://DEMO_AUTH_TOKEN",
        timeout_seconds=15.0,
        max_retries=2,
    )
    assert version.version == "v1"
    assert version.spec_digest is not None

    # Duplicate version must fail
    with pytest.raises(ValueError, match="already exists"):
        registry.create_version(
            agent_id="test-agent",
            version="v1",
            endpoint="http://localhost:8080/invoke",
        )

    # Unsupported protocol/method must fail
    with pytest.raises(ValueError, match="Only HTTP_JSON protocol is supported"):
        registry.create_version(
            agent_id="test-agent",
            version="v2",
            endpoint="http://localhost:8080/invoke",
            protocol="GRPC",
        )

    with pytest.raises(ValueError, match="Only POST method is supported"):
        registry.create_version(
            agent_id="test-agent",
            version="v3",
            endpoint="http://localhost:8080/invoke",
            method="GET",
        )

    # Archive version
    archived = registry.archive_version(agent_id="test-agent", version="v1")
    assert archived.is_active is False


def test_yaml_idempotent_import(tmp_path):
    db_file = tmp_path / "yaml_import_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    yaml_path = ROOT / "config" / "agents.yaml"

    # First import
    results = registry.import_yaml(yaml_path)
    assert len(results["created"]) == 2  # v1 and v2

    # Second import should skip because digest matches
    results2 = registry.import_yaml(yaml_path)
    assert len(results2["created"]) == 0
    assert len(results2["skipped"]) == 2
