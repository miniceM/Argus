from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from app import main
from app.comparison_contracts import aggregation_comparison_digest
from app.db_models import ExperimentLaunchRecord, RunResultSnapshotRecord
from app.registry import AgentRegistry
from app.release_gates import ReleasePolicy, evaluate_gate
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError


def snapshot(*, passed=2, count=2, baseline_id=None):
    items = [{
        "dataset_item_id": str(index), "case_digest": f"case-{index}",
        "execution_status": "succeeded", "eval_status": "succeeded",
        "quality_conclusion": "pass" if index < passed else "fail",
        "scores": {"intent_match": 1.0 if index < passed else 0.0},
        "latency_ms": 10,
        "quality_evaluation": {"rules": [{"critical": False, "conclusion": "pass"}]},
    } for index in range(count)]
    manifest = {
        "schema_version": "1.2", "agent": {"agent_id": "test-agent", "version": "v1"},
        "dataset": {"source": "seed", "dataset_id": "ds", "items": [{"id": str(i)} for i in range(count)]},
        "comparison": {"environment": "production", "baseline_snapshot_id": baseline_id},
        "evaluators": [{"id": "intent_match", "version": "1.0.0", "scope": "item", "threshold": 1.0}],
        "contract_digests": {
            "measurement": {"digest": "measurement-v1"}, "quality_policy": {"digest": "policy-v1"},
            "aggregation_comparison": {"digest": aggregation_comparison_digest()},
        },
    }
    return SimpleNamespace(
        id=str(uuid.uuid4()), launch_id=str(uuid.uuid4()), agent_id="test-agent", revision=1,
        manifest=manifest, items=items, evidence_state="COMPLETE", manifest_digest="manifest-digest",
        source_result_digest="source-digest",
    )


def policy(*rules, **kwargs):
    return ReleasePolicy(
        name="production-default", version="1.0.0", agent_id="test-agent", environment="production",
        rules=list(rules) or [{"id": "pass-rate", "metric": "pass_rate", "operator": ">=", "threshold": 0.95}],
        **kwargs,
    )


def test_absolute_gate_explains_rules_and_keeps_quality_failure_separate():
    assert evaluate_gate(policy(), snapshot()).decision == "PASS"
    result = evaluate_gate(policy(), snapshot(passed=1))
    assert result.decision == "FAIL"
    assert not result.releasable
    assert result.rules[0].actual == 0.5
    assert result.rules[0].reason == "THRESHOLD_VIOLATED"


@pytest.mark.parametrize("problem", ["empty", "unknown", "execution", "evaluation", "diagnostic", "missing_case", "legacy"])
def test_incomplete_evidence_never_releases(problem):
    candidate = snapshot()
    if problem == "empty":
        candidate.items = []
    elif problem == "unknown":
        candidate.items[0]["quality_conclusion"] = "unknown"
    elif problem == "execution":
        candidate.items[0]["execution_status"] = "failed"
    elif problem == "evaluation":
        candidate.items[0]["eval_status"] = "failed"
    elif problem == "diagnostic":
        candidate.evidence_state = "DIAGNOSTIC"
    elif problem == "missing_case":
        candidate.items.pop()
    else:
        candidate.manifest.pop("contract_digests")
    result = evaluate_gate(policy(), candidate)
    assert result.decision == "UNKNOWN"
    assert result.releasable is False
    assert result.reason_codes


def test_relative_rules_use_every_case_and_frozen_baseline():
    baseline = snapshot()
    candidate = snapshot(passed=1, baseline_id=baseline.id)
    result = evaluate_gate(policy(
        {"id": "regressions", "metric": "regression_count", "operator": "<=", "threshold": 0},
        {"id": "delta", "metric": "pass_rate_delta", "operator": ">=", "threshold": -0.01},
    ), candidate, baseline)
    assert result.decision == "FAIL"
    assert [rule.actual for rule in result.rules] == [1, -0.5]
    assert result.baseline_snapshot_id == baseline.id


def test_relative_gate_rejects_baseline_from_another_environment():
    baseline = snapshot()
    baseline.manifest["comparison"]["environment"] = "staging"
    candidate = snapshot(baseline_id=baseline.id)
    result = evaluate_gate(policy(
        {"id": "regressions", "metric": "regression_count", "operator": "<=", "threshold": 0},
    ), candidate, baseline)
    assert result.decision == "UNKNOWN"
    assert "BASELINE_ENVIRONMENT_MISMATCH" in result.reason_codes


