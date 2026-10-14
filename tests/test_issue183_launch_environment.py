"""环境管理列不等于冻结证据；迁移失败不得登记成功。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from app.db import DatabaseManager, MigrationRunner
from app.manifest import LaunchService
from app.registry import AgentRegistry
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = "018_launch_environment.sql"


def legacy_db(tmp_path, manifest):
    db = DatabaseManager(f"sqlite:///{tmp_path / 'legacy.db'}")
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE experiment_launches (id VARCHAR(64) PRIMARY KEY, agent_id VARCHAR(128) DEFAULT 'agent', manifest JSON NOT NULL)"))
        conn.execute(text("INSERT INTO experiment_launches (id, manifest) VALUES (:id, :manifest)"),
                     {"id": "legacy", "manifest": json.dumps(manifest)})
    folder = tmp_path / "migrations"
    folder.mkdir()
    shutil.copy(ROOT / "migrations" / MIGRATION, folder / MIGRATION)
    return db, MigrationRunner(db.engine, folder)


@pytest.mark.parametrize(("manifest", "expected"), [
    ({"comparison": {"environment": " Test "}, "agent": {"environment": "production"}}, "test"),
    ({"agent": {"environment": "staging"}}, "staging"),
    ({"comparison": {"environment": None}}, "production"),
    ({}, "production"),
])
def test_backfill_preserves_frozen_json_and_is_idempotent(tmp_path, manifest, expected):
    db, runner = legacy_db(tmp_path, manifest)
    assert runner.apply_all() == [MIGRATION]
    with db.engine.connect() as conn:
        row = conn.execute(text("SELECT environment, manifest FROM experiment_launches")).one()
        assert row.environment == expected
        assert json.loads(row.manifest) == manifest
    indexes = inspect(db.engine).get_indexes("experiment_launches")
    assert any(i["column_names"] == ["agent_id", "environment"] for i in indexes)
    assert runner.apply_all() == []


@pytest.mark.parametrize("value", ["bad/env", 42, ["test"]])
def test_invalid_backfill_rolls_back_schema_and_receipt(tmp_path, value):
    db, runner = legacy_db(tmp_path, {"comparison": {"environment": value}})
    with pytest.raises(ValueError, match="legacy"):
        runner.apply_all()
    assert "environment" not in {c["name"] for c in inspect(db.engine).get_columns("experiment_launches")}
    assert runner.get_applied_versions() == {}


def test_creation_persists_normalized_environment(setup_runtime, monkeypatch):
    db, _, _, orchestrator, _, _ = setup_runtime
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    svc = LaunchService(db, AgentRegistry(db), runner_version="0.2.0")
    launch = svc.create_launch(agent_id="test-agent", agent_version="v1",
                               dataset_name="banking-agent-regression", environment=" QA-blue ")
    assert launch.environment == "qa-blue" == launch.manifest["comparison"]["environment"]
    internal = orchestrator.create_launch(agent_id="test-agent", agent_version="v1", dataset_name="ds",
                                           dataset_version="v1", name="internal",
                                           manifest={"comparison": {"environment": "test"}})
    assert internal.environment == "test"


def test_frozen_environment_is_not_normalized_or_inferred():
    from app.environment_contracts import frozen_environment
    assert frozen_environment({"comparison": {"environment": "test"}}) == "test"
    for manifest in ({}, {"agent": {"environment": "test"}},
                     {"comparison": {"environment": " Test "}}):
        assert frozen_environment(manifest) is None
