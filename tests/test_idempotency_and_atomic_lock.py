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



def test_idempotent_replay_succeeds_even_when_dataset_source_down(tmp_path, monkeypatch):
    """If a Launch with same idempotency_key exists, re-request must return it without accessing Langfuse/seed."""
    from unittest.mock import patch

    db_file = tmp_path / "idem_down.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")

    launch_svc = LaunchService(db_mgr, registry)
    launch1 = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        idempotency_key="key-langfuse-down",
    )

    # Now simulate Langfuse / DatasetResolver completely broken
    with patch("app.manifest.DatasetResolver") as mock_resolver_cls:
        mock_resolver = mock_resolver_cls.return_value
        mock_resolver.resolve.side_effect = RuntimeError("Langfuse is down (503 Service Unavailable)")

        # Replaying identical request MUST succeed by returning cached launch, without touching DatasetResolver
        launch2 = launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            idempotency_key="key-langfuse-down",
        )
        assert launch2.id == launch1.id
        mock_resolver.resolve.assert_not_called()


def test_concurrent_idempotent_launch_creation(tmp_path):
    """Concurrent threads attempting to create launch with same idempotency_key must all succeed with same launch."""
    from concurrent.futures import ThreadPoolExecutor

    db_file = tmp_path / "idem_concurrent.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")
    launch_svc = LaunchService(db_mgr, registry)

    results = []

    def _worker(thread_idx):
        return launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            idempotency_key="concurrent-key-same",
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_worker, i) for i in range(5)]
        for f in futures:
            results.append(f.result())

    # All 5 concurrent workers must have received the exact same Launch ID
    assert len(results) == 5
    first_id = results[0].id
    for r in results:
        assert r.id == first_id


def test_idempotent_replay_succeeds_even_when_evaluator_unregistered(tmp_path):
    """If a Launch with same idempotency_key exists, re-request must return it without calling EvaluatorRegistry."""
    from unittest.mock import patch

    db_file = tmp_path / "idem_eval.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")

    launch_svc = LaunchService(db_mgr, registry)
    launch1 = launch_svc.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        evaluator_ids=["intent_match"],
        idempotency_key="key-eval-unregistered",
    )

    # Now simulate EvaluatorRegistry completely unregistering or failing on the evaluator
    with patch("app.manifest.default_evaluator_registry.resolve") as mock_resolve:
        mock_resolve.side_effect = KeyError("Evaluator 'intent_match' has been deprecated/removed")

        # Replay with same key and payload MUST succeed by returning existing launch without calling resolve
        launch2 = launch_svc.create_launch(
            agent_id="banking-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            evaluator_ids=["intent_match"],
            idempotency_key="key-eval-unregistered",
        )
        assert launch2.id == launch1.id
        mock_resolve.assert_not_called()


