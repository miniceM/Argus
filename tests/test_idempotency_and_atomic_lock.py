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