def test_agent_purge_removes_unused_policies_instead_of_orphaning_them(setup_runtime, monkeypatch):
    monkeypatch.setattr(main, "db_manager", setup_runtime[0])
    monkeypatch.setattr(main, "registry", AgentRegistry(setup_runtime[0]))
    client = TestClient(main.app)
    draft = policy().model_dump()
    assert client.post("/api/v1/release-policies", json=draft).status_code == 201
    assert client.post("/api/v1/agents/purge", json={"agent_id": "test-agent", "confirm_name": "Test Agent"}).status_code == 200
    assert client.get("/api/v1/release-policies", params={"name": draft["name"], "version": draft["version"]}).status_code == 404


def test_purge_flush_conflict_returns_409_and_rolls_back(setup_runtime, monkeypatch):
    from sqlalchemy.orm import Session

    manager = setup_runtime[0]
    candidate = snapshot()
    store_snapshot(manager, candidate)
    original = Session.flush

    def race(session, *args, **kwargs):
        if any(isinstance(record, ExperimentLaunchRecord) for record in session.deleted):
            raise IntegrityError("gate inserted during purge", {}, Exception("restricted snapshot"))
        return original(session, *args, **kwargs)

    monkeypatch.setattr(main, "db_manager", manager)
    monkeypatch.setattr(main, "registry", AgentRegistry(manager))
    monkeypatch.setattr(Session, "flush", race)
    response = TestClient(main.app, raise_server_exceptions=False).post(
        "/api/v1/agents/purge", json={"agent_id": "test-agent", "confirm_name": "Test Agent"},
    )
    assert response.status_code == 409
    with manager.get_session() as session:
        assert session.get(ExperimentLaunchRecord, candidate.launch_id) is not None


@pytest.mark.parametrize("problem", ["missing", "wrong_id", "contract", "dataset", "case", "diagnostic"])
def test_noncomparable_baseline_is_unknown_not_zero_regressions(problem):
    baseline = snapshot()
    candidate = snapshot(baseline_id=baseline.id)
    if problem == "missing":
        baseline = None
    elif problem == "wrong_id":
        baseline.id = "other-snapshot"
    elif problem == "contract":
        baseline.manifest["contract_digests"]["quality_policy"]["digest"] = "changed"
    elif problem == "dataset":
        baseline.manifest["dataset"]["dataset_id"] = "other-dataset"
    elif problem == "case":
        baseline.items[0]["case_digest"] = "changed-case"
    else:
        baseline.evidence_state = "DIAGNOSTIC"
    result = evaluate_gate(policy(
        {"id": "regressions", "metric": "regression_count", "operator": "<=", "threshold": 5},
    ), candidate, baseline)
    assert result.decision == "UNKNOWN"
    assert not result.releasable
    assert result.rules[0].actual is None


def test_critical_violation_vetoes_otherwise_passing_policy():
    candidate = snapshot()
    candidate.items[0]["quality_evaluation"]["rules"][0] = {"critical": True, "conclusion": "fail"}
    result = evaluate_gate(policy(), candidate)
    assert result.decision == "FAIL"
    assert "CRITICAL_FAILURE" in result.reason_codes


def test_missing_critical_evidence_blocks_release_even_when_optional_item_rule_passes():
    candidate = snapshot()
    candidate.items[0]["quality_evaluation"]["rules"][0] = {"critical": True, "conclusion": "unknown"}
    result = evaluate_gate(policy(), candidate)
    assert result.decision == "UNKNOWN"
    assert "CRITICAL_EVIDENCE_INCOMPLETE" in result.reason_codes


def test_undefined_and_nonfinite_metrics_are_unknown_not_zero():
    candidate = snapshot()
    for item in candidate.items:
        item["latency_ms"] = None
    result = evaluate_gate(policy(
        {"id": "latency", "metric": "p95_latency_ms", "operator": "<=", "threshold": 100},
    ), candidate)
    assert result.decision == "UNKNOWN"
    assert result.rules[0].actual is None


def test_latency_gate_requires_measurements_for_every_required_case():
    candidate = snapshot()
    candidate.items[0]["latency_ms"] = None
    result = evaluate_gate(policy(
        {"id": "latency", "metric": "p95_latency_ms", "operator": "<=", "threshold": 100},
    ), candidate)
    assert result.decision == "UNKNOWN"
    assert result.rules[0].actual is None


def test_declared_critical_rule_cannot_disappear_from_item_evidence():
    candidate = snapshot()
    candidate.manifest["quality_policy"] = {"rules": [{"evaluator_id": "safety", "critical": True}]}
    result = evaluate_gate(policy(), candidate)
    assert result.decision == "UNKNOWN"
    assert "CRITICAL_EVIDENCE_INCOMPLETE" in result.reason_codes


