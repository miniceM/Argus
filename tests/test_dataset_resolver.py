from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.dataset import DatasetResolver  # noqa: E402
from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.manifest import LaunchService  # noqa: E402
from app.registry import AgentRegistry  # noqa: E402


def test_dataset_resolver_langfuse_fails_fast(monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "langfuse")
    resolver = DatasetResolver()

    with patch("app.dataset._get_langfuse_client") as mock_client:
        mock_lf = MagicMock()
        mock_lf.get_dataset.side_effect = RuntimeError("Langfuse API connection timeout")
        mock_client.return_value = mock_lf

        # Must fail fast, NEVER silently fall back to seed
        with pytest.raises(RuntimeError, match="Langfuse API connection timeout"):
            resolver.resolve("any-dataset")


def test_dataset_resolver_seed_name_mismatch_fails_fast(monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    resolver = DatasetResolver()

    # Requested dataset does not match dataset in data/dataset.json
    with pytest.raises(ValueError, match="not found in seed"):
        resolver.resolve("non-existent-seed-dataset")


def test_dataset_resolver_seed_success(monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    resolver = DatasetResolver()

    snapshot = resolver.resolve("banking-agent-regression")
    assert snapshot["source"] == "seed"
    assert snapshot["dataset_name"] == "banking-agent-regression"
    assert snapshot["dataset_id"] is not None
    assert len(snapshot["snapshot_digest"]) == 64
    assert snapshot["items_count"] == 6
    assert len(snapshot["items"]) == 6

    item0 = snapshot["items"][0]
    assert "id" in item0
    assert "input" in item0
    assert "expected_output" in item0


def test_launch_manifest_has_frozen_dataset_and_evaluators(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_file = tmp_path / "test_freeze.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    reg = AgentRegistry(db_mgr)
    reg.create_agent("agent-1", "Agent One")
    reg.create_version(
        agent_id="agent-1",
        version="v1",
        endpoint="http://example.com/api",
        is_idempotent=True,
    )

    launch_svc = LaunchService(db_mgr, reg, runner_version="0.2.0")

    # Create launch with custom evaluator
    launch = launch_svc.create_launch(
        agent_id="agent-1",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        evaluator_ids=["pii_safe"],
    )

    assert launch.dataset_id is not None
    manifest = launch.manifest
    assert manifest["dataset"]["source"] == "seed"
    assert len(manifest["dataset"]["items"]) == 6
    assert manifest["dataset"]["snapshot_digest"] is not None

    # Evaluators must only contain pii_safe
    assert len(manifest["evaluators"]) == 1
    assert manifest["evaluators"][0]["id"] == "pii_safe"
    assert manifest["evaluators"][0]["threshold"] == 1.0

    # Quality policy must be explicitly recorded
    assert manifest["quality_policy"]["mode"] == "all_selected_must_pass"
