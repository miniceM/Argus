from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.manifest import LaunchService, acquire_launch_execution  # noqa: E402
from app.registry import AgentRegistry  # noqa: E402


def test_atomic_execution_lock_prevents_duplicate_runs(tmp_path):
    db_file = tmp_path / "lock_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")

    launch_svc = LaunchService(db_mgr, registry)
    launch = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
    )

    # First call acquires execution lock
    acquired_1 = acquire_launch_execution(db_mgr, launch.id)
    assert acquired_1 is True

    # Second call must fail to acquire lock
    acquired_2 = acquire_launch_execution(db_mgr, launch.id)
    assert acquired_2 is False


def test_idempotent_launch_creation_same_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_file = tmp_path / "idem_same.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")

    launch_svc = LaunchService(db_mgr, registry)
    launch1 = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        idempotency_key="key-123",
    )

    # Replay with same key and payload
    launch2 = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        idempotency_key="key-123",
    )
    assert launch1.id == launch2.id


def test_idempotent_launch_creation_different_payload_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_file = tmp_path / "idem_conflict.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")

    launch_svc = LaunchService(db_mgr, registry)
    launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        idempotency_key="key-conflict",
    )

    import pytest
    with pytest.raises(ValueError, match="Idempotency key conflict"):
        launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v2",  # Different agent_version!
            dataset_name="banking-agent-regression",
            idempotency_key="key-conflict",
        )


def test_non_idempotency_integrity_error_reraised(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_file = tmp_path / "non_idem.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")

    launch_svc = LaunchService(db_mgr, registry)

    # Trigger PK violation on launch_id without idempotency_key
    launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        launch_id="fixed-pk-1",
    )

    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            launch_id="fixed-pk-1",  # Same PK, no idempotency key
        )

