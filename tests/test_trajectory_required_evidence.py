"""T4：正式资格按 required 测量计算，optional 诊断和历史记录独立。"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.baselines import validate_baseline_snapshot
from app.db_models import (
    EvaluationResultRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseRunScoreTaskRecord,
    RunResultSnapshotRecord,
)
from app.evaluator_binding import evaluate_frozen_item, resolve_execution_plan, summarize_typed_results
from app.manifest import LaunchService
from app.registry import AgentRegistry
from app.release_gates import evaluate_gate
from app.result_snapshots import create_result_snapshot
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session
from test_issue_7_release_gates import policy
from test_issue_85_snapshot_revision import _api_client
from test_trajectory_evidence import postgres_engine as postgres_engine
from test_trajectory_manifest import context as sqlite_context
from test_trajectory_rules import B, trajectory

ROOT = Path(__file__).resolve().parents[1]
VERSION = "trajectory-1.0"


@pytest.fixture(params=["sqlite", "postgres"])
def runtime(request, tmp_path):
    if request.param == "sqlite":
        db, registry, service, _ = sqlite_context.__wrapped__(tmp_path)
    else:
        engine = request.getfixturevalue("postgres_engine")
        @contextmanager
        def sessions():
            with Session(engine, expire_on_commit=False) as session:
                try:
                    yield session
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
        db = SimpleNamespace(engine=engine, get_session=sessions)
        registry = AgentRegistry(db)
        registry.import_yaml(ROOT / "config/agents.yaml")
        registry.create_version(agent_id="banking-agent", version="native", endpoint="http://demo-agent:8080/invoke",
            trajectory_source_profile_id="native-otel-v1")
        service = LaunchService(db, registry)
    return db, registry, service, trajectory(with_contract=True)[1]


def launch_for(runtime, cases=("case",)):
    return runtime[2].create_launch(agent_id="banking-agent", agent_version="native", dataset_name="golden",
        trajectory_contract=runtime[3], dataset_snapshot={"source": "seed", "dataset_id": "golden", "dataset_version": "v1",
            "items": [{"id": case, "input": {}, "expected_output": {}} for case in cases]},
        evaluator_selections=[{"id": "answer_present", "version": "1.0.0"},
            {"id": "trajectory.max_tool_calls", "version": "1.0.0", "params": {"rule": "max_tool_calls", "tool_identity": B, "max_count": 1}}],
        quality_policy_rules=[{"evaluator_id": "answer_present", "result_type": "boolean", "operator": "==", "expected_value": True, "required": True},
            {"evaluator_id": "trajectory.max_tool_calls", "result_type": "boolean", "operator": "==", "expected_value": True, "required": False}])


def measured(launch, *, optional_status="skipped", required_value=True, problem=None):
    first = evaluate_frozen_item(launch.manifest, output={"answer": "ok"}, expected_output={})
    rows = list(first.typed_results)
    rows[0] = replace(rows[0], value=required_value)
    rows[1] = replace(rows[1], status=optional_status, value=None,
        error_code="OPTIONAL_DIAGNOSTIC" if optional_status == "failed" else None, error_message=None)
    if problem == "missing":
        rows.pop(0)
    elif problem in {"failed", "skipped", "no_result"}:
        rows[0] = replace(rows[0], status=problem, value=None)
    elif problem == "binding":
        rows[0] = replace(rows[0], provenance=replace(rows[0].provenance, binding_id="wrong"))
    elif problem == "schema":
        rows[0] = replace(rows[0], provenance=replace(rows[0].provenance, manifest_schema_version="1.2"))
    elif problem == "duplicate":
        rows.insert(0, rows[0])
    plan = resolve_execution_plan(launch.manifest)
    return rows, summarize_typed_results(plan, rows, manifest=launch.manifest)


def persist(runtime, launch, rows, summary):
    db = runtime[0]
    with db.get_session() as session:
        item = session.scalar(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id,
            ExperimentItemExecutionRecord.dataset_item_id == "case"))
        item.execution_status, item.eval_status, item.quality_conclusion = "succeeded", summary.eval_status, summary.quality_conclusion
        item.quality_evaluation, item.scores, item.dispatch_generation = summary.quality_decision.payload(), summary.scores, 1
        for row in rows:
            p = row.provenance
            session.add(EvaluationResultRecord(id=str(uuid4()), launch_id=launch.id, item_execution_id=item.id,
                evaluator_id=row.evaluator_id, evaluator_version=p.evaluator_version, result_type=row.result_type,
                status=row.status, value=row.value, normalized_value=row.normalized_value, evidence=row.evidence,
                error_code=row.error_code, error_message=row.error_message, binding_id=p.binding_id,
                definition_digest=p.definition_digest, executor_type=p.executor_type,
                manifest_schema_version=p.manifest_schema_version, contract_status=p.contract_status))
        session.get(ExperimentLaunchRecord, launch.id).status = "COMPLETED"
    return item.id


def freeze(runtime, launch):
    with runtime[0].get_session() as session:
        return create_result_snapshot(session, session.get(ExperimentLaunchRecord, launch.id))


def gate_policy():
    return policy().model_copy(update={"agent_id": "banking-agent"})


@pytest.mark.parametrize("status", ["skipped", "failed", "no_result"])
def test_optional_diagnostics_preserve_complete_snapshot_baseline_and_gate(runtime, status):
    launch = launch_for(runtime)
    assert launch.manifest.get("evidence_contract_version") == VERSION
    rows, summary = measured(launch, optional_status=status)
    assert summary.quality_conclusion == "pass" and summary.eval_status in {"partial", "failed"}
    assert getattr(summary, "required_evidence_status", None) == "COMPLETE"
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    assert snapshot.evidence_state == "COMPLETE"
    assert snapshot.items[0]["required_evidence_status"] == "COMPLETE"
    assert snapshot.items[0]["evaluation_results"][1]["status"] == status
    assert snapshot.summary["evaluation_coverage"] == 1 and snapshot.summary["pass_rate"] == 1
    validate_baseline_snapshot(snapshot, launch, "banking-agent")
    assert evaluate_gate(gate_policy(), snapshot).decision == "PASS"


@pytest.mark.parametrize("problem,value", [("missing", True), ("failed", True), ("skipped", True), ("no_result", True),
    (None, None), (None, 1), ("binding", True), ("schema", True)])
def test_required_missing_invalid_or_mismatched_results_are_incomplete(runtime, problem, value):
    launch = launch_for(runtime)
    rows, summary = measured(launch, required_value=value, problem=problem)
    assert getattr(summary, "required_evidence_status", None) == "INCOMPLETE"
    assert summary.quality_conclusion == "unknown"
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    assert snapshot.evidence_state == "DIAGNOSTIC" and snapshot.summary["evaluation_coverage"] == 0
    with pytest.raises(ValueError):
        validate_baseline_snapshot(snapshot, launch, "banking-agent")
    assert evaluate_gate(gate_policy(), snapshot).decision == "UNKNOWN"


def test_proven_false_is_complete_evidence_but_gate_refuses_release(runtime):
    launch = launch_for(runtime)
    rows, summary = measured(launch, required_value=False)
    assert getattr(summary, "required_evidence_status", None) == "COMPLETE" and summary.quality_conclusion == "fail"
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    assert snapshot.evidence_state == "COMPLETE" and snapshot.summary["pass_rate"] == 0
    validate_baseline_snapshot(snapshot, launch, "banking-agent")
    verdict = evaluate_gate(gate_policy(), snapshot)
    assert verdict.decision == "FAIL" and not verdict.releasable


@pytest.mark.parametrize("damage", ["item_status", "item_version", "summary_version", "aggregation", "discriminator"])
def test_new_half_contract_is_rejected_by_api_baseline_and_gate(runtime, monkeypatch, damage):
    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    with runtime[0].get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        items, manifest, payload = deepcopy(stored.items), deepcopy(stored.manifest), deepcopy(stored.summary)
        if damage == "item_status":
            items[0].pop("required_evidence_status", None)
        elif damage == "item_version":
            items[0].pop("evidence_contract_version", None)
        elif damage == "summary_version":
            payload.pop("evidence_contract_version", None)
        elif damage == "aggregation":
            manifest["contract_digests"]["aggregation_comparison"]["digest"] = "sha256:" + "0" * 64
        else:
            manifest.pop("evidence_contract_version", None)
        stored.items, stored.manifest, stored.summary = items, manifest, payload
        from app.result_snapshots import _canonical_digest
        stored.source_result_digest, stored.manifest_digest = _canonical_digest(items), _canonical_digest(manifest)
    with runtime[0].get_session() as session:
        snapshot = session.get(RunResultSnapshotRecord, snapshot.id)
        with pytest.raises(ValueError):
            validate_baseline_snapshot(snapshot, launch, "banking-agent")
        assert evaluate_gate(gate_policy(), snapshot).decision == "UNKNOWN"
    with _api_client(monkeypatch, runtime[0]) as client:
        response = client.get(f"/api/v1/experiment-launches/{launch.id}/result-snapshots/{snapshot.id}")
    assert response.status_code == 409 and "EVIDENCE_CONTRACT" in str(response.json())


@pytest.mark.parametrize("stage", ["summary", "serialization", "outbox", "commit"])
def test_snapshot_qualification_and_outbox_roll_back_together(runtime, monkeypatch, stage):
    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    if stage == "summary":
        monkeypatch.setattr("app.result_snapshots.aggregate_run", lambda *a, **kw: (_ for _ in ()).throw(ValueError("serialize failed")))
    elif stage == "serialization":
        from app.result_snapshots import aggregate_run
        monkeypatch.setattr("app.result_snapshots.aggregate_run", lambda *a, **kw: aggregate_run(*a, **kw) | {"non_json": object()})
    elif stage == "outbox":
        monkeypatch.setattr("app.result_snapshots.LangfuseRunScoreTaskRecord", lambda **kw: (_ for _ in ()).throw(ValueError("outbox failed")))
    else:
        original = Session.commit
        failed = False
        def commit(session):
            nonlocal failed
            if not failed:
                failed = True
                raise ValueError("commit failed")
            return original(session)
        monkeypatch.setattr(Session, "commit", commit)
    # psycopg 的 JSON 适配器直接抛 TypeError；SQLite 由 SQLAlchemy 包装。
    expected_error = (
        TypeError if runtime[0].engine.dialect.name == "postgresql" else StatementError
    ) if stage == "serialization" else ValueError
    with pytest.raises(expected_error):
        freeze(runtime, launch)
    with runtime[0].get_session() as session:
        assert session.scalar(select(func.count()).select_from(RunResultSnapshotRecord)) == 0
        assert session.scalar(select(func.count()).select_from(LangfuseRunScoreTaskRecord)) == 0


def test_missing_discriminator_cannot_execute_new_aggregation_as_legacy(runtime):
    launch = launch_for(runtime)
    manifest = deepcopy(launch.manifest)
    manifest.pop("evidence_contract_version", None)
    result = evaluate_frozen_item(manifest, output={"answer": "ok"}, expected_output={})
    assert result.quality_conclusion == "unknown" and result.eval_error == "EVIDENCE_CONTRACT_INVALID"
    assert getattr(result, "required_evidence_status", None) == "INCOMPLETE"


@pytest.mark.parametrize("version", ["1.0", "1.1", "1.2", "1.3"])
def test_legacy_snapshots_keep_optional_diagnostic_qualification(runtime, version):
    from app.comparison_contracts import aggregation_comparison_digest
    launch = launch_for(runtime)
    manifest = deepcopy(launch.manifest)
    manifest.pop("evidence_contract_version")
    manifest["schema_version"] = version
    if version != "1.3":
        manifest.pop("trajectory")
    manifest["comparison"]["comparison_policy_version"] = "comparison-v1"
    manifest["contract_digests"]["aggregation_comparison"] = {"digest": aggregation_comparison_digest(), "version": "comparison-v1"}
    with runtime[0].get_session() as session:
        session.get(ExperimentLaunchRecord, launch.id).manifest = manifest
    launch.manifest = manifest
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    assert snapshot.evidence_state == "DIAGNOSTIC"
    assert "required_evidence_status" not in snapshot.items[0] and "evidence_contract_version" not in snapshot.summary


def test_optional_failure_does_not_block_relative_gate_and_api_exposes_frozen_status(runtime, monkeypatch):
    launch = launch_for(runtime)
    rows, summary = measured(launch, optional_status="failed")
    persist(runtime, launch, rows, summary)
    baseline = freeze(runtime, launch)
    candidate_launch = launch_for(runtime)
    with runtime[0].get_session() as session:
        rec = session.get(ExperimentLaunchRecord, candidate_launch.id)
        manifest = deepcopy(rec.manifest)
        manifest["comparison"]["baseline_snapshot_id"] = baseline.id
        rec.manifest = manifest
        candidate_launch.manifest = manifest
    rows, summary = measured(candidate_launch, optional_status="no_result")
    persist(runtime, candidate_launch, rows, summary)
    candidate = freeze(runtime, candidate_launch)
    relative = gate_policy().model_copy(update={"rules": policy({"id": "delta", "metric": "regression_count", "operator": "<=", "threshold": 0}).rules})
    assert evaluate_gate(relative, candidate, baseline).decision == "PASS"
    with _api_client(monkeypatch, runtime[0]) as client:
        detail = client.get(f"/api/v1/experiment-launches/{candidate_launch.id}/result-snapshots/{candidate.id}")
        summary = client.get(f"/api/v1/experiment-launches/{candidate_launch.id}/summary", params={"snapshot_id": candidate.id})
        cases = client.get(f"/api/v1/experiment-launches/{candidate_launch.id}/items")
    assert detail.status_code == summary.status_code == cases.status_code == 200
    assert detail.json()["items"][0]["required_evidence_status"] == "COMPLETE"
    assert summary.json()["summary"]["evidence_contract_version"] == VERSION
    assert cases.json()[0]["required_evidence_status"] == "COMPLETE"


@pytest.mark.parametrize("field", ["evaluator_id", "evaluator_version", "definition_digest", "executor_type", "contract_status"])
def test_all_required_provenance_fields_must_match_the_frozen_binding(runtime, field):
    launch = launch_for(runtime)
    rows, _ = measured(launch)
    rows[0] = replace(rows[0], provenance=replace(rows[0].provenance, **{field: "forged"}))
    summary = summarize_typed_results(resolve_execution_plan(launch.manifest), rows, manifest=launch.manifest)
    assert summary.required_evidence_status == "INCOMPLETE" and summary.quality_conclusion == "unknown"


def test_duplicate_required_result_is_not_complete(runtime):
    launch = launch_for(runtime)
    rows, _ = measured(launch)
    summary = summarize_typed_results(resolve_execution_plan(launch.manifest), rows + [rows[0]], manifest=launch.manifest)
    assert summary.required_evidence_status == "INCOMPLETE" and summary.quality_conclusion == "unknown"


def test_missing_required_binding_is_finite_incomplete_before_execution(runtime):
    launch = launch_for(runtime)
    manifest = deepcopy(launch.manifest)
    manifest["evaluators"] = manifest["evaluators"][1:]
    from app.evaluator_binding import manifest_measurement_digest
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
    result = evaluate_frozen_item(manifest, output={"answer": "ok"}, expected_output={})
    assert result.required_evidence_status == "INCOMPLETE" and result.quality_conclusion == "unknown" and not result.typed_results


def test_missing_frozen_reference_cannot_make_successful_trajectory_measurement_eligible(runtime):
    from app.evaluator_binding import canonical_digest
    from test_trajectory_rules import context as rule_context
    launch = launch_for(runtime)
    manifest = deepcopy(launch.manifest)
    rules = manifest["quality_policy"]["rules"]
    rules[0]["required"], rules[1]["required"] = False, True
    from app.quality_policy import QualityPolicy
    manifest["quality_policy"]["policy_digest"] = QualityPolicy.from_payload(manifest["quality_policy"]).policy_digest
    manifest["contract_digests"]["quality_policy"]["digest"] = manifest["quality_policy"]["policy_digest"]
    with runtime[0].get_session() as session:
        session.get(ExperimentLaunchRecord, launch.id).manifest = manifest
    launch.manifest = manifest
    result = evaluate_frozen_item(manifest, output={"answer": "ok"}, expected_output={}, context=rule_context(trajectory()))
    assert result.typed_results[1].value is True
    assert result.required_evidence_status == "INCOMPLETE" and result.quality_conclusion == "unknown"
    persist(runtime, launch, result.typed_results, result)
    snapshot = freeze(runtime, launch)
    assert snapshot.items[0]["required_evidence_status"] == "INCOMPLETE" and snapshot.items[0]["quality_conclusion"] == "unknown"
    assert snapshot.evidence_state == "DIAGNOSTIC" and evaluate_gate(gate_policy(), snapshot).decision == "UNKNOWN"
    assert snapshot.manifest_digest.removeprefix("sha256:") == canonical_digest(manifest).removeprefix("sha256:")


def test_live_item_cannot_claim_complete_from_a_synthetic_reference(runtime, monkeypatch):
    from app.quality_policy import QualityPolicy
    from test_trajectory_rules import context as rule_context
    launch = launch_for(runtime)
    manifest = deepcopy(launch.manifest)
    rules = manifest["quality_policy"]["rules"]
    rules[0]["required"], rules[1]["required"] = False, True
    manifest["quality_policy"]["policy_digest"] = QualityPolicy.from_payload(manifest["quality_policy"]).policy_digest
    manifest["contract_digests"]["quality_policy"]["digest"] = manifest["quality_policy"]["policy_digest"]
    with runtime[0].get_session() as session:
        session.get(ExperimentLaunchRecord, launch.id).manifest = manifest
    launch.manifest = manifest
    result = evaluate_frozen_item(manifest, output={"answer": "ok"}, expected_output={}, context=rule_context(trajectory()))
    assert result.typed_results[1].value is True
    persist(runtime, launch, result.typed_results, result)
    with _api_client(monkeypatch, runtime[0]) as client:
        response = client.get(f"/api/v1/experiment-launches/{launch.id}/items")
    assert response.status_code == 200
    assert response.json()[0]["required_evidence_status"] == "INCOMPLETE"


@pytest.mark.parametrize("damage", ["manifest_null", "summary_list", "items_scalar", "item_null", "comparison_scalar"])
def test_malformed_snapshot_is_finite_conflict_and_unknown(runtime, monkeypatch, damage):
    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    snapshot = freeze(runtime, launch)
    with runtime[0].get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        if damage == "manifest_null":
            stored.manifest = None
        elif damage == "summary_list":
            stored.summary = [1]
        elif damage == "items_scalar":
            stored.items = "damaged"
        elif damage == "item_null":
            stored.items = [None]
        else:
            changed = deepcopy(stored.manifest)
            changed["comparison"] = [1]
            stored.manifest = changed
    with runtime[0].get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        with pytest.raises(ValueError, match="EVIDENCE_CONTRACT_INVALID"):
            validate_baseline_snapshot(stored, launch, "banking-agent")
        assert evaluate_gate(gate_policy(), stored).decision == "UNKNOWN"
    with _api_client(monkeypatch, runtime[0]) as client:
        response = client.get(f"/api/v1/experiment-launches/{launch.id}/result-snapshots/{snapshot.id}")
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"


@pytest.fixture(params=["sqlite", "postgres"])
def required_trajectory_snapshot(request, tmp_path):
    from app.comparison_contracts import aggregation_comparison_digest
    from app.db_models import ExecutionCheckpointRecord
    from app.evaluation_result_store import persist_typed_results
    from app.evaluator_binding import canonical_digest, freeze_binding, manifest_measurement_digest
    from app.evaluators import default_evaluator_registry
    from app.execution_checkpoint import binding_provenance_from_manifest
    from app.quality_policy import QualityRule, freeze_quality_policy
    from app.required_evidence import COMPARISON_POLICY_VERSION
    from app.trajectory.inputs import make_evaluation_context
    from test_trajectory_inputs import context as input_context
    from test_trajectory_inputs import ready
    setup = input_context.__wrapped__(request, tmp_path)
    prepared = next(setup)
    db, scope, _, _ = prepared
    try:
        with db.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            manifest = deepcopy(launch.manifest)
            registry = default_evaluator_registry
            binding = freeze_binding(registry.definition("trajectory.max_tool_calls"), registry.version("trajectory.max_tool_calls", "1.0.0"),
                runner_identity=manifest["runner"], params={"rule": "max_tool_calls", "tool_identity": B, "max_count": 10})
            manifest["evaluators"] = [binding.to_payload()]
            rule = QualityRule(evaluator_id=binding.evaluator_id, result_type="boolean", operator="==", expected_value=True)
            manifest["quality_policy"] = freeze_quality_policy(policy_id="required-trajectory", version="1.0", rules=[rule],
                bindings={binding.evaluator_id: binding}).to_payload()
            manifest["evidence_contract_version"] = VERSION
            manifest["comparison"] = {"environment": "production", "baseline_snapshot_id": None, "comparison_policy_version": COMPARISON_POLICY_VERSION}
            manifest["measurement_digest"] = manifest_measurement_digest(manifest)
            manifest["contract_digests"].update(measurement={"version": "1.3", "digest": manifest["measurement_digest"]},
                quality_policy={"version": "required-trajectory@1.0", "digest": manifest["quality_policy"]["policy_digest"]},
                aggregation_comparison={"version": COMPARISON_POLICY_VERSION, "digest": aggregation_comparison_digest(evidence_contract_version=VERSION)})
            launch.manifest = manifest
            cp = session.get(ExecutionCheckpointRecord, "checkpoint")
            cp.manifest_digest, cp.binding_provenance = canonical_digest(manifest), binding_provenance_from_manifest(manifest)
        acquired = ready.__wrapped__(prepared)
        with db.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            cp = session.get(ExecutionCheckpointRecord, "checkpoint")
            ctx = make_evaluation_context(session, cp, manifest)
            result = evaluate_frozen_item(manifest, output=ctx.output, expected_output=ctx.expected_output, context=ctx)
            assert result.required_evidence_status == "COMPLETE" and result.quality_conclusion == "pass"
            item = session.get(ExperimentItemExecutionRecord, scope.item_execution_id)
            item.execution_status, item.eval_status, item.quality_conclusion = "succeeded", result.eval_status, result.quality_conclusion
            item.quality_evaluation, item.scores = result.quality_decision.payload(), result.scores
            item.final_attempt_id, item.active_attempt_id = scope.attempt_id, None
            item.trace_id, item.observation_id = scope.trace_id, scope.root_observation_id
            launch.status = "COMPLETED"
            persist_typed_results(session, item_execution_id=item.id, launch_id=launch.id, results=list(result.typed_results))
            session.flush()
            snapshot = create_result_snapshot(session, launch)
            assert snapshot.evidence_state == "COMPLETE"
        yield db, snapshot, acquired
    finally:
        setup.close()


@pytest.mark.parametrize("damage", ["partial_ref", "deleted_artifact", "corrupted_artifact", "scope", "acquisition_scalar"])
def test_required_snapshot_revalidates_the_owned_artifact(required_trajectory_snapshot, monkeypatch, damage):
    from app.db_models import TrajectoryEvidenceArtifactRecord
    from app.result_snapshots import _canonical_digest
    db, original, acquired = required_trajectory_snapshot
    with db.get_session() as session:
        snapshot = session.get(RunResultSnapshotRecord, original.id)
        items = deepcopy(snapshot.items)
        artifact = session.get(TrajectoryEvidenceArtifactRecord, acquired.evidence_ref.artifact_id)
        if damage == "deleted_artifact":
            session.delete(artifact)
        elif damage == "corrupted_artifact":
            artifact.payload_json = "{}"
        elif damage == "partial_ref":
            for result in items[0]["evaluation_results"]:
                result["evidence"]["evidence_ref"] = {"artifact_id": "fake"}
            items[0]["trajectory_input"]["acquisition"]["evidence_ref"] = {"artifact_id": "fake"}
        elif damage == "scope":
            items[0]["trajectory_input"]["acquisition"]["scope"]["dataset_item_id"] = "sibling"
        else:
            items[0]["trajectory_input"]["acquisition"] = [1]
        snapshot.items, snapshot.source_result_digest = items, _canonical_digest(items)
    with db.get_session() as session:
        snapshot = session.get(RunResultSnapshotRecord, original.id)
        launch = session.get(ExperimentLaunchRecord, snapshot.launch_id)
        with pytest.raises(ValueError, match="EVIDENCE_CONTRACT_INVALID"):
            validate_baseline_snapshot(snapshot, launch, snapshot.agent_id)
        from test_issue_7_release_gates import policy as release_policy
        assert evaluate_gate(release_policy(), snapshot).decision == "UNKNOWN"
        assert snapshot.items == items and snapshot.evidence_state == "COMPLETE", "历史原文不得回写"
    with _api_client(monkeypatch, db) as client:
        response = client.get(f"/api/v1/experiment-launches/{original.launch_id}/result-snapshots/{original.id}")
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"


def test_missing_case_stays_in_required_coverage_denominator(runtime):
    launch = launch_for(runtime, cases=("case", "missing"))
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    with runtime[0].get_session() as session:
        missing = session.scalar(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id,
            ExperimentItemExecutionRecord.dataset_item_id == "missing"))
        session.delete(missing)
    snapshot = freeze(runtime, launch)
    assert snapshot.evidence_state == "DIAGNOSTIC"
    assert snapshot.summary["total_cases"] == 2 and snapshot.summary["evaluated_cases"] == 1
    assert snapshot.summary["evaluation_coverage"] == 0.5 and snapshot.summary["required_evidence_status"] == "INCOMPLETE"
    with pytest.raises(ValueError):
        validate_baseline_snapshot(snapshot, launch, "banking-agent")
    assert evaluate_gate(gate_policy(), snapshot).decision == "UNKNOWN"


def test_live_case_before_evaluation_is_explicitly_required_incomplete(runtime, monkeypatch):
    launch = launch_for(runtime)
    with _api_client(monkeypatch, runtime[0]) as client:
        response = client.get(f"/api/v1/experiment-launches/{launch.id}/items")
    assert response.status_code == 200 and response.json()[0]["required_evidence_status"] == "INCOMPLETE"


def test_invalid_new_contract_manual_freeze_returns_finite_conflict(runtime, monkeypatch):
    launch = launch_for(runtime)
    rows, summary = measured(launch)
    persist(runtime, launch, rows, summary)
    with runtime[0].get_session() as session:
        rec = session.get(ExperimentLaunchRecord, launch.id)
        manifest = deepcopy(rec.manifest)
        manifest["evidence_contract_version"] = "unknown-version"
        rec.manifest = manifest
    with _api_client(monkeypatch, runtime[0]) as client:
        response = client.post(f"/api/v1/experiment-launches/{launch.id}/result-snapshots")
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"
