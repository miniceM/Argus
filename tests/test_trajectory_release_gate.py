"""S45-06/08：冻结门禁观测与轨迹 critical 策略约束。"""

import pytest
from app.db_models import RunResultSnapshotRecord
from app.release_gates import ReleasePolicy, evaluate_gate
from test_trajectory_evidence import postgres_engine as postgres_engine
from test_trajectory_required_evidence import (
    freeze,
    gate_policy,
    launch_for,
    measured,
    persist,
)
from test_trajectory_required_evidence import (
    required_trajectory_snapshot as required_trajectory_snapshot,
)
from test_trajectory_required_evidence import (
    runtime as runtime,
)


@pytest.mark.parametrize("value,pass_rate,coverage,unknown", [(True, 1.0, 1.0, 0), (False, 0.0, 1.0, 0), (None, None, 0.0, 1)])
def test_gate_exposes_frozen_case_ratios_even_with_one_policy_metric(runtime, value, pass_rate, coverage, unknown):
    launch = launch_for(runtime)
    rows, summary = measured(launch, required_value=value)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    with runtime[0].get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        policy = gate_policy().model_copy(update={"rules": [gate_policy().rules[0]]})
        verdict = evaluate_gate(policy, stored)
    assert verdict.decided_pass_rate == pass_rate
    assert verdict.decision_coverage == coverage
    assert verdict.unknown_count == unknown
    assert verdict.critical_failure_count == 0
    assert verdict.required_case_counts.total == 1
    assert verdict.required_case_counts.unknown == unknown
    assert verdict.evidence_contract_version == "trajectory-1.0"