def test_concurrent_delete_agent_and_create_launch(tmp_path):
    """Verifies concurrency safety between delete_agent and create_launch:
    either delete succeeds and create is rejected, or create succeeds and delete is rejected.
    No orphaned launches or corrupt state can occur."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.db_models import ExperimentLaunchRecord
    from app.models import AgentConcurrencyError, AgentHasActiveLaunchesError

    db_file = tmp_path / "concurrent_del_launch.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.create_agent("target-agent", name="Target Agent")
    registry.create_version(
        agent_id="target-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    launch_svc = LaunchService(db_mgr, registry)

    results = {"delete": None, "create": None, "del_err": None, "create_err": None}
    barrier = threading.Barrier(2)

    def _do_delete():
        barrier.wait()
        try:
            results["delete"] = registry.delete_agent("target-agent", force=True, confirm_name="Target Agent")
        except Exception as exc:
            results["del_err"] = exc

    def _do_create():
        barrier.wait()
        try:
            results["create"] = launch_svc.create_launch(
                agent_id="target-agent",
                agent_version="v1",
                dataset_name="banking-agent-regression",
            )
        except Exception as exc:
            results["create_err"] = exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_do_delete)
        f2 = pool.submit(_do_create)
        f1.result()
        f2.result()

    # Core Invariants:
    # 1. Both must never succeed simultaneously (orphaned launch invariant)
    assert not (results["delete"] is not None and results["create"] is not None), "Both operations must not succeed simultaneously"
    # 2. Exactly one succeeded, one failed
    assert (results["delete"] is not None) ^ (results["create"] is not None)

    if results["delete"] is not None:
        assert results["del_err"] is None
        assert results["create_err"] is not None
        assert isinstance(results["create_err"], ValueError)
        assert registry.get_agent("target-agent") is None
    else:
        assert results["create"] is not None
        assert results["del_err"] is not None
        # In concurrent scheduling, delete is blocked by either active launch check or database foreign key concurrency error
        assert isinstance(results["del_err"], (AgentHasActiveLaunchesError, AgentConcurrencyError))
        assert registry.get_agent("target-agent") is not None
        # Verify launch record exists consistently in DB
        with db_mgr.get_session() as session:
            launch_rec = session.get(ExperimentLaunchRecord, results["create"].id)
            assert launch_rec is not None
            assert launch_rec.agent_id == "target-agent"


def test_postgres_row_level_lock_mutual_exclusion_agent_delete_and_launch():
    """Validates real PostgreSQL row-level exclusive lock (SELECT ... FOR UPDATE)
    mutual exclusion between Agent deletion and Launch creation transactions."""
    import os
    import time
    from pathlib import Path

    import pytest
    from app.db import DatabaseManager, MigrationRunner
    from app.db_models import AgentRecord
    from sqlalchemy import select, text

    pg_url = os.getenv("TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("TEST_POSTGRES_URL is not configured; skipping real PostgreSQL row lock test")

    root_dir = Path(__file__).resolve().parents[1]
    db_mgr = DatabaseManager(pg_url)
    runner = MigrationRunner(engine=db_mgr.engine, migrations_dir=root_dir / "migrations")
    runner.apply_all()

    with db_mgr.engine.connect() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE execution_attempts, experiment_item_executions, "
                "langfuse_sync_tasks, experiment_launches, agent_versions, agents CASCADE;"
            )
        )
        conn.execute(text("INSERT INTO agents (id, name, status) VALUES ('pg-lock-agent', 'PG Agent', 'active');"))
        conn.execute(
            text(
                "INSERT INTO agent_versions (id, agent_id, version, spec_digest, endpoint) "
                "VALUES ('pg-ver-1', 'pg-lock-agent', 'v1', 'digest1', 'http://localhost:8080/invoke');"
            )
        )
        conn.commit()

    import threading

    lock_acquired = threading.Event()
    waiter_started = threading.Event()
    waiter_completed = threading.Event()
    waiter_observed_status = []

    def lock_holder():
        with db_mgr.get_session() as session:
            # Transaction 1 acquires exclusive row lock on the agent
            agent = session.scalar(
                select(AgentRecord).where(AgentRecord.id == "pg-lock-agent").with_for_update()
            )
            assert agent is not None
            # Mutate status to deleting
            agent.status = "deleting"
            session.flush()
            lock_acquired.set()

            # Wait for Transaction 2 to attempt acquiring lock
            waiter_started.wait(timeout=5)
            time.sleep(0.5)
            # Commit transaction, releasing the row lock
            session.commit()

    def lock_waiter():
        lock_acquired.wait(timeout=5)
        with db_mgr.get_session() as session:
            waiter_started.set()
            # Transaction 2 will block here until Transaction 1 commits
            agent = session.scalar(
                select(AgentRecord).where(AgentRecord.id == "pg-lock-agent").with_for_update()
            )
            assert agent is not None
            waiter_observed_status.append(agent.status)
            session.commit()
        waiter_completed.set()

    t1 = threading.Thread(target=lock_holder)
    t2 = threading.Thread(target=lock_waiter)

    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert waiter_completed.is_set()
    # Transaction 2 was blocked until Transaction 1 committed, observing the updated 'deleting' status
    assert waiter_observed_status == ["deleting"]



