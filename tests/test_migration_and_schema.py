from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402


def test_missing_database_url_fails_fast_in_normal_mode(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ARGUS_DB_MODE", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is not configured"):
        DatabaseManager.from_env()


def test_migration_runner_applies_and_records_schema(tmp_path):
    db_file = tmp_path / "test.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)

    migrations_dir = ROOT / "migrations"
    runner = MigrationRunner(engine=engine, migrations_dir=migrations_dir)
    applied = runner.apply_all()
    assert "001_initial_schema.sql" in applied

    # Check schema_migrations table
    with engine.connect() as conn:
        res = conn.execute(text("SELECT version, checksum FROM schema_migrations")).fetchall()
        assert len(res) >= 1
        assert res[0][0] == "001_initial_schema.sql"

    # Idempotent re-run
    applied_again = runner.apply_all()
    assert len(applied_again) == 0


def test_schema_constraints_enforced(tmp_path):
    db_file = tmp_path / "test_constraints.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)

    # Enable foreign keys for sqlite connection
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))

    runner = MigrationRunner(engine=engine, migrations_dir=ROOT / "migrations")
    runner.apply_all()

    with engine.connect() as conn:
        # Insert agent
        conn.execute(
            text("INSERT INTO agents (id, name, status) VALUES (:id, :name, :status)"),
            {"id": "agent-1", "name": "Agent One", "status": "active"},
        )
        # Insert version
        conn.execute(
            text(
                "INSERT INTO agent_versions (id, agent_id, version, spec_digest, endpoint) "
                "VALUES (:id, :agent_id, :version, :spec_digest, :endpoint)"
            ),
            {
                "id": "v1-id",
                "agent_id": "agent-1",
                "version": "v1",
                "spec_digest": "digest1",
                "endpoint": "http://localhost:8080/invoke",
            },
        )
        conn.commit()

        # Duplicate (agent_id, version) must fail
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO agent_versions (id, agent_id, version, spec_digest, endpoint) "
                    "VALUES (:id, :agent_id, :version, :spec_digest, :endpoint)"
                ),
                {
                    "id": "v1-dup-id",
                    "agent_id": "agent-1",
                    "version": "v1",
                    "spec_digest": "digest2",
                    "endpoint": "http://localhost:8080/invoke2",
                },
            )
            conn.commit()
        conn.rollback()

        # Insert Launch
        conn.execute(
            text(
                "INSERT INTO experiment_launches "
                "(id, name, dataset_name, agent_id, agent_version, agent_version_id, manifest, idempotency_key) "
                "VALUES (:id, :name, :d_name, :a_id, :a_v, :a_v_id, :manifest, :idem_key)"
            ),
            {
                "id": "launch-1",
                "name": "Launch 1",
                "d_name": "dataset-1",
                "a_id": "agent-1",
                "a_v": "v1",
                "a_v_id": "v1-id",
                "manifest": '{"schema_version": "1.0"}',
                "idem_key": "idem-key-1",
            },
        )
        conn.commit()

        # Duplicate idempotency_key must fail
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO experiment_launches "
                    "(id, name, dataset_name, agent_id, agent_version, agent_version_id, manifest, idempotency_key) "
                    "VALUES (:id, :name, :d_name, :a_id, :a_v, :a_v_id, :manifest, :idem_key)"
                ),
                {
                    "id": "launch-2",
                    "name": "Launch 2",
                    "d_name": "dataset-1",
                    "a_id": "agent-1",
                    "a_v": "v1",
                    "a_v_id": "v1-id",
                    "manifest": '{"schema_version": "1.0"}',
                    "idem_key": "idem-key-1",
                },
            )
            conn.commit()
        conn.rollback()

        # Insert item execution
        conn.execute(
            text(
                "INSERT INTO experiment_item_executions (id, launch_id, dataset_item_id) "
                "VALUES (:id, :launch_id, :d_item_id)"
            ),
            {"id": "item-1", "launch_id": "launch-1", "d_item_id": "d-item-1"},
        )
        conn.commit()

        # Duplicate (launch_id, dataset_item_id) must fail
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO experiment_item_executions (id, launch_id, dataset_item_id) "
                    "VALUES (:id, :launch_id, :d_item_id)"
                ),
                {"id": "item-2", "launch_id": "launch-1", "d_item_id": "d-item-1"},
            )
            conn.commit()
        conn.rollback()

        # Insert execution attempt
        conn.execute(
            text(
                "INSERT INTO execution_attempts (id, item_execution_id, attempt_no) "
                "VALUES (:id, :item_id, :attempt_no)"
            ),
            {"id": "att-1", "item_id": "item-1", "attempt_no": 1},
        )
        conn.commit()

        # Duplicate (item_execution_id, attempt_no) must fail
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO execution_attempts (id, item_execution_id, attempt_no) "
                    "VALUES (:id, :item_id, :attempt_no)"
                ),
                {"id": "att-2", "item_id": "item-1", "attempt_no": 1},
            )
            conn.commit()
        conn.rollback()


def test_migration_checksum_tamper_fails(tmp_path):
    db_file = tmp_path / "tamper_test.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)

    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    f1 = mig_dir / "001_test.sql"
    f1.write_text("CREATE TABLE t1 (id VARCHAR(32) PRIMARY KEY);", encoding="utf-8")

    runner = MigrationRunner(engine=engine, migrations_dir=mig_dir)
    applied = runner.apply_all()
    assert "001_test.sql" in applied

    # Tamper with file content
    f1.write_text("CREATE TABLE t1 (id VARCHAR(32) PRIMARY KEY, name TEXT);", encoding="utf-8")

    # Re-running must fail fast on checksum mismatch
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        runner.apply_all()


def test_002_migration_applied_successfully(tmp_path):
    db_file = tmp_path / "test_002.db"
    db_url = f"sqlite:///{db_file}"
    engine = create_engine(db_url)

    runner = MigrationRunner(engine=engine, migrations_dir=ROOT / "migrations")
    applied = runner.apply_all()
    assert "001_initial_schema.sql" in applied
    assert "002_final_attempt_fk.sql" in applied

