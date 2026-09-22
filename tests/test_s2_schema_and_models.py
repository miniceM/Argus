from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager  # noqa: E402
from app.db_models import (  # noqa: E402
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)


def test_s2_schema_fields_exist():
    """Verify that new S2 fields exist on the ORM models."""
    launch_cols = {c.name for c in inspect(ExperimentLaunchRecord).columns}
    assert "cancel_requested_at" in launch_cols
    assert "updated_at" in launch_cols
    assert "status_reason" in launch_cols

    item_cols = {c.name for c in inspect(ExperimentItemExecutionRecord).columns}
    assert "queued_at" in item_cols
    assert "available_at" in item_cols
    assert "lease_owner" in item_cols
    assert "lease_token" in item_cols
    assert "lease_expires_at" in item_cols
    assert "dispatch_generation" in item_cols
    assert "updated_at" in item_cols

    attempt_cols = {c.name for c in inspect(ExecutionAttemptRecord).columns}
    assert "worker_id" in attempt_cols
    assert "request_phase" in attempt_cols


def test_migration_004_applies_cleanly(tmp_path):
    from app.db import MigrationRunner
    db_file = tmp_path / "test_mig_s2.db"
    db_url = f"sqlite:///{db_file}"
    db_mgr = DatabaseManager(db_url)
    runner = MigrationRunner(db_mgr.engine, ROOT / "migrations")
    applied = runner.apply_all()
    assert "004_s2_reliable_runtime.sql" in applied
    # Verify tables have the new columns via direct inspection
    with db_mgr.get_session() as session:
        res = session.connection().exec_driver_sql("PRAGMA table_info(experiment_launches)").fetchall()
        col_names = [r[1] for r in res]
        assert "cancel_requested_at" in col_names
        assert "status_reason" in col_names

        res_item = session.connection().exec_driver_sql("PRAGMA table_info(experiment_item_executions)").fetchall()
        item_cols = [r[1] for r in res_item]
        assert "lease_owner" in item_cols
        assert "lease_token" in item_cols
        assert "dispatch_generation" in item_cols
        assert "available_at" in item_cols

        res_att = session.connection().exec_driver_sql("PRAGMA table_info(execution_attempts)").fetchall()
        att_cols = [r[1] for r in res_att]
        assert "worker_id" in att_cols
        assert "request_phase" in att_cols