def test_trajectory_gate_cannot_disable_critical_blocking(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    policy = ReleasePolicy(name="trajectory-policy", version="1.0", agent_id=snapshot.agent_id,
        block_critical_failures=False, rules=[{"id": "pass-rate", "metric": "pass_rate", "operator": ">=", "threshold": 0.0}])
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        verdict = evaluate_gate(policy, stored)
    assert verdict.decision == "UNKNOWN" and not verdict.releasable
    assert "TRAJECTORY_CRITICAL_BLOCK_REQUIRED" in verdict.reason_codes


@pytest.mark.parametrize("block", [True, False])
def test_trajectory_gate_api_freezes_new_engine_and_rejects_disabled_critical(runtime, monkeypatch, block):
    from app import main
    from fastapi.testclient import TestClient

    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    monkeypatch.setattr(main, "db_manager", runtime[0])
    client = TestClient(main.app)
    policy = gate_policy().model_copy(update={"block_critical_failures": block})
    assert client.post("/api/v1/release-policies", json=policy.model_dump(mode="json")).status_code == 201
    request = {"policy_name": policy.name, "policy_version": policy.version,
        "candidate_launch_id": snapshot.launch_id, "candidate_snapshot_id": snapshot.id}
    response = client.post("/api/v1/release-gates/evaluate", json=request)
    if not block:
        assert response.status_code == 409
        assert response.json()["detail"] == "TRAJECTORY_CRITICAL_BLOCK_REQUIRED"
        return
    assert response.status_code == 201
    result = response.json()
    assert result["engine_version"] == "trajectory-release-gate-v1"
    from app.db_models import ReleaseGateRecord
    from app.evaluator_binding import canonical_digest
    with runtime[0].get_session() as session:
        record = session.get(ReleaseGateRecord, result["id"])
        assert record.request_digest == canonical_digest({
            "candidate_snapshot_id": snapshot.id, "policy_digest": policy.content_digest,
            "engine_version": result["engine_version"],
        })
    assert result["decided_pass_rate"] == result["decision_coverage"] == 1.0
    assert result["required_case_counts"] == {"total": 1, "passed": 1, "failed": 0, "unknown": 0}
    assert client.post("/api/v1/release-gates/evaluate", json=request).json() == result
    assert client.get(f"/api/v1/release-gates/{result['id']}").json() == result


def test_new_gate_response_preserves_historical_results_without_backfill():
    from app.release_gates import GateEvaluation

    historical = {"decision": "PASS", "releasable": True, "engine_version": "release-gate-v3",
        "candidate_snapshot_id": "historical", "reason_codes": [], "rules": []}
    response = GateEvaluation.model_validate(historical)
    assert response.engine_version == "release-gate-v3" and response.decision == "PASS"
    assert response.required_case_counts is None and response.decided_pass_rate is None
    assert response.decision_coverage is None and response.evidence_contract_version is None
    assert set(historical) == {"decision", "releasable", "engine_version", "candidate_snapshot_id", "reason_codes", "rules"}


def test_optional_missing_trajectory_does_not_reduce_required_decision_coverage(runtime):
    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    with runtime[0].get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        verdict = evaluate_gate(gate_policy(), stored)
    assert verdict.decision == "PASS"
    assert verdict.trajectory_coverage == 0.0
    assert verdict.decision_coverage == 1.0


def test_gate_trajectory_coverage_verifies_actual_frozen_artifact(required_trajectory_snapshot):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        verdict = evaluate_gate(ReleasePolicy(name="trajectory-coverage", version="1.0", agent_id=snapshot.agent_id,
            rules=[{"id": "pass-rate", "metric": "pass_rate", "operator": ">=", "threshold": 1.0}]), stored)
    # 制品 fixture 缺少正式 Dataset/Agent 身份；轨迹可校验不替代发布资格。
    assert verdict.decision == "UNKNOWN" and not verdict.releasable
    assert set(verdict.reason_codes) == {"DATASET_IDENTITY_UNKNOWN", "AGENT_IDENTITY_UNKNOWN"}
    assert verdict.trajectory_coverage == 1.0


def test_detached_optional_snapshot_cannot_claim_verified_trajectory_coverage(runtime):
    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    verdict = evaluate_gate(gate_policy(), snapshot)
    assert verdict.trajectory_coverage is None


def test_summary_api_reports_required_counts_and_optional_trajectory_coverage(runtime, monkeypatch):
    from app import main
    from fastapi.testclient import TestClient

    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    monkeypatch.setattr(main, "db_manager", runtime[0])
    response = TestClient(main.app).get(f"/api/v1/experiment-launches/{launch.id}/summary",
        params={"snapshot_id": snapshot.id})
    assert response.status_code == 200
    data = response.json()
    assert data["snapshot_id"] == snapshot.id
    assert data["required_case_counts"] == {"total": 1, "passed": 1, "failed": 0, "unknown": 0}
    assert data["trajectory_coverage"] == 0.0
    assert data["summary"]["decided_pass_rate"] == data["summary"]["decision_coverage"] == 1.0


def test_six_case_two_required_rule_denominators_are_cases_not_measurements(runtime):
    from dataclasses import replace

    from app.db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
    from app.evaluation_result_store import persist_typed_results
    from app.evaluator_binding import evaluate_frozen_item, resolve_execution_plan, summarize_typed_results
    from sqlalchemy import select
    from test_trajectory_rules import B

    launch = runtime[2].create_launch(agent_id="banking-agent", agent_version="native", dataset_name="golden",
        trajectory_contract=runtime[3], dataset_snapshot={"source": "seed", "dataset_id": "golden", "dataset_version": "v1",
            "items": [{"id": f"case-{n}", "input": {}, "expected_output": {"intent": "ok"}} for n in range(6)]},
        evaluator_selections=[{"id": "answer_present", "version": "1.0.0"},
            {"id": "intent_match", "version": "1.0.0"},
            {"id": "trajectory.max_tool_calls", "version": "1.0.0", "params": {
                "rule": "max_tool_calls", "tool_identity": B, "max_count": 1}}],
        quality_policy_rules=[
            {"evaluator_id": "answer_present", "result_type": "boolean", "operator": "==", "expected_value": True, "required": True},
            {"evaluator_id": "intent_match", "result_type": "numeric", "operator": ">=", "threshold": 1.0, "required": True},
            {"evaluator_id": "trajectory.max_tool_calls", "result_type": "boolean", "operator": "==", "expected_value": True, "required": False},
        ])
    with runtime[0].get_session() as session:
        items = session.scalars(select(ExperimentItemExecutionRecord).where(
            ExperimentItemExecutionRecord.launch_id == launch.id).order_by(ExperimentItemExecutionRecord.dataset_item_id)).all()
        assert len(items) == 6
        for n, item in enumerate(items):
            output = {"answer": "ok", "intent": "ok" if n < 4 else "wrong"}
            result = evaluate_frozen_item(launch.manifest, output=output, expected_output={"intent": "ok"})
            rows = list(result.typed_results)
            if n == 5:
                rows = [replace(row, status="skipped", value=None, normalized_value=None)
                    if row.evaluator_id == "intent_match" else row for row in rows]
                result = summarize_typed_results(resolve_execution_plan(launch.manifest), rows, manifest=launch.manifest)
            item.execution_status, item.eval_status = "succeeded", result.eval_status
            item.quality_conclusion, item.quality_evaluation = result.quality_conclusion, result.quality_decision.payload()
            item.scores, item.dispatch_generation = result.scores, 1
            persist_typed_results(session, item_execution_id=item.id, launch_id=launch.id, results=rows)
        session.get(ExperimentLaunchRecord, launch.id).status = "COMPLETED"
    snapshot = freeze(runtime, launch)
    with runtime[0].get_session() as session:
        verdict = evaluate_gate(gate_policy(), session.get(RunResultSnapshotRecord, snapshot.id))
    assert verdict.required_case_counts.model_dump() == {"total": 6, "passed": 4, "failed": 1, "unknown": 1}
    assert verdict.decided_pass_rate == 4 / 5
    assert verdict.decision_coverage == 5 / 6
    assert verdict.unknown_count == 1 and verdict.trajectory_coverage == 0
    assert verdict.decision == "UNKNOWN" and not verdict.releasable


@pytest.mark.parametrize("trajectory_enabled", [True, False])
def test_optional_critical_configuration_is_rejected_only_for_new_trajectory_launch(runtime, trajectory_enabled):
    from app.quality_policy import QualityPolicyError

    options = dict(agent_id="banking-agent", agent_version="native", dataset_name="golden",
        trajectory_contract=runtime[3] if trajectory_enabled else None, capture_only=trajectory_enabled,
        dataset_snapshot={"source": "seed", "dataset_id": "golden", "dataset_version": "v1",
            "items": [{"id": "case", "input": {}, "expected_output": {}}]},
        evaluator_selections=[{"id": "answer_present", "version": "1.0.0"}, {"id": "intent_match", "version": "1.0.0"}],
        quality_policy_rules=[
            {"evaluator_id": "answer_present", "result_type": "boolean", "operator": "==", "expected_value": True, "required": True},
            {"evaluator_id": "intent_match", "result_type": "numeric", "operator": ">=", "threshold": 1.0,
                "required": False, "critical": True},
        ])
    if trajectory_enabled:
        with pytest.raises(QualityPolicyError) as caught:
            runtime[2].create_launch(**options)
        assert caught.value.code == "TRAJECTORY_OPTIONAL_CRITICAL_INVALID"
    else:
        assert runtime[2].create_launch(**options).manifest["schema_version"] == "1.2"


@pytest.mark.parametrize("damage", ["manifest_null", "manifest_list", "comparison_null"])
def test_gate_api_rejects_malformed_frozen_manifest_without_server_error(runtime, monkeypatch, damage):
    from copy import deepcopy

    from app import main
    from fastapi.testclient import TestClient

    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    monkeypatch.setattr(main, "db_manager", runtime[0])
    client = TestClient(main.app, raise_server_exceptions=False)
    policy = gate_policy()
    assert client.post("/api/v1/release-policies", json=policy.model_dump(mode="json")).status_code == 201
    with runtime[0].get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        if damage == "manifest_null":
            stored.manifest = None
        elif damage == "manifest_list":
            stored.manifest = []
        else:
            changed = deepcopy(stored.manifest)
            changed["comparison"] = None
            stored.manifest = changed
    response = client.post("/api/v1/release-gates/evaluate", json={"policy_name": policy.name,
        "policy_version": policy.version, "candidate_launch_id": launch.id, "candidate_snapshot_id": snapshot.id})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"


def test_historical_optional_critical_launch_idempotent_replay_survives_new_freeze_guard(runtime, monkeypatch):
    from app import manifest as manifest_module
    from app.quality_policy import QualityPolicyError

    options = dict(agent_id="banking-agent", agent_version="native", dataset_name="golden", capture_only=True,
        trajectory_contract=runtime[3], idempotency_key="historical-critical",
        dataset_snapshot={"source": "seed", "dataset_id": "golden", "dataset_version": "v1",
            "items": [{"id": "case", "input": {}, "expected_output": {}}]},
        evaluator_selections=[{"id": "answer_present", "version": "1.0.0"}, {"id": "intent_match", "version": "1.0.0"}],
        quality_policy_rules=[
            {"evaluator_id": "answer_present", "result_type": "boolean", "operator": "==", "expected_value": True, "required": True},
            {"evaluator_id": "intent_match", "result_type": "numeric", "operator": ">=", "threshold": 1.0,
                "required": False, "critical": True},
        ])
    # 模拟部署新冻结拒绝规则之前已持久化的同一请求；不改变摘要或测量语义。
    with monkeypatch.context() as historical:
        historical.setattr(manifest_module, "_validate_trajectory_critical_policy", lambda policy: None)
        original = runtime[2].create_launch(**options)
    assert runtime[2].create_launch(**options).id == original.id
    with pytest.raises(QualityPolicyError) as caught:
        runtime[2].create_launch(**{**options, "idempotency_key": "new-critical"})
    assert caught.value.code == "TRAJECTORY_OPTIONAL_CRITICAL_INVALID"


def test_legacy_gate_api_rejects_null_comparison_without_server_error(setup_runtime, monkeypatch):
    from app import main
    from fastapi.testclient import TestClient
    from test_issue_7_release_gates import policy as legacy_policy
    from test_issue_7_release_gates import snapshot as legacy_snapshot
    from test_issue_7_release_gates import store_snapshot

    candidate = legacy_snapshot()
    candidate.manifest["comparison"] = None
    store_snapshot(setup_runtime[0], candidate)
    monkeypatch.setattr(main, "db_manager", setup_runtime[0])
    client = TestClient(main.app, raise_server_exceptions=False)
    policy = legacy_policy()
    assert client.post("/api/v1/release-policies", json=policy.model_dump(mode="json")).status_code == 201
    response = client.post("/api/v1/release-gates/evaluate", json={"policy_name": policy.name,
        "policy_version": policy.version, "candidate_launch_id": candidate.launch_id, "candidate_snapshot_id": candidate.id})
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"
