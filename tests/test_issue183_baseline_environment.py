"""同环境资格、历史诊断与真实 revision 的 CAS 修复。"""
from __future__ import annotations

from copy import deepcopy

import pytest
from app import api_baselines
from app.baselines import BaselineConflictError, get_baseline, set_baseline
from app.db_models import BaselineBindingRecord, ExperimentLaunchRecord
from app.manifest import LaunchService
from app.registry import AgentRegistry
from app.result_snapshots import create_result_snapshot
from test_run_result_s3 import _create_completed_launch


def snapshot(db, environment="production"):
    launch_id = _create_completed_launch(db)
    with db.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        manifest = deepcopy(launch.manifest)
        if environment is None:
            manifest["comparison"].pop("environment")
        else:
            manifest["comparison"]["environment"] = environment
        launch.manifest = manifest
        launch.environment = environment if environment in {"production", "test"} else "production"
        result = create_result_snapshot(session, launch)
        session.flush()
        return result


@pytest.mark.parametrize("source", ["test", None, " Test "])
def test_binding_rejects_mismatch_or_unknown_frozen_environment(setup_runtime, source):
    db = setup_runtime[0]
    result = snapshot(db, source)
    with pytest.raises(ValueError, match="environment"):
        set_baseline(db, agent_id="test-agent", environment="production", result_snapshot_id=result.id, expected_revision=0)
    assert get_baseline(db, "test-agent", "production") is None


def test_invalid_legacy_binding_retains_revision_and_can_be_repaired(setup_runtime):
    db = setup_runtime[0]
    old = snapshot(db, "production")
    new = snapshot(db, "test")
    with db.get_session() as session:
        session.add(BaselineBindingRecord(agent_id="test-agent", environment="test", result_snapshot_id=old.id, revision=3))
    binding, result = get_baseline(db, "test-agent", "test")
    response = api_baselines._response(db, binding, result)
    assert response.revision == 3
    assert response.environment_validation_status == "INVALID"
    assert response.environment_reason_codes == ["BASELINE_ENVIRONMENT_MISMATCH"]
    with pytest.raises(BaselineConflictError):
        set_baseline(db, agent_id="test-agent", environment="test", result_snapshot_id=new.id, expected_revision=0)
    repaired = set_baseline(db, agent_id="test-agent", environment="test", result_snapshot_id=new.id, expected_revision=3)
    assert repaired.revision == 4
    assert api_baselines._response(db, repaired, new).environment_validation_status == "VALID"


@pytest.mark.parametrize("explicit", [True, False])
def test_candidate_cannot_bypass_environment_checks(setup_runtime, monkeypatch, explicit):
    db = setup_runtime[0]
    old = snapshot(db, "production")
    with db.get_session() as session:
        session.add(BaselineBindingRecord(agent_id="test-agent", environment="test", result_snapshot_id=old.id, revision=3))
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    service = LaunchService(db, AgentRegistry(db), runner_version="0.2.0")
    with pytest.raises(ValueError, match="environment"):
        service.create_launch(agent_id="test-agent", agent_version="v1", dataset_name="banking-agent-regression",
                              environment="test", baseline_snapshot_id=old.id if explicit else None)


@pytest.mark.parametrize(("source", "code"), [
    ("test", "BASELINE_ENVIRONMENT_MISMATCH"),
    (None, "SNAPSHOT_ENVIRONMENT_MISSING"),
    (" Test ", "SNAPSHOT_ENVIRONMENT_INVALID"),
])
def test_http_invalid_binding_diagnostics_and_structured_rejection(setup_runtime, monkeypatch, source, code):
    from app import main
    from fastapi.testclient import TestClient

    db = setup_runtime[0]
    old = snapshot(db, source)
    monkeypatch.setattr(main, "db_manager", db)
    client = TestClient(main.app)
    path = "/api/v1/agents/test-agent/baselines"
    rejected = client.post(path, json={"environment": "production", "result_snapshot_id": old.id, "expected_revision": 0})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == code
    with db.get_session() as session:
        session.add(BaselineBindingRecord(agent_id="test-agent", environment="production", result_snapshot_id=old.id, revision=9))
    response = client.get(path, params={"environment": "production"})
    assert response.status_code == 200
    assert response.json()["revision"] == 9
    assert response.json()["environment_validation_status"] == "INVALID"
    assert response.json()["environment_reason_codes"] == [code]
