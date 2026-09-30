from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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


def test_explicit_langfuse_dataset_client_is_authoritative_in_seed_mode():
    dataset_version = datetime(2026, 9, 28, tzinfo=UTC)
    dataset = SimpleNamespace(
        id="langfuse-dataset-id",
        version=dataset_version,
        items=[
            SimpleNamespace(
                id="langfuse-case-1",
                input={"question": "explicit client"},
                expected_output={"answer": "pinned"},
                metadata={"source": "test"},
            )
        ],
    )

    snapshot = DatasetResolver(source="seed").resolve(
        "banking-agent-regression",
        dataset_version=dataset_version.isoformat(),
        dataset_client=dataset,
    )

    assert snapshot["source"] == "langfuse"
    assert snapshot["dataset_id"] == "langfuse-dataset-id"
    assert snapshot["dataset_version"] == dataset_version.isoformat()
    assert snapshot["items"][0]["id"] == "langfuse-case-1"


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

    # Issue #83: the quality policy is a real, digested decision — not a
    # placeholder — and it is recorded separately from what was measured.
    policy = manifest["quality_policy"]
    assert policy["policy_id"] == "default-all-required"
    assert policy["policy_digest"].startswith("sha256:")
    assert [rule["evaluator_id"] for rule in policy["rules"]] == ["pii_safe"]
    assert policy["rules"][0]["operator"] == ">="
    assert policy["rules"][0]["threshold"] == 1.0
    assert policy["rules"][0]["required"] is True
    assert manifest["measurement_digest"].startswith("sha256:")


def test_parse_dataset_version_contract():
    from datetime import datetime, timezone

    from app.dataset import parse_dataset_version

    assert parse_dataset_version(None) is None
    assert parse_dataset_version("") is None

    # Valid UTC ISO-8601 strings
    dt1 = parse_dataset_version("2026-09-01T10:00:00Z")
    assert dt1 == datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)

    dt2 = parse_dataset_version("2026-09-01T10:00:00+00:00")
    assert dt2 == datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)

    # Valid UTC datetime object
    dt_in = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
    assert parse_dataset_version(dt_in) == dt_in

    # Non-UTC timezone must fail
    from datetime import timedelta
    non_utc = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(ValueError, match="UTC"):
        parse_dataset_version(non_utc)

    # Naive datetime must fail
    naive_dt = datetime(2026, 9, 1, 10, 0, 0)
    with pytest.raises(ValueError, match="UTC"):
        parse_dataset_version(naive_dt)

    # Invalid ISO-8601 strings must fail
    with pytest.raises(ValueError, match="ISO-8601"):
        parse_dataset_version("invalid-date-string")


def test_dataset_resolver_langfuse_passes_version_datetime(monkeypatch):
    from datetime import datetime
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "langfuse")
    resolver = DatasetResolver()

    with patch("app.dataset._get_langfuse_client") as mock_client:
        mock_lf = MagicMock()
        mock_ds = MagicMock()
        mock_ds.id = "lf-ds-1"
        mock_ds.version = "2026-09-01T10:00:00+00:00"
        mock_ds.items = []
        mock_lf.get_dataset.return_value = mock_ds
        mock_client.return_value = mock_lf

        snapshot = resolver.resolve("test-ds", "2026-09-01T10:00:00Z")

        # Assert Langfuse SDK get_dataset was called with version=<UTC datetime>
        expected_dt = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
        mock_lf.get_dataset.assert_called_once_with("test-ds", version=expected_dt)
        assert snapshot["dataset_version"] == "2026-09-01T10:00:00+00:00"


def test_launch_dataset_version_persisted_in_db_and_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    db_file = tmp_path / "test_version_db.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    reg = AgentRegistry(db_mgr)
    reg.create_agent("agent-1", "Agent One")
    reg.create_version(agent_id="agent-1", version="v1", endpoint="http://example.com/api")
    launch_svc = LaunchService(db_mgr, reg)

    # When dataset_version is not provided in seed mode, it resolves to seed version
    launch = launch_svc.create_launch(
        agent_id="agent-1",
        agent_version="v1",
        dataset_name="banking-agent-regression",
    )
    # DB column must be populated, NOT NULL
    assert launch.dataset_version is not None
    assert launch.dataset_version == launch.manifest["dataset"]["dataset_version"]



def test_dataset_resolver_pins_langfuse_latest_to_request_timestamp(monkeypatch):
    from datetime import datetime as RealDateTime

    from app.dataset import DatasetResolver

    pinned = RealDateTime(2026, 9, 28, 12, 0, tzinfo=UTC)

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return pinned

    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "langfuse")
    monkeypatch.setattr("app.dataset.datetime", FixedDateTime)
    mock_lf = MagicMock()
    mock_ds = MagicMock()
    mock_ds.id = "dataset-1"
    mock_ds.version = None
    mock_ds.items = []
    mock_lf.get_dataset.return_value = mock_ds
    with patch("app.dataset._get_langfuse_client", return_value=mock_lf):
        snapshot = DatasetResolver().resolve("golden")

    mock_lf.get_dataset.assert_called_once_with("golden", version=pinned)
    assert snapshot["dataset_version"] == pinned.isoformat()


def test_dataset_resolver_seed_rejects_unknown_version(monkeypatch):
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    with pytest.raises(ValueError, match="unavailable in seed source"):
        DatasetResolver().resolve("banking-agent-regression", "2026-09-01T00:00:00Z")
