import os
import sys
from pathlib import Path

import pytest

# Ensure tests default to test DB mode unless explicitly overridden
os.environ.setdefault("ARGUS_DB_MODE", "test")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))


@pytest.fixture
def setup_runtime(tmp_path):
    from app.db import DatabaseManager, MigrationRunner
    from app.db_models import AgentRecord, AgentVersionRecord
    from app.limiter import MemoryAgentLimiter
    from app.orchestrator import LaunchOrchestrator
    from app.queue import MemoryQueueAdapter
    from app.reconciler import ExecutionReconciler
    from app.worker import ExecutionWorker

    db_file = tmp_path / "runtime_test.db"
    db_url = f"sqlite:///{db_file}"
    db_mgr = DatabaseManager(db_url)
    MigrationRunner(db_mgr.engine, ROOT / "migrations").apply_all()

    queue = MemoryQueueAdapter()
    limiter = MemoryAgentLimiter()

    with db_mgr.get_session() as session:
        a_rec = AgentRecord(id="test-agent", name="Test Agent")
        session.add(a_rec)
        session.flush()

        v_rec = AgentVersionRecord(
            id="test-agent-v1",
            agent_id="test-agent",
            version="v1",
            endpoint="http://localhost:8080/invoke",
            method="POST",
            timeout_seconds=5,
            max_retries=2,
            max_concurrency=5,
            rate_limit_per_minute=60,
            request_mapping={"input": "text"},
            is_idempotent=False,
            spec_digest="sha256:abc",
        )
        session.add(v_rec)

    orchestrator = LaunchOrchestrator(db_mgr, queue, limiter)
    worker = ExecutionWorker(db_mgr, queue, limiter, worker_id="worker-test-1")
    reconciler = ExecutionReconciler(db_mgr, queue, limiter)

    return db_mgr, queue, limiter, orchestrator, worker, reconciler
