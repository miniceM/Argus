from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.registry import AgentRegistry, compute_spec_digest  # noqa: E402
from app.security import validate_credential_ref, validate_endpoint_url  # noqa: E402


def test_endpoint_security_validation():
    # URL embedded credentials must be rejected
    with pytest.raises(ValueError, match="embedded credentials"):
        validate_endpoint_url("http://admin:secret@demo-agent:8080/invoke")

    # Sensitive query parameter must be rejected
    with pytest.raises(ValueError, match="sensitive query parameters"):
        validate_endpoint_url("http://demo-agent:8080/invoke?token=secret123")

    # Clean URL accepted
    validate_endpoint_url("http://demo-agent:8080/invoke")
    validate_endpoint_url("https://api.example.com/v1/agent")


def test_credential_ref_validation(monkeypatch):
    monkeypatch.setenv("ARGUS_ALLOWED_CREDENTIAL_ENVS", "DEMO_AUTH_TOKEN,TEST_KEY")

    # Allowed env
    validate_credential_ref("env://DEMO_AUTH_TOKEN")
    validate_credential_ref("env://TEST_KEY")

    # Disallowed env
    with pytest.raises(ValueError, match="not in allowed credential whitelist"):
        validate_credential_ref("env://SYSTEM_ROOT_PASSWORD")

    # Unsupported schemes that cannot be resolved must be rejected
    with pytest.raises(ValueError, match="not supported"):
        validate_credential_ref("vault://secret/my-token")

    with pytest.raises(ValueError, match="not supported"):
        validate_credential_ref("k8s-secret://argus-ns/token")

    # Invalid scheme
    with pytest.raises(ValueError, match="Unsupported credential reference scheme"):
        validate_credential_ref("plain://my-secret-token")


def test_spec_digest_deterministic():
    spec1 = {
        "endpoint": "http://demo:8080/invoke",
        "method": "POST",
        "protocol": "HTTP_JSON",
        "request_mapping": {"messages": "input.messages"},
        "timeout_seconds": 10.0,
        "max_retries": 2,
    }
    spec2 = {
        "max_retries": 2,
        "timeout_seconds": 10.0,
        "request_mapping": {"messages": "input.messages"},
        "protocol": "HTTP_JSON",
        "method": "POST",
        "endpoint": "http://demo:8080/invoke",
    }
    digest1 = compute_spec_digest(spec1)
    digest2 = compute_spec_digest(spec2)
    assert digest1 == digest2
    assert len(digest1) == 64


def test_agent_and_version_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_ALLOWED_CREDENTIAL_ENVS", "DEMO_AUTH_TOKEN")
    db_file = tmp_path / "registry_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    # Register Agent
    agent = registry.create_agent(
        agent_id="test-agent",
        name="Test Agent",
        description="A test agent",
        owner="qa-team",
    )
    assert agent.id == "test-agent"

    # Register Version
    version = registry.create_version(
        agent_id="test-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
        method="POST",
        protocol="HTTP_JSON",
        request_mapping={"messages": "input.messages"},
        credential_ref="env://DEMO_AUTH_TOKEN",
        timeout_seconds=15.0,
        max_retries=2,
    )
    assert version.version == "v1"
    assert version.spec_digest is not None

    # Duplicate version must fail
    with pytest.raises(ValueError, match="already exists"):
        registry.create_version(
            agent_id="test-agent",
            version="v1",
            endpoint="http://localhost:8080/invoke",
        )

    # Unsupported protocol/method must fail
    with pytest.raises(ValueError, match="Only HTTP_JSON protocol is supported"):
        registry.create_version(
            agent_id="test-agent",
            version="v2",
            endpoint="http://localhost:8080/invoke",
            protocol="GRPC",
        )

    with pytest.raises(ValueError, match="Only POST method is supported"):
        registry.create_version(
            agent_id="test-agent",
            version="v3",
            endpoint="http://localhost:8080/invoke",
            method="GET",
        )

    # Archive version
    archived = registry.archive_version(agent_id="test-agent", version="v1")
    assert archived.is_active is False


def test_yaml_idempotent_import(tmp_path):
    db_file = tmp_path / "yaml_import_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    yaml_path = ROOT / "config" / "agents.yaml"

    # First import
    results = registry.import_yaml(yaml_path)
    assert len(results["created"]) == 2  # v1 and v2

    # Second import should skip because digest matches
    results2 = registry.import_yaml(yaml_path)
    assert len(results2["created"]) == 0
    assert len(results2["skipped"]) == 2


