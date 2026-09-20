from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.manifest import LaunchService  # noqa: E402
from app.registry import AgentRegistry  # noqa: E402


def setup_db(tmp_path):
    db_file = tmp_path / "manifest_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")
    return db_mgr, registry


def test_launch_manifest_snapshot_freeze(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch_svc = LaunchService(db_mgr, registry, runner_version="0.1.0")

    # Creating launch with non-existent agent version must fail fast
    with pytest.raises(ValueError, match="not found"):
        launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v999",
            dataset_name="banking-agent-regression",
        )

    # Creating valid launch
    launch = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        name="test-launch-v1",
        idempotency_key="key-123",
        max_concurrency=2,
    )

    assert launch.id is not None
    assert launch.status == "PENDING"
    manifest = launch.manifest
    assert manifest["schema_version"] == "1.0"
    assert manifest["agent"]["agent_id"] == "banking-agent"
    assert manifest["agent"]["version"] == "v1"
    assert manifest["agent"]["endpoint"] == "http://demo-agent-v1:8080/invoke"
    assert manifest["agent"]["spec_digest"] is not None
    assert manifest["runner"]["runner_version"] == "0.1.0"
    assert manifest["runner"]["mapping_engine_version"] is not None
    assert manifest["execution_policy"]["max_concurrency"] == 2
    assert len(manifest["evaluators"]) >= 4

    # Idempotent re-creation with same key and payload returns existing launch
    same_launch = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        name="test-launch-v1",
        idempotency_key="key-123",
        max_concurrency=2,
    )
    assert same_launch.id == launch.id

    # Idempotent re-creation with same key but different payload must raise Conflict
    with pytest.raises(ValueError, match="Idempotency key conflict"):
        launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            name="test-launch-v1",
            idempotency_key="key-123",
            max_concurrency=4,  # Different concurrency!
        )

    # Idempotent re-creation with same key but different requested name must raise Conflict
    with pytest.raises(ValueError, match="Idempotency key conflict"):
        launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            name="different-launch-name-v1",  # Different name!
            idempotency_key="key-123",
            max_concurrency=2,
        )

    # Agent with is_idempotent=True must preserve this flag in the frozen manifest
    registry.create_version(
        agent_id="banking-agent",
        version="v-idempotent",
        endpoint="http://demo-agent-v1:8080/invoke",
        is_idempotent=True,
    )
    launch_idem = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v-idempotent",
        dataset_name="banking-agent-regression",
    )
    assert launch_idem.manifest["agent"]["is_idempotent"] is True

    # Creating launch with explicitly supplied launch_id
    custom_id = "custom-launch-uuid-123"
    custom_launch = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        launch_id=custom_id,
    )
    assert custom_launch.id == custom_id


def test_launch_concurrency_inherits_agent_version_policy(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch_svc = LaunchService(db_mgr, registry)

    # Register an agent version with strict max_concurrency=1
    registry.create_version(
        agent_id="banking-agent",
        version="v-single-thread",
        endpoint="http://demo-agent-v1:8080/invoke",
        max_concurrency=1,
    )

    # 1. Launch without max_concurrency override MUST inherit from AgentVersion (1)
    launch = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v-single-thread",
        dataset_name="banking-agent-regression",
        max_concurrency=None,  # No override
    )
    assert launch.manifest["execution_policy"]["max_concurrency"] == 1

    # 2. Launch with valid override (1 <= 1) succeeds
    launch_valid = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v-single-thread",
        dataset_name="banking-agent-regression",
        max_concurrency=1,
    )
    assert launch_valid.manifest["execution_policy"]["max_concurrency"] == 1

    # 3. Launch with excessive override (4 > 1) MUST fail fast with ValueError
    with pytest.raises(ValueError, match="exceeds AgentVersion limit"):
        launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v-single-thread",
            dataset_name="banking-agent-regression",
            max_concurrency=4,
        )


def test_api_request_model_concurrency_defaults_to_none():
    from app.models import ExperimentLaunchCreateRequest

    req = ExperimentLaunchCreateRequest(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
    )
    # Default must be None, NOT hardcoded 4
    assert req.max_concurrency is None


