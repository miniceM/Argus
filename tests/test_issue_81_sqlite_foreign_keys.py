"""Regression: SQLite must enforce foreign keys on *every* pooled connection.

`PRAGMA foreign_keys` is connection-scoped and defaults to OFF. Setting it once
on a pooled connection left all later connections unenforced, so the
delete-Agent / create-Launch concurrency invariant that PostgreSQL enforces via
`ON DELETE RESTRICT` was not actually enforced locally and could be violated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402


def _fk_enabled(db_mgr: DatabaseManager) -> bool:
    with db_mgr.engine.connect() as conn:
        return bool(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())


def test_foreign_keys_are_enforced_on_every_new_connection(tmp_path):
    db_mgr = DatabaseManager(f"sqlite:///{tmp_path / 'fk.db'}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    # Force several fresh connections through the pool, not just the first one.
    assert all(_fk_enabled(db_mgr) for _ in range(5))


def test_orphan_launch_row_is_rejected(tmp_path):
    """A launch pointing at a non-existent AgentVersion must not be storable."""
    db_mgr = DatabaseManager(f"sqlite:///{tmp_path / 'fk_orphan.db'}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    from app.db_models import ExperimentLaunchRecord

    with pytest.raises(IntegrityError):
        with db_mgr.get_session() as session:
            session.add(
                ExperimentLaunchRecord(
                    id="launch-orphan",
                    name="orphan",
                    status="PENDING",
                    quality_conclusion="unknown",
                    agent_id="ghost-agent",
                    agent_version="v1",
                    agent_version_id="ghost-version",
                    dataset_name="d",
                    manifest={},
                )
            )
            session.commit()