def test_delete_agent_without_launches(tmp_path):
    db_file = tmp_path / "del_test.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    registry.create_agent(agent_id="test-agent", name="Test Agent")
    registry.create_version(
        agent_id="test-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    assert registry.get_agent("test-agent") is not None
    assert len(registry.list_versions("test-agent")) == 1

    res = registry.delete_agent("test-agent", force=False)
    assert res["id"] == "test-agent"
    assert res["deleted"] is True
    assert res["launches_deleted"] == 0

    assert registry.get_agent("test-agent") is None
    assert len(registry.list_versions("test-agent")) == 0


def test_delete_agent_not_found(tmp_path):
    db_file = tmp_path / "del_not_found.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    with pytest.raises(KeyError, match="not found"):
        registry.delete_agent("non-existent-agent")


def test_agent_summary_includes_launch_and_active_launch_counts(tmp_path):
    from app.db_models import ExperimentLaunchRecord

    db_file = tmp_path / "summary_counts.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    registry.create_agent(agent_id="count-agent", name="Count Agent")
    ver = registry.create_version(
        agent_id="count-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    # Initial summary without launches
    sum_init = registry.get_agent_summary("count-agent")
    assert sum_init["launch_count"] == 0
    assert sum_init["active_launch_count"] == 0

    # Add 1 terminal launch, 1 running launch
    with db_mgr.get_session() as session:
        l1 = ExperimentLaunchRecord(
            id="launch-completed",
            name="L1",
            agent_id="count-agent",
            agent_version="v1",
            agent_version_id=ver.id,
            dataset_name="banking-reg",
            status="COMPLETED",
            manifest={},
        )
        l2 = ExperimentLaunchRecord(
            id="launch-running",
            name="L2",
            agent_id="count-agent",
            agent_version="v1",
            agent_version_id=ver.id,
            dataset_name="banking-reg",
            status="RUNNING",
            manifest={},
        )
        session.add_all([l1, l2])
        session.commit()

    sum_after = registry.get_agent_summary("count-agent")
    assert sum_after["launch_count"] == 2
    assert sum_after["active_launch_count"] == 1

    list_sums = registry.list_agents_summary()
    assert len(list_sums) == 1
    assert list_sums[0]["launch_count"] == 2
    assert list_sums[0]["active_launch_count"] == 1


def test_delete_agent_with_launches_rejected_when_not_force(tmp_path):
    from app.db_models import ExperimentLaunchRecord
    from app.models import AgentHasLaunchesError

    db_file = tmp_path / "del_rejected.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    registry.create_agent(agent_id="bank-agent", name="Bank Agent")
    ver = registry.create_version(
        agent_id="bank-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id="launch-100",
            name="Launch 100",
            agent_id="bank-agent",
            agent_version="v1",
            agent_version_id=ver.id,
            dataset_name="banking-reg",
            status="COMPLETED",
            manifest={},
        )
        session.add(launch)
        session.commit()

    with pytest.raises(AgentHasLaunchesError) as exc_info:
        registry.delete_agent("bank-agent", force=False)

    assert exc_info.value.code == "AGENT_HAS_LAUNCHES"
    assert exc_info.value.launch_count == 1
    assert "关联的评测记录" in str(exc_info.value)

    # Agent still exists
    assert registry.get_agent("bank-agent") is not None


def test_delete_agent_force_server_side_name_validation(tmp_path):
    from app.db_models import ExperimentLaunchRecord
    from app.models import AgentNameMismatchError

    db_file = tmp_path / "del_name_val.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    registry.create_agent(agent_id="secure-agent", name="Secure Banking Agent")
    ver = registry.create_version(
        agent_id="secure-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id="launch-sec",
            name="Launch Sec",
            agent_id="secure-agent",
            agent_version="v1",
            agent_version_id=ver.id,
            dataset_name="banking-reg",
            status="COMPLETED",
            manifest={},
        )
        session.add(launch)
        session.commit()

    # 1. Missing or wrong name must be rejected with AgentNameMismatchError
    with pytest.raises(AgentNameMismatchError) as exc1:
        registry.delete_agent("secure-agent", force=True, confirm_name=None)
    assert exc1.value.code == "AGENT_NAME_MISMATCH"

    with pytest.raises(AgentNameMismatchError) as exc2:
        registry.delete_agent("secure-agent", force=True, confirm_name="Secure Banking")
    assert exc2.value.code == "AGENT_NAME_MISMATCH"

    # Strict match: whitespace mismatch also rejected
    with pytest.raises(AgentNameMismatchError):
        registry.delete_agent("secure-agent", force=True, confirm_name=" Secure Banking Agent ")

    # 2. Correct name succeeds via delete_agent(force=True, confirm_name=...) or purge_agent(...)
    res = registry.purge_agent("secure-agent", confirm_name="Secure Banking Agent")
    assert res["deleted"] is True
    assert res["launches_deleted"] == 1
    assert registry.get_agent("secure-agent") is None


def test_delete_agent_force_cleans_launches_and_leaves_langfuse_untouched(tmp_path):
    from app.db_models import (
        ExecutionAttemptRecord,
        ExperimentItemExecutionRecord,
        ExperimentLaunchRecord,
        LangfuseSyncTaskRecord,
    )

    db_file = tmp_path / "del_force.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    registry.create_agent(agent_id="force-agent", name="Force Agent")
    ver = registry.create_version(
        agent_id="force-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id="launch-force-1",
            name="Force Launch",
            agent_id="force-agent",
            agent_version="v1",
            agent_version_id=ver.id,
            dataset_name="banking-reg",
            status="COMPLETED",
            manifest={},
        )
        session.add(launch)
        session.flush()

        item = ExperimentItemExecutionRecord(
            id="item-force-1",
            launch_id="launch-force-1",
            dataset_item_id="ds-item-1",
        )
        session.add(item)
        session.flush()

        att = ExecutionAttemptRecord(
            id="att-force-1",
            item_execution_id="item-force-1",
            attempt_no=1,
        )
        session.add(att)

        task = LangfuseSyncTaskRecord(
            id="sync-force-1",
            launch_id="launch-force-1",
            item_id="item-force-1",
            dataset_item_id="ds-item-1",
            dispatch_generation=1,
            trace_id="trace-123",
            dataset_run_name="run-1",
            scores_payload={},
        )
        session.add(task)
        session.commit()

    # Perform force delete with confirmed name
    res = registry.delete_agent("force-agent", force=True, confirm_name="Force Agent")
    assert res["id"] == "force-agent"
    assert res["deleted"] is True
    assert res["launches_deleted"] == 1

    # Verify everything in local DB is cleaned up
    assert registry.get_agent("force-agent") is None
    assert len(registry.list_versions("force-agent")) == 0

    with db_mgr.get_session() as session:
        assert session.get(ExperimentLaunchRecord, "launch-force-1") is None
        assert session.get(ExperimentItemExecutionRecord, "item-force-1") is None
        assert session.get(ExecutionAttemptRecord, "att-force-1") is None
        assert session.get(LangfuseSyncTaskRecord, "sync-force-1") is None


def test_delete_agent_force_rejects_active_launches(tmp_path):
    from app.db_models import ExperimentLaunchRecord
    from app.models import AgentHasActiveLaunchesError

    db_file = tmp_path / "del_active.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()

    registry = AgentRegistry(db_mgr)
    registry.create_agent(agent_id="active-agent", name="Active Agent")
    ver = registry.create_version(
        agent_id="active-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
    )

    # Test each active status: PENDING, QUEUED, RUNNING, CANCELLING
    for active_status in ("PENDING", "QUEUED", "RUNNING", "CANCELLING"):
        with db_mgr.get_session() as session:
            launch = ExperimentLaunchRecord(
                id=f"launch-{active_status.lower()}",
                name=f"Launch {active_status}",
                agent_id="active-agent",
                agent_version="v1",
                agent_version_id=ver.id,
                dataset_name="banking-reg",
                status=active_status,
                manifest={},
            )
            session.add(launch)
            session.commit()

        with pytest.raises(AgentHasActiveLaunchesError) as exc_info:
            registry.delete_agent("active-agent", force=True, confirm_name="Active Agent")

        assert exc_info.value.code == "AGENT_HAS_ACTIVE_LAUNCHES"
        assert exc_info.value.active_launch_count >= 1

        # Confirm agent still exists
        assert registry.get_agent("active-agent") is not None

        # Clean up this launch record for testing next status
        with db_mgr.get_session() as session:
            rec = session.get(ExperimentLaunchRecord, f"launch-{active_status.lower()}")
            session.delete(rec)
            session.commit()

