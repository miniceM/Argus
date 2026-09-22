from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db_models import AgentRecord, AgentVersionRecord  # noqa: E402
from app.main import app, db_manager  # noqa: E402


@pytest.fixture(autouse=True)
def setup_api_data():
    from app.db import MigrationRunner
    from app.db_models import (
        ExecutionAttemptRecord,
        ExperimentItemExecutionRecord,
        ExperimentLaunchRecord,
    )
    MigrationRunner(db_manager.engine, ROOT / "migrations").apply_all()
    with db_manager.get_session() as session:
        session.query(ExecutionAttemptRecord).delete()
        session.query(ExperimentItemExecutionRecord).delete()
        session.query(ExperimentLaunchRecord).delete()
        session.query(AgentVersionRecord).delete()
        session.query(AgentRecord).delete()
        session.flush()
        a = AgentRecord(id="banking-agent", name="Banking Agent")
        session.add(a)
        session.flush()
        v = AgentVersionRecord(
            id="banking-agent-v1",
            agent_id="banking-agent",
            version="v1",
            endpoint="http://localhost:8080/invoke",
            method="POST",
            timeout_seconds=5,
            max_retries=2,
            rate_limit_per_minute=600,
            max_concurrency=4,
            request_mapping={},
            is_idempotent=False,
            spec_digest="sha256:123",
        )
        session.add(v)


def test_async_launch_api_lifecycle():
    client = TestClient(app)

    # 1. Create Launch
    res = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "dataset_version": "v1.0",
            "name": "API Test Launch",
        },
    )
    assert res.status_code == 201, res.text
    data = res.json()
    launch_id = data["id"]
    assert data["status"] == "PENDING"
    assert data["progress"]["total"] > 0
    assert data["progress"]["pending"] > 0
    assert "cancel" not in data["allowed_actions"]

    # 2. Trigger asynchronous run -> Returns 202 QUEUED
    run_res = client.post(f"/api/v1/experiment-launches/{launch_id}/run")
    assert run_res.status_code == 202, run_res.text
    run_data = run_res.json()
    assert run_data["launch_id"] == launch_id
    assert run_data["status"] == "QUEUED"

    # 3. Query Launch Detail via RESTful path
    get_res = client.get(f"/api/v1/experiment-launches/{launch_id}")
    assert get_res.status_code == 200
    detail = get_res.json()
    assert detail["status"] == "QUEUED"
    assert "cancel" in detail["allowed_actions"]

    # 4. Cancel Launch
    cancel_res = client.post(f"/api/v1/experiment-launches/{launch_id}/cancel")
    assert cancel_res.status_code == 200
    cancel_data = cancel_res.json()
    assert cancel_data["status"] in ("CANCELLING", "CANCELLED")

    # 5. Items query with status filter
    items_res = client.get(f"/api/v1/experiment-launches/{launch_id}/items?status=cancelled")
    assert items_res.status_code == 200
    items = items_res.json()
    assert len(items) > 0
    assert items[0]["execution_status"] == "cancelled"

    # 6. Resume Launch
    resume_res = client.post(f"/api/v1/experiment-launches/{launch_id}/resume")
    assert resume_res.status_code == 200
    assert resume_res.json()["status"] == "QUEUED"


def test_async_launch_api_retry_failed():
    client = TestClient(app)

    # 1. Create Launch & Run
    res = client.post(
        "/api/v1/experiment-launches",
        json={
            "agent_id": "banking-agent",
            "agent_version": "v1",
            "dataset_name": "banking-agent-regression",
            "dataset_version": "v1.0",
            "name": "Retry Failed API Test",
        },
    )
    assert res.status_code == 201
    launch_id = res.json()["id"]
    client.post(f"/api/v1/experiment-launches/{launch_id}/run")

    # Manually simulate a failed item with AMBIGUOUS_OUTCOME in DB
    import datetime

    from app.db_models import ExecutionAttemptRecord, ExperimentItemExecutionRecord, ExperimentLaunchRecord

    with db_manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.status = "PARTIAL_FAILED"
        item = session.query(ExperimentItemExecutionRecord).filter_by(launch_id=launch_id).first()
        item.execution_status = "failed"
        item_id = item.id

        att = ExecutionAttemptRecord(
            id="att-ambiguous-api",
            item_execution_id=item_id,
            attempt_no=1,
            status="FAILED",
            error_type="AMBIGUOUS_OUTCOME",
            error_message="Worker crash during MAY_HAVE_BEEN_SENT",
            latency_ms=100,
            trace_context_received=False,
            started_at=datetime.datetime.now(datetime.UTC),
            completed_at=datetime.datetime.now(datetime.UTC),
        )
        session.add(att)
        session.commit()

    # 2. Query attempts endpoint
    att_res = client.get(f"/api/v1/experiment-item-executions/{item_id}/attempts")
    assert att_res.status_code == 200
    atts = att_res.json()
    assert len(atts) == 1
    assert atts[0]["error_type"] == "AMBIGUOUS_OUTCOME"

    # 3. Retry-failed without force -> should return 409 Conflict
    retry_res = client.post(f"/api/v1/experiment-launches/{launch_id}/retry-failed", json={"force": False})
    assert retry_res.status_code == 409, retry_res.text
    assert "force" in retry_res.json()["detail"].lower()

    # 4. Retry-failed with force=True -> should succeed with 200 QUEUED
    retry_force_res = client.post(f"/api/v1/experiment-launches/{launch_id}/retry-failed", json={"force": True})
    assert retry_force_res.status_code == 200, retry_force_res.text
    assert retry_force_res.json()["status"] == "QUEUED"
