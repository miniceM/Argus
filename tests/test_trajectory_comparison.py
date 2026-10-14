"""T7：两份受控持久化轨迹的正式比较，不以假引用替代制品。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.aggregation import aggregate_run
from app.db_models import RunResultSnapshotRecord, TrajectoryEvidenceArtifactRecord
from app.evaluator_binding import evaluate_frozen_item, freeze_binding, manifest_measurement_digest
from app.evaluators import default_evaluator_registry
from app.quality_policy import QualityPolicy
from app.required_evidence import required_quality_decision, validate_snapshot_contract
from app.result_snapshots import _canonical_digest
from app.trajectory.evidence import persist_trajectory_evidence
from app.trajectory.inputs import EvaluationContext, FrozenTrajectoryInput, load_snapshot_input
from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract, load_trajectory_contract
from test_issue_85_snapshot_revision import _api_client
from test_trajectory_assembler import configured
from test_trajectory_evidence import postgres_engine as postgres_engine
from test_trajectory_evidence import reseal
from test_trajectory_required_evidence import _required_trajectory_snapshot, freeze, launch_for, measured, persist
from test_trajectory_required_evidence import runtime as runtime


@pytest.fixture
def pair(comparison_trajectory_snapshot):
    """同一成功 Attempt 的两个不可变测量制品，构造历史/候选 Snapshot。"""
    db, baseline, _ = comparison_trajectory_snapshot

    def candidate(*, policy_changed=False, measurement_changed=False, contract_changed=False):
        with db.get_session() as session:
            old = session.get(RunResultSnapshotRecord, baseline.id)
            before = deepcopy((old.manifest, old.items, old.summary))
            loaded = load_snapshot_input(session, snapshot_id=old.id, dataset_item_id=old.items[0]["dataset_item_id"])
            assert loaded.trajectory is not None
            contract = load_trajectory_contract(old.manifest)
            changed = reseal(loaded.trajectory, lambda p: p["steps"][2].update(duration_ms=9))
            if contract_changed:
                contract = configured(contract, limits={"max_pages": contract.limits.max_pages - 1})
                changed = reseal(changed, lambda p: p["provenance"].update(trajectory_contract_digest=contract.digest))
            ref = persist_trajectory_evidence(session, changed, contract)
            assert ref.trajectory_digest != loaded.trajectory_evidence_ref.trajectory_digest
            manifest = deepcopy(old.manifest)
            manifest["comparison"]["baseline_snapshot_id"] = old.id
            if contract_changed:
                manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract),
                    SimpleNamespace(trajectory_source_profile_id=manifest["agent"]["trajectory_source_profile_id"]))
            if policy_changed:
                manifest["quality_policy"]["rules"][0]["expected_value"] = False
                manifest["quality_policy"]["policy_digest"] = QualityPolicy.from_payload(manifest["quality_policy"]).policy_digest
                manifest["contract_digests"]["quality_policy"]["digest"] = manifest["quality_policy"]["policy_digest"]
            if measurement_changed:
                previous = manifest["evaluators"][0]
                registry = default_evaluator_registry
                manifest["evaluators"] = [freeze_binding(registry.definition(previous["id"]),
                    registry.version(previous["id"], previous["version"]), runner_identity=manifest["runner"],
                    params=previous["params"] | {"max_count": 11}).to_payload()]
            if measurement_changed or contract_changed:
                manifest["measurement_digest"] = manifest_measurement_digest(manifest)
                manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
            acquisition = loaded.acquisition.model_copy(update={"evidence_ref": ref})
            frozen = FrozenTrajectoryInput(contract.digest, acquisition, changed)
            ctx = EvaluationContext({"answer": "synthetic"}, {}, {}, changed, ref, frozen)
            result = evaluate_frozen_item(manifest, output=ctx.output, expected_output=ctx.expected_output, context=ctx)
            assert result.required_evidence_status == "COMPLETE"
            items = deepcopy(old.items)
            items[0]["trajectory_input"] = frozen.to_payload()
            items[0]["evaluation_results"] = [r.to_payload() | {"evaluator_version": r.provenance.evaluator_version} for r in result.typed_results]
            items[0]["scores"] = result.scores
            decision = required_quality_decision(manifest, items[0]["evaluation_results"], trajectory_input=items[0]["trajectory_input"])
            items[0].update(required_evidence_status=decision.required_evidence_status,
                quality_evaluation=decision.payload(), quality_conclusion=decision.conclusion)
            summary = aggregate_run(items, manifest["evaluators"],
                required_case_ids=[i["id"] for i in manifest["dataset"]["items"]], evidence_contract_version="trajectory-1.0")
            value = RunResultSnapshotRecord(id=str(uuid4()), launch_id=old.launch_id, agent_id=old.agent_id,
                revision=old.revision + 1, manifest=manifest, manifest_digest=_canonical_digest(manifest),
                items=items, source_result_digest=_canonical_digest(items), summary=summary,
                evidence_state="COMPLETE", evidence_reasons=[])
            session.add(value)
            session.flush()
            validate_snapshot_contract(old)
            validate_snapshot_contract(value)
            assert (old.manifest, old.items, old.summary) == before
            return value

    return db, baseline, candidate


@pytest.fixture(params=["sqlite", "postgres"])
def comparison_trajectory_snapshot(request, tmp_path):
    yield from _required_trajectory_snapshot(request, tmp_path, complete_identity=True)


def comparison(db, candidate, monkeypatch):
    with _api_client(monkeypatch, db) as client:
        response = client.get(f"/api/v1/experiment-launches/{candidate.launch_id}/comparison", params={"snapshot_id": candidate.id})
    assert response.status_code == 200, response.text
    return response.json()


def test_different_owned_trajectory_digests_are_formally_comparable(pair, monkeypatch):
    db, baseline, create = pair
    candidate = create()
    assert candidate.manifest["measurement_digest"] == baseline.manifest["measurement_digest"]
    assert candidate.source_result_digest != baseline.source_result_digest
    body = comparison(db, candidate, monkeypatch)
    assert body["formal"]["available"] is True and body["formal"]["verdict"] == "UNCHANGED"
    assert body["formal"]["required_cases"] == body["formal"]["comparable_cases"] == 1
    assert body["formal"]["coverage"] == 1 and body["items"][0]["basis"] == "FORMAL"


@pytest.mark.parametrize("change,reason", [("policy", "QUALITY_POLICY_CHANGED"), ("measurement", "MEASUREMENT_CHANGED")])
def test_frozen_contract_change_withholds_formal_trajectory_comparison(pair, monkeypatch, change, reason):
    db, _, create = pair
    candidate = create(policy_changed=change == "policy", measurement_changed=change == "measurement")
    body = comparison(db, candidate, monkeypatch)
    assert body["formal"]["available"] is False and body["formal"]["verdict"] is None
    assert reason in body["formal"]["withheld_reasons"]
    assert all(i["basis"] == "DIAGNOSTIC_ONLY" for i in body["items"])


def test_changed_trajectory_contract_enters_measurement_and_withholds_formal(pair, monkeypatch):
    db, baseline, create = pair
    candidate = create(contract_changed=True)
    assert candidate.manifest["trajectory"]["trajectory_contract_digest"] != baseline.manifest["trajectory"]["trajectory_contract_digest"]
    assert candidate.manifest["measurement_digest"] != baseline.manifest["measurement_digest"]
    assert candidate.manifest["contract_digests"]["quality_policy"] == baseline.manifest["contract_digests"]["quality_policy"]
    assert candidate.manifest["contract_digests"]["aggregation_comparison"] == baseline.manifest["contract_digests"]["aggregation_comparison"]
    body = comparison(db, candidate, monkeypatch)
    assert body["formal"]["available"] is False and "MEASUREMENT_CHANGED" in body["formal"]["withheld_reasons"]
    assert all(i["basis"] == "DIAGNOSTIC_ONLY" for i in body["items"])


@pytest.mark.parametrize("damage", ["missing", "cross_attempt"])
def test_invalid_candidate_artifact_returns_stable_conflict(pair, monkeypatch, damage):
    db, _, create = pair
    candidate = create()
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, candidate.id)
        items = deepcopy(stored.items)
        if damage == "missing":
            session.delete(session.get(TrajectoryEvidenceArtifactRecord,
                items[0]["trajectory_input"]["acquisition"]["evidence_ref"]["artifact_id"]))
        else:
            items[0]["trajectory_input"]["acquisition"]["scope"]["attempt_id"] = "another-attempt"
            stored.items, stored.source_result_digest = items, _canonical_digest(items)
    with _api_client(monkeypatch, db) as client:
        response = client.get(f"/api/v1/experiment-launches/{candidate.launch_id}/comparison", params={"snapshot_id": candidate.id})
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"


@pytest.mark.parametrize("damage", ["missing", "cross_attempt"])
def test_invalid_baseline_artifact_withholds_formal_and_case_basis(pair, monkeypatch, damage):
    db, baseline, create = pair
    candidate = create()
    with db.get_session() as session:
        old = session.get(RunResultSnapshotRecord, baseline.id)
        items = deepcopy(old.items)
        if damage == "missing":
            artifact_id = items[0]["trajectory_input"]["acquisition"]["evidence_ref"]["artifact_id"]
            session.delete(session.get(TrajectoryEvidenceArtifactRecord, artifact_id))
        else:
            items[0]["trajectory_input"]["acquisition"]["scope"]["attempt_id"] = "another-attempt"
            old.items, old.source_result_digest = items, _canonical_digest(items)
        before = deepcopy((old.manifest, old.items, old.summary))
    body = comparison(db, candidate, monkeypatch)
    assert body["formal"]["available"] is False
    assert "BASELINE_EVIDENCE_CONTRACT_INVALID" in body["formal"]["withheld_reasons"]
    assert all(i["basis"] == "DIAGNOSTIC_ONLY" for i in body["items"])
    with _api_client(monkeypatch, db) as client:
        detail = client.get(f"/api/v1/experiment-launches/{candidate.launch_id}/comparison/case",
            params={"snapshot_id": candidate.id, "dataset_item_id": candidate.items[0]["dataset_item_id"]})
    assert detail.status_code == 200
    assert detail.json()["classification"] == "NOT_COMPARABLE"
    assert detail.json()["reason"] == "BASELINE_EVIDENCE_CONTRACT_INVALID"
    with db.get_session() as session:
        old = session.get(RunResultSnapshotRecord, baseline.id)
        assert (old.manifest, old.items, old.summary) == before


def test_missing_required_case_keeps_frozen_dataset_comparison_denominator(runtime, monkeypatch):
    from app.db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
    from sqlalchemy import select

    snapshots = []
    for _ in range(2):
        launch = launch_for(runtime, cases=("case", "missing"))
        rows, result = measured(launch)
        persist(runtime, launch, rows, result)
        with runtime[0].get_session() as session:
            missing = session.scalar(select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == launch.id,
                ExperimentItemExecutionRecord.dataset_item_id == "missing"))
            session.delete(missing)
            if snapshots:
                stored = session.get(ExperimentLaunchRecord, launch.id)
                manifest = deepcopy(stored.manifest)
                manifest["comparison"]["baseline_snapshot_id"] = snapshots[0].id
                stored.manifest = manifest
        snapshots.append(freeze(runtime, launch))
    body = comparison(runtime[0], snapshots[1], monkeypatch)
    assert body["formal"]["available"] is False
    assert body["formal"]["required_cases"] == 2
    assert body["formal"]["comparable_cases"] == 1
    assert body["formal"]["coverage"] == 0.5
    assert "COVERAGE_INCOMPLETE" in body["formal"]["withheld_reasons"]
    assert all(i["basis"] == "DIAGNOSTIC_ONLY" for i in body["items"])


@pytest.mark.parametrize("damage", ["manifest", "items", "summary", "dataset"])
@pytest.mark.parametrize("endpoint", ["comparison", "comparison/case"])
def test_malformed_baseline_has_finite_conflict(pair, monkeypatch, damage, endpoint):
    db, baseline, create = pair
    candidate = create()
    with db.get_session() as session:
        old = session.get(RunResultSnapshotRecord, baseline.id)
        if damage == "dataset":
            manifest = deepcopy(old.manifest)
            manifest["dataset"]["items"] = None
            old.manifest = manifest
        else:
            setattr(old, damage, [None] if damage == "items" else None)
    with _api_client(monkeypatch, db) as client:
        response = client.get(f"/api/v1/experiment-launches/{candidate.launch_id}/{endpoint}",
            params={"snapshot_id": candidate.id, "dataset_item_id": candidate.items[0]["dataset_item_id"]})
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"
