from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.registry import AgentRegistry, compute_spec_digest  # noqa: E402


@pytest.fixture
def clean_registry(tmp_path):
    db_file = tmp_path / "test_yaml_sec.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    return AgentRegistry(db_mgr)


def test_yaml_import_rejects_plaintext_token(clean_registry, tmp_path):
    yaml_content = """
agents:
  bad-agent:
    name: Bad Agent
    versions:
      v1:
        endpoint: http://example.com/api
        credential_ref: "eyJh...raw-plaintext-token"
"""
    bad_yaml = tmp_path / "bad_token.yaml"
    bad_yaml.write_text(yaml_content, encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported credential reference scheme"):
        clean_registry.import_yaml(bad_yaml)


def test_yaml_import_rejects_invalid_endpoint(clean_registry, tmp_path):
    yaml_content = """
agents:
  bad-agent:
    name: Bad Agent
    versions:
      v1:
        endpoint: ftp://bad-scheme.com/api
"""
    bad_yaml = tmp_path / "bad_scheme.yaml"
    bad_yaml.write_text(yaml_content, encoding="utf-8")

    with pytest.raises(ValueError, match="Endpoint URL must use http or https scheme"):
        clean_registry.import_yaml(bad_yaml)


def test_yaml_import_rejects_non_w3c_trace_propagation(clean_registry, tmp_path):
    yaml_content = """
agents:
  bad-agent:
    name: Bad Agent
    versions:
      v1:
        endpoint: http://example.com/api
        trace_propagation: B3
"""
    bad_yaml = tmp_path / "bad_trace.yaml"
    bad_yaml.write_text(yaml_content, encoding="utf-8")

    with pytest.raises(ValueError, match="W3C"):
        clean_registry.import_yaml(bad_yaml)


def test_spec_digest_covers_deployment_evidence():
    base_spec = {
        "endpoint": "http://agent.internal/invoke",
        "protocol": "HTTP_JSON",
        "method": "POST",
        "request_mapping": {"query": "input.question"},
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "rate_limit_per_minute": 600,
        "max_concurrency": 4,
        "is_idempotent": False,
        "artifact_ref": "docker.io/argus/agent:v1.0.0@sha256:abc",
        "environment": "production",
        "trace_propagation": "W3C",
    }

    base_digest = compute_spec_digest(base_spec)

    # 1. Changing artifact_ref changes digest
    spec_changed_artifact = dict(base_spec, artifact_ref="docker.io/argus/agent:v1.0.1@sha256:def")
    assert compute_spec_digest(spec_changed_artifact) != base_digest

    # 2. Changing environment changes digest
    spec_changed_env = dict(base_spec, environment="staging")
    assert compute_spec_digest(spec_changed_env) != base_digest

    # 3. Changing is_idempotent changes digest
    spec_changed_idem = dict(base_spec, is_idempotent=True)
    assert compute_spec_digest(spec_changed_idem) != base_digest
