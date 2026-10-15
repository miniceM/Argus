"""环境不同/未知的快照只能诊断，不能形成正式比较或放行。"""
from copy import deepcopy

import pytest
from app import api_results
from app.comparison_contracts import assess_comparability
from app.release_gates import ENGINE_VERSION, evaluate_gate
from test_issue_7_release_gates import policy, snapshot
from test_issue_86_comparability import _manifest


@pytest.mark.parametrize("value", ["test", None, " Production "])
def test_environment_is_a_required_comparison_contract(value):
    baseline = _manifest()
    candidate = deepcopy(baseline)
    if value is None:
        candidate["comparison"].pop("environment")
    else:
        candidate["comparison"]["environment"] = value
    verdict = assess_comparability(baseline, candidate)
    assert not verdict.comparable
    assert any("ENVIRONMENT" in reason for reason in verdict.reason_codes)


def test_absolute_gate_cannot_hide_an_invalid_frozen_baseline():
    base = snapshot()
    candidate = snapshot(baseline_id=base.id)
    base.manifest["comparison"]["environment"] = "test"
    result = evaluate_gate(policy(), candidate, base)
    assert not result.releasable
    assert "BASELINE_ENVIRONMENT_MISMATCH" in result.reason_codes
    assert ENGINE_VERSION != "release-gate-v3"


def test_comparison_overview_and_case_reject_legacy_cross_environment(setup_runtime, monkeypatch):
    from app.db_models import RunResultSnapshotRecord
    from test_api_results_s3 import _launch
    db = setup_runtime[0]
    monkeypatch.setattr(api_results, "_db_manager", lambda: db)
    _, base_id = _launch(db, "pass", 1.0)
    launch_id, candidate_id = _launch(db, "pass", 1.0, base_id)
    with db.get_session() as session:
        base = session.get(RunResultSnapshotRecord, base_id)
        manifest = deepcopy(base.manifest)
        manifest["comparison"]["environment"] = "test"
        base.manifest = manifest  # 仅制造历史不兼容测试数据，产品不改写冻结证据。
    overview = api_results.get_launch_comparison(launch_id, candidate_id, classification=None, limit=50, cursor=0)
    case = api_results.get_comparison_case(launch_id, candidate_id, "case-1")
    assert overview.items[0]["classification"] == "NOT_COMPARABLE"
    assert case.classification == "NOT_COMPARABLE"
    assert not overview.formal.available


def test_new_gate_engine_does_not_reuse_old_cached_pass(setup_runtime, monkeypatch):
    from app import api_release_gates, main
    from app.db_models import RunResultSnapshotRecord
    from fastapi.testclient import TestClient
    from test_issue_7_release_gates import store_snapshot
    db = setup_runtime[0]
    monkeypatch.setattr(main, "db_manager", db)
    client = TestClient(main.app)
    draft = policy().model_dump()
    assert client.post("/api/v1/release-policies", json=draft).status_code == 201
    base = snapshot()
    candidate = snapshot(baseline_id=base.id)
    store_snapshot(db, base)
    store_snapshot(db, candidate)
    payload = {"policy_name": draft["name"], "policy_version": draft["version"],
               "candidate_launch_id": candidate.launch_id, "candidate_snapshot_id": candidate.id}
    with monkeypatch.context() as context:
        context.setattr(api_release_gates, "ENGINE_VERSION", "release-gate-v3")
        old = client.post("/api/v1/release-gates/evaluate", json=payload).json()
        assert old["releasable"]
    with db.get_session() as session:
        row = session.get(RunResultSnapshotRecord, base.id)
        changed = deepcopy(row.manifest)
        changed["comparison"]["environment"] = "test"
        row.manifest = changed
    new = client.post("/api/v1/release-gates/evaluate", json=payload).json()
    assert new["id"] != old["id"]
    assert not new["releasable"]
    assert client.post("/api/v1/release-gates/evaluate", json=payload).json()["id"] == new["id"]
    assert client.get(f"/api/v1/release-gates/{old['id']}").json()["releasable"]
