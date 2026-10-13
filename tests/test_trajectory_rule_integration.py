"""真实来源的正式规则消费者；不重做已冻结的 Reader 资格校准。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from app.db_models import (
    EvaluationResultRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from app.evaluation_recovery import recover_evaluation
from app.result_snapshots import create_result_snapshot
from app.trajectory.contract import TrajectoryContract
from app.trajectory.factory import QualifiedReaderFactory, ReaderApproval, reader_artifact_digest
from app.trajectory.inputs import load_snapshot_input
from app.trajectory.worker_acquisition import WorkerTrajectoryCollector
from sqlalchemy import select
from test_trajectory_observation_integration import (
    AgentProcess,
    calibrated_banking_payload,
    invoke_worker,
)
from test_trajectory_observation_integration import local_langfuse as local_langfuse
from test_trajectory_observation_integration import s40_runtime as s40_runtime

pytestmark = pytest.mark.trajectory_local

PARAMS = [
    {"rule": "require_tool", "tool_identity": "banking.customer.lookup"},
    {"rule": "forbid_tool", "tool_identity": "banking.balance.read"},
    {"rule": "max_tool_calls", "tool_identity": "banking.balance.read", "max_count": 0},
    {"rule": "tool_order", "before_tool_identity": "banking.customer.lookup", "after_tool_identity": "banking.balance.read"},
]


@pytest.mark.parametrize("profile,mode,approved", [("native_otel", "immediate", True),
    ("pydantic_ai", "immediate", True), ("http_only", "immediate", True),
    ("native_otel", "late_all", True), ("native_otel", "immediate", False)])
@pytest.mark.parametrize("recovery_stage", [None, "before_acquisition", "ready"])
def test_real_frozen_rule_consumers(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile, mode, approved, recovery_stage):
    report = json.loads((Path(__file__).resolve().parents[1] / "docs/validation/s4-1/reader-calibration.json").read_text())
    approval = report["groups"][profile + ":serial"]["approved_identity"]
    contract = TrajectoryContract.model_validate(calibrated_banking_payload())
    assert contract.digest == approval["trajectory_contract_digest"] and reader_artifact_digest() == approval["reader_artifact_digest"]
    factory = QualifiedReaderFactory(server_url=local_langfuse.base_url, public_key=local_langfuse.env["S4_PUBLIC_KEY"],
        secret_key=local_langfuse.env["S4_SECRET_KEY"], approvals=(ReaderApproval(**approval),) if approved else ())
    worker = s40_runtime[4]
    monkeypatch.setattr(worker, "trajectory_collector", WorkerTrajectoryCollector(s40_runtime[0].get_session, factory_provider=lambda: factory))
    selections = [{"id": "trajectory." + p["rule"], "params": p} for p in PARAMS]
    observed = approved and profile != "http_only"
    quality, eval_status = ("fail", "succeeded") if observed else ("unknown", "failed")
    profile_id = {"native_otel": "native-otel-v1", "pydantic_ai": "pydantic-ai-v1", "http_only": "http-only-v1"}[profile]
    with AgentProcess(local_langfuse, tmp_path, profile, export_mode=mode) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, trajectory_source_profile_id=profile_id,
            trajectory_contract=contract, recovery_stage=recovery_stage, rule_selections=selections,
            expected_quality=quality, expected_eval_status=eval_status)
    with s40_runtime[0].get_session() as session:
        launch = session.get(ExperimentLaunchRecord, identity["launch_id"])
        launch.status = "COMPLETED"
        original = create_result_snapshot(session, launch)
        assert original.items[0]["required_evidence_status"] == ("COMPLETE" if observed else "INCOMPLETE")
        assert original.evidence_state == ("COMPLETE" if observed else "DIAGNOSTIC")
        from app.baselines import validate_baseline_snapshot
        from app.release_gates import evaluate_gate
        from test_issue_7_release_gates import policy
        if observed:
            validate_baseline_snapshot(original, launch, "test-agent")
        else:
            with pytest.raises(ValueError):
                validate_baseline_snapshot(original, launch, "test-agent")
        assert evaluate_gate(policy(), original).decision == ("FAIL" if observed else "UNKNOWN")
        old_items = deepcopy(original.items)
        results = session.scalars(select(EvaluationResultRecord).where(EvaluationResultRecord.item_execution_id == identity["item_id"])).all()
        measured = {r.evaluator_id: (r.status, r.value, r.evidence) for r in results if r.evaluator_id.startswith("trajectory.")}
        assert len(measured) == 4
        expected = dict(zip(("trajectory." + p["rule"] for p in PARAMS), (True, False, False, True), strict=True))
        for rule, (status, value, evidence) in measured.items():
            assert status == ("succeeded" if observed else "no_result") and value is (expected[rule] if observed else None)
            if observed:
                assert evidence["evidence_ref"] == old_items[0]["trajectory_input"]["acquisition"]["evidence_ref"]
        item = session.get(ExperimentItemExecutionRecord, identity["item_id"])
        item.evaluation_status, item.evaluation_generation = "evaluating", 1
        item.eval_status, item.quality_conclusion = "failed", "unknown"
        for result in results:
            result.status = "failed"
    monkeypatch.setattr(worker.trajectory_collector, "_get_factory", lambda: pytest.fail("eval-only reopened Reader"))
    async def forbid_invoke(*args, **kwargs):
        pytest.fail("eval-only called business Agent")
    monkeypatch.setattr("app.worker.RemoteAgentExecutor.invoke_once", forbid_invoke)
    outcome = recover_evaluation(s40_runtime[0], item_id=identity["item_id"], evaluation_generation=1, worker_id="frozen-rules")
    assert outcome["finalized"] and outcome["quality_conclusion"] == quality
    with s40_runtime[0].get_session() as session:
        after = session.scalars(select(EvaluationResultRecord).where(EvaluationResultRecord.item_execution_id == identity["item_id"])).all()
        assert {r.evaluator_id: (r.status, r.value, r.evidence) for r in after if r.evaluator_id.startswith("trajectory.")} == measured
        cp = session.scalar(select(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.item_execution_id == identity["item_id"]))
        session.delete(cp)
    with s40_runtime[0].get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=original.id, dataset_item_id=identity["dataset_item_id"])
        acquired = old_items[0]["trajectory_input"]["acquisition"]
        expected_ref = acquired["evidence_ref"] if acquired is not None else None
        assert (loaded.trajectory_evidence_ref.model_dump(mode="json") if loaded.trajectory_evidence_ref is not None else None) == expected_ref
        from app.db_models import RunResultSnapshotRecord
        assert session.get(RunResultSnapshotRecord, original.id).items == old_items
    rows = local_langfuse.wait_observations(identity["trace_id"], minimum=2 if profile == "http_only" else 8)
    local_langfuse.record(profile, "formal-rules-" + mode + ("-unqualified" if not approved else "") + str(recovery_stage or ""), identity, rows,
        normalization={"formal_rules": True, "original_ref_preserved": True, "eval_only_zero_agent_reader": True,
            "quality_conclusion": quality, "recovery_stage": recovery_stage})


@pytest.mark.parametrize("profile,profile_id", [("native_otel", "native-otel-v1"), ("pydantic_ai", "pydantic-ai-v1")])
def test_real_optional_no_result_keeps_required_complete_through_eval_only(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile, profile_id):
    from app.baselines import validate_baseline_snapshot
    from app.release_gates import evaluate_gate
    from test_issue_7_release_gates import policy
    report = json.loads((Path(__file__).resolve().parents[1] / "docs/validation/s4-1/reader-calibration.json").read_text())
    approval = report["groups"][profile + ":serial"]["approved_identity"]
    contract = TrajectoryContract.model_validate(calibrated_banking_payload())
    factory = QualifiedReaderFactory(server_url=local_langfuse.base_url, public_key=local_langfuse.env["S4_PUBLIC_KEY"],
        secret_key=local_langfuse.env["S4_SECRET_KEY"], approvals=(ReaderApproval(**approval),))
    worker = s40_runtime[4]
    monkeypatch.setattr(worker, "trajectory_collector", WorkerTrajectoryCollector(s40_runtime[0].get_session, factory_provider=lambda: factory))
    selections = [{"id": "trajectory.require_tool", "params": PARAMS[0]}, {"id": "trajectory.tool_order",
        "params": PARAMS[3] | {"scope": "WORKFLOW", "workflow_scope_id": "optional-unobserved-workflow"}}]
    rules = [{"evaluator_id": "intent_match", "result_type": "numeric", "operator": ">=", "threshold": 1, "required": True},
        {"evaluator_id": "trajectory.require_tool", "result_type": "boolean", "operator": "==", "expected_value": True, "required": True},
        {"evaluator_id": "trajectory.tool_order", "result_type": "boolean", "operator": "==", "expected_value": True, "required": False}]
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, trajectory_source_profile_id=profile_id,
            trajectory_contract=contract, rule_selections=selections, quality_policy_rules=rules, expected_quality="pass", expected_eval_status="partial")
    with s40_runtime[0].get_session() as session:
        launch = session.get(ExperimentLaunchRecord, identity["launch_id"])
        launch.status = "COMPLETED"
        snapshot = create_result_snapshot(session, launch)
        assert snapshot.evidence_state == "COMPLETE" and snapshot.items[0]["eval_status"] == "partial"
        assert snapshot.summary["pass_rate"] == snapshot.summary["evaluation_coverage"] == 1
        validate_baseline_snapshot(snapshot, launch, "test-agent")
        assert evaluate_gate(policy(), snapshot).decision == "PASS"
        item = session.get(ExperimentItemExecutionRecord, identity["item_id"])
        assert item.quality_evaluation["required_evidence_status"] == "COMPLETE"
        item.evaluation_status, item.evaluation_generation = "evaluating", 1
        item.eval_status, item.quality_conclusion = "failed", "unknown"
        for result in session.scalars(select(EvaluationResultRecord).where(EvaluationResultRecord.item_execution_id == item.id)):
            result.status = "failed"
    monkeypatch.setattr(worker.trajectory_collector, "_get_factory", lambda: pytest.fail("eval-only reopened Reader"))
    outcome = recover_evaluation(s40_runtime[0], item_id=identity["item_id"], evaluation_generation=1, worker_id="optional-qualification")
    assert outcome["finalized"] and outcome["quality_conclusion"] == "pass"
    with s40_runtime[0].get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, identity["item_id"])
        assert item.eval_status == "partial" and item.quality_evaluation["required_evidence_status"] == "COMPLETE"
        refreshed = create_result_snapshot(session, session.get(ExperimentLaunchRecord, identity["launch_id"]))
        assert refreshed.evidence_state == "COMPLETE" and evaluate_gate(policy(), refreshed).decision == "PASS"
    rows = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
    local_langfuse.record(profile, "required-complete-optional-no-result", identity, rows, normalization={
        "required_evidence_status": "COMPLETE", "optional_status": "no_result", "eval_status": "partial", "gate": "PASS", "eval_only": True})