@pytest.mark.parametrize("changes", [{"version": "latest"}, {"rules": []}, {"rules": [
    {"id": "bad", "metric": "pass_rate", "operator": ">=", "threshold": float("nan")}
]}, {"rules": [
    {"id": "bad", "metric": "arbitrary", "operator": ">=", "threshold": 1}
]}])
def test_policy_rejects_ambiguous_or_invalid_definitions(changes):
    draft = policy().model_dump()
    draft.update(changes)
    with pytest.raises(ValidationError):
        ReleasePolicy.model_validate(draft)


def store_snapshot(db_manager, value):
    with db_manager.get_session() as session:
        session.add(ExperimentLaunchRecord(
            id=value.launch_id, name="release-test", agent_id="test-agent", agent_version="v1", agent_version_id="test-agent-v1",
            dataset_id="ds", dataset_name="ds", dataset_version="v1", manifest=value.manifest,
            status="COMPLETED",
        ))
        session.flush()
        session.add(RunResultSnapshotRecord(
            id=value.id, launch_id=value.launch_id, agent_id=value.agent_id, revision=value.revision,
            source_result_digest=value.source_result_digest, manifest_digest=value.manifest_digest,
            manifest=value.manifest, summary={}, items=value.items, evidence_state=value.evidence_state,
        ))
        session.commit()


def test_http_policy_is_immutable_and_gate_result_is_durable_and_idempotent(setup_runtime, monkeypatch):
    db_manager = setup_runtime[0]
    monkeypatch.setattr(main, "db_manager", db_manager)
    monkeypatch.setattr(main, "registry", AgentRegistry(db_manager))
    client = TestClient(main.app)
    draft = policy().model_dump()
    created = client.post("/api/v1/release-policies", json=draft)
    assert created.status_code == 201, created.text
    assert client.post("/api/v1/release-policies", json=draft).json() == created.json()
    changed = copy.deepcopy(draft)
    changed["rules"][0]["threshold"] = 0.5
    assert client.post("/api/v1/release-policies", json=changed).status_code == 409
    assert client.get("/api/v1/release-policies", params={"name": draft["name"], "version": draft["version"]}).json() == created.json()
    candidate = snapshot()
    store_snapshot(db_manager, candidate)
    request = {
        "policy_name": draft["name"], "policy_version": draft["version"],
        "candidate_launch_id": candidate.launch_id, "candidate_snapshot_id": candidate.id,
    }
    response = client.post("/api/v1/release-gates/evaluate", json=request)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["decision"] == "PASS"
    assert result["candidate_snapshot_id"] == candidate.id
    assert result["policy_digest"] == created.json()["policy_digest"]
    assert client.post("/api/v1/release-gates/evaluate", json=request).json() == result
    # 活动 Launch 的变化、重新读报告或重发请求不能改写已保存的门禁结论。
    with db_manager.get_session() as session:
        session.get(ExperimentLaunchRecord, candidate.launch_id).quality_conclusion = "fail"
        session.commit()
    assert client.get(f"/api/v1/release-gates/{result['id']}").json() == result
    assert candidate.id in result["report_url"]
    purge = client.post("/api/v1/agents/purge", json={"agent_id": "test-agent", "confirm_name": "Test Agent"})
    assert purge.status_code == 409
    assert client.get(f"/api/v1/release-gates/{result['id']}").json() == result
    with db_manager.get_session() as session:
        session.delete(session.get(RunResultSnapshotRecord, candidate.id))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_api_rejects_snapshot_from_different_launch_and_wrong_policy_scope(setup_runtime, monkeypatch):
    db_manager = setup_runtime[0]
    monkeypatch.setattr(main, "db_manager", db_manager)
    client = TestClient(main.app)
    draft = policy().model_dump()
    assert client.post("/api/v1/release-policies", json=draft).status_code == 201
    candidate = snapshot()
    store_snapshot(db_manager, candidate)
    request = {"policy_name": draft["name"], "policy_version": draft["version"],
               "candidate_launch_id": "other-launch", "candidate_snapshot_id": candidate.id}
    assert client.post("/api/v1/release-gates/evaluate", json=request).status_code == 404
    request["candidate_launch_id"] = candidate.launch_id
    draft["version"] = "2.0.0"
    draft["environment"] = "staging"
    assert client.post("/api/v1/release-policies", json=draft).status_code == 201
    request["policy_version"] = "2.0.0"
    assert client.post("/api/v1/release-gates/evaluate", json=request).status_code == 409
