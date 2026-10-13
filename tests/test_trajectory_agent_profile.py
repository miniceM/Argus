"""AgentVersion 来源声明的身份、摘要与不可变注册契约。"""

from pathlib import Path

import pytest
import yaml
from app.api_registry import get_registry, router
from app.db import DatabaseManager, MigrationRunner
from app.models import AgentVersionCreateRequest, AgentVersionResponse
from app.registry import AgentRegistry, compute_spec_digest, normalize_and_validate_spec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "http://localhost:8080/invoke"
LEGACY_DIGEST = "392113f610c7151e47502cd88e254be29c855d665c1dbfc4a4fe23836491e372"


@pytest.fixture
def registry():
    manager = DatabaseManager("sqlite:///:memory:")
    MigrationRunner(manager.engine, ROOT / "migrations").apply_all()
    registry = AgentRegistry(manager)
    registry.create_agent(agent_id="profile-agent", name="Synthetic Profile Agent")
    yield registry
    manager.engine.dispose()


def test_absent_or_null_profile_preserves_pre_change_digest_and_metadata_cannot_select_profile():
    old = normalize_and_validate_spec({"endpoint": ENDPOINT})
    null = normalize_and_validate_spec({"endpoint": ENDPOINT, "trajectory_source_profile_id": None})
    self_report = normalize_and_validate_spec(
        {
            "endpoint": ENDPOINT,
            "metadata": {"trajectory_source_profile_id": "pretend-native"},
        }
    )
    assert old["spec_digest"] == null["spec_digest"] == self_report["spec_digest"] == LEGACY_DIGEST
    assert old["trajectory_source_profile_id"] is None
    assert null["trajectory_source_profile_id"] is None
    assert self_report["trajectory_source_profile_id"] is None


def test_declared_profile_changes_digest_without_using_version_label_or_metadata():
    native = normalize_and_validate_spec({"endpoint": ENDPOINT, "trajectory_source_profile_id": "native-otel-v1"})
    pydantic = normalize_and_validate_spec({"endpoint": ENDPOINT, "trajectory_source_profile_id": "pydantic-ai-v1"})
    assert native["spec_digest"] != pydantic["spec_digest"] != LEGACY_DIGEST
    assert compute_spec_digest(dict(reversed(list(native.items())))) == native["spec_digest"]
    assert compute_spec_digest({**native, "version": "other", "metadata": {"changed": True}}) == native["spec_digest"]


@pytest.mark.parametrize("profile", ["", "has space", "has\ncontrol", "中文", "x" * 65, 1, True, b"native"])
def test_profile_rejects_invalid_identity_and_type_coercion(profile):
    with pytest.raises(ValidationError):
        AgentVersionCreateRequest.model_validate(
            {
                "agent_id": "profile-agent",
                "version": "v1",
                "endpoint": ENDPOINT,
                "trajectory_source_profile_id": profile,
            }
        )


def test_profile_is_top_level_persisted_and_immutable_in_registry(registry):
    first = registry.create_version(
        "profile-agent",
        "v1",
        ENDPOINT,
        trajectory_source_profile_id="native-otel-v1",
    )
    second = registry.create_version(
        "profile-agent",
        "v2",
        ENDPOINT,
        trajectory_source_profile_id="pydantic-ai-v1",
    )
    assert first.spec_digest != second.spec_digest
    assert registry.get_version("profile-agent", "v1").trajectory_source_profile_id == "native-otel-v1"
    assert registry.get("profile-agent", "v1").trajectory_source_profile_id == "native-otel-v1"
    assert registry.list()["profile-agent"]["versions"]["v1"]["trajectory_source_profile_id"] == "native-otel-v1"
    assert AgentVersionResponse.model_validate(first).trajectory_source_profile_id == "native-otel-v1"
    with pytest.raises(ValueError, match="immutable"):
        registry.create_version("profile-agent", "v1", ENDPOINT, trajectory_source_profile_id="pydantic-ai-v1")
    assert registry.get_version("profile-agent", "v1").spec_digest == first.spec_digest


def test_yaml_bootstrap_preserves_profile_and_rejects_changed_profile_for_existing_version(registry, tmp_path):
    path = tmp_path / "agents.yaml"
    data = {
        "agents": {
            "profile-agent": {
                "name": "Synthetic",
                "versions": {
                    "v1": {
                        "endpoint": ENDPOINT,
                        "trajectory_source_profile_id": "native-otel-v1",
                    }
                },
            }
        }
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert registry.import_yaml(path) == {"created": ["profile-agent:v1"], "skipped": []}
    assert registry.import_yaml(path) == {"created": [], "skipped": ["profile-agent:v1"]}
    assert registry.get("profile-agent", "v1").trajectory_source_profile_id == "native-otel-v1"
    data["agents"]["profile-agent"]["versions"]["v1"]["trajectory_source_profile_id"] = "pydantic-ai-v1"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot overwrite"):
        registry.import_yaml(path)
    assert registry.get("profile-agent", "v1").trajectory_source_profile_id == "native-otel-v1"


def test_http_create_query_list_and_archive_round_trip_profile(registry):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_registry] = lambda: registry
    with TestClient(app) as client:
        payload = {
            "agent_id": "profile-agent",
            "version": "v1",
            "endpoint": ENDPOINT,
            "trajectory_source_profile_id": "native-otel-v1",
        }
        created = client.post("/api/v1/agent-versions", json=payload)
        assert created.status_code == 201
        assert created.json()["trajectory_source_profile_id"] == "native-otel-v1"
        queried = client.get("/api/v1/agent-versions", params={"agent_id": "profile-agent", "version": "v1"})
        assert queried.json()["trajectory_source_profile_id"] == "native-otel-v1"
        listed = client.get("/api/v1/agent-versions", params={"agent_id": "profile-agent"})
        assert listed.json()[0]["trajectory_source_profile_id"] == "native-otel-v1"
        assert (
            client.post(
                "/api/v1/agent-versions", json={**payload, "trajectory_source_profile_id": "pydantic-ai-v1"}
            ).status_code
            == 409
        )
        assert (
            client.get("/api/v1/agent-versions", params={"agent_id": "profile-agent", "version": "v1"}).json()[
                "spec_digest"
            ]
            == created.json()["spec_digest"]
        )
        archived = client.post("/api/v1/agent-versions/archive", json={"agent_id": "profile-agent", "version": "v1"})
        assert archived.json()["trajectory_source_profile_id"] == "native-otel-v1"
        assert archived.json()["is_active"] is False
        invalid = client.post(
            "/api/v1/agent-versions",
            json={**payload, "version": "v2", "trajectory_source_profile_id": "invalid profile"},
        )
        assert invalid.status_code == 422


def test_legacy_version_omits_profile_in_compatibility_listing(registry):
    legacy = registry.create_version("profile-agent", "legacy", ENDPOINT)
    assert legacy.spec_digest == LEGACY_DIGEST
    assert registry.get("profile-agent", "legacy").trajectory_source_profile_id is None
    assert AgentVersionResponse.model_validate(legacy).trajectory_source_profile_id is None
    assert "trajectory_source_profile_id" not in registry.list()["profile-agent"]["versions"]["legacy"]
