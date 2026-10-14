"""T5：只读固定 Snapshot，身份隔离、有限缺证据和精确数值显示。"""

import json
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from app.db_models import (
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    RunResultSnapshotRecord,
    TrajectoryEvidenceArtifactRecord,
)
from app.required_evidence import required_quality_decision
from app.result_snapshots import _canonical_digest
from app.trajectory.codec import canonical_dumps
from app.trajectory.evidence import persist_trajectory_evidence
from app.trajectory.inputs import load_snapshot_input
from app.trajectory.manifest import load_trajectory_contract
from test_issue_85_snapshot_revision import _api_client
from test_trajectory_evidence import postgres_engine as postgres_engine
from test_trajectory_evidence import reseal
from test_trajectory_required_evidence import required_trajectory_snapshot as required_trajectory_snapshot


def request(client, snapshot, **changes):
    query = {"snapshot_id": snapshot.id, "dataset_item_id": snapshot.items[0]["dataset_item_id"]} | changes
    return client.get(f"/api/v1/experiment-launches/{snapshot.launch_id}/trajectory", params=query)


def test_read_uses_frozen_snapshot_after_checkpoint_and_live_manifest_change(required_trajectory_snapshot, monkeypatch):
    db, snapshot, acquired = required_trajectory_snapshot
    original = deepcopy(snapshot.items)
    with db.get_session() as session:
        session.delete(session.get(ExecutionCheckpointRecord, "checkpoint"))
        session.get(ExperimentLaunchRecord, snapshot.launch_id).manifest = {"schema_version": "1.2"}
    monkeypatch.setattr("app.trajectory.factory.QualifiedReaderFactory.open", lambda *a, **kw: pytest.fail("diagnosis opened online Reader"))
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 200
    body = response.json()
    assert (body["launch_id"], body["snapshot_id"], body["dataset_item_id"]) == (snapshot.launch_id, snapshot.id, original[0]["dataset_item_id"])
    assert body["acquisition_state"] == "READY" and body["reason_code"] is None
    assert body["evidence_ref"] == acquired.evidence_ref.model_dump(mode="json")
    assert body["trajectory"]["scope"]["attempt_id"] == original[0]["final_attempt_id"]
    assert body["rule_results"][0]["conclusion"] == "pass"
    assert body["rule_results"][0]["typed_status"] == "succeeded"
    assert body["rule_results"][0]["evaluator_version"] == "1.0.0"
    for step in body["trajectory"]["steps"]:
        assert json.loads(body["step_argument_display_json"][step["step_id"]]) == step["arguments_redacted"]
    assert "synthetic-secret" not in response.text
    assert "payload_json" not in body and "headers" not in body
    with db.get_session() as session:
        assert session.get(RunResultSnapshotRecord, snapshot.id).items == original


@pytest.mark.parametrize("query", [{}, {"snapshot_id": "missing"}, {"dataset_item_id": "missing"}])
def test_both_snapshot_and_case_are_required(required_trajectory_snapshot, monkeypatch, query):
    db, snapshot, _ = required_trajectory_snapshot
    with _api_client(monkeypatch, db) as client:
        response = client.get(f"/api/v1/experiment-launches/{snapshot.launch_id}/trajectory", params=query)
    assert response.status_code == 422


@pytest.mark.parametrize("identity", ["launch", "snapshot", "case"])
def test_foreign_identity_is_not_disclosed(required_trajectory_snapshot, monkeypatch, identity):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        source = session.get(ExperimentLaunchRecord, snapshot.launch_id)
        attributes = {column.key: getattr(source, column.key) for column in source.__table__.columns}
        attributes.update(id="foreign-launch", idempotency_key=None)
        session.add(ExperimentLaunchRecord(**attributes))
        session.flush()
        attributes = {column.key: getattr(snapshot, column.key) for column in snapshot.__table__.columns}
        attributes.update(id="foreign-snapshot", launch_id="foreign-launch")
        session.add(RunResultSnapshotRecord(**attributes))
        source = session.get(ExperimentItemExecutionRecord, "item-1")
        attributes = {column.key: getattr(source, column.key) for column in source.__table__.columns}
        attributes.update(id="sibling-item", dataset_item_id="sibling-case")
        session.add(ExperimentItemExecutionRecord(**attributes))
    with _api_client(monkeypatch, db) as client:
        if identity == "launch":
            response = client.get("/api/v1/experiment-launches/foreign-launch/trajectory",
                params={"snapshot_id": snapshot.id, "dataset_item_id": snapshot.items[0]["dataset_item_id"]})
        else:
            response = request(client, snapshot, **({"snapshot_id": "foreign-snapshot"} if identity == "snapshot" else {"dataset_item_id": "sibling-case"}))
    assert response.status_code == 404 and snapshot.id not in response.text


@pytest.mark.parametrize("damage", ["deleted", "corrupt", "reference", "scope"])
def test_unavailable_evidence_has_finite_reason_and_no_green_pass(required_trajectory_snapshot, monkeypatch, damage):
    db, snapshot, acquired = required_trajectory_snapshot
    with db.get_session() as session:
        artifact = session.get(TrajectoryEvidenceArtifactRecord, acquired.evidence_ref.artifact_id)
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        if damage == "deleted":
            session.delete(artifact)
        elif damage == "corrupt":
            artifact.payload_json = "{}"
        else:
            rows = deepcopy(stored.items)
            acquisition = rows[0]["trajectory_input"]["acquisition"]
            if damage == "reference":
                acquisition["evidence_ref"]["artifact_id"] = "other-artifact"
            else:
                acquisition["scope"]["dataset_item_id"] = "sibling-case"
            stored.items, stored.source_result_digest = rows, _canonical_digest(rows)
        original = deepcopy(stored.items)
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 200
    body = response.json()
    assert body["acquisition_state"] == "UNAVAILABLE" and body["trajectory"] is None
    assert body["reason_code"].startswith("TRAJECTORY_")
    assert body["step_argument_display_json"] == {}
    assert all(rule["conclusion"] != "pass" for rule in body["rule_results"])
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        assert stored.items == original and stored.evidence_state == "COMPLETE"


def test_historical_snapshot_has_contractual_unavailable_reason(required_trajectory_snapshot, monkeypatch):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        stored.manifest = {"schema_version": "1.2"}
        rows = deepcopy(stored.items)
        rows[0].pop("trajectory_input")
        stored.items, stored.source_result_digest = rows, _canonical_digest(rows)
        stored.manifest_digest = _canonical_digest(stored.manifest)
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 200
    assert response.json()["reason_code"] == "HISTORICAL_TRAJECTORY_UNAVAILABLE"
    assert response.json()["trajectory"] is None


@pytest.mark.parametrize("status,value,conclusion", [("succeeded", False, "fail"), ("no_result", None, "unknown")])
def test_measurement_success_is_not_quality_pass(required_trajectory_snapshot, monkeypatch, status, value, conclusion):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        rows = deepcopy(stored.items)
        row = rows[0]
        result = row["evaluation_results"][0]
        result.update(status=status, value=value, evidence=result["evidence"] | {"reason_code": "TRAJECTORY_RULE_VIOLATED" if value is False else "TRAJECTORY_CAPABILITY_UNAVAILABLE"})
        decision = required_quality_decision(stored.manifest, row["evaluation_results"], trajectory_input=row["trajectory_input"])
        row.update(quality_evaluation=decision.payload(), quality_conclusion=decision.conclusion,
            required_evidence_status=decision.required_evidence_status)
        stored.items, stored.source_result_digest = rows, _canonical_digest(rows)
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 200
    rule = response.json()["rule_results"][0]
    assert rule["typed_status"] == status and rule["value"] is value
    assert rule["conclusion"] == conclusion


@pytest.mark.parametrize("value", [9007199254740993, Decimal("0.12345678901234567890123456789")])
def test_parameters_keep_exact_json_numbers_and_same_artifact_display(required_trajectory_snapshot, monkeypatch, value):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=snapshot.items[0]["dataset_item_id"])
        contract = load_trajectory_contract(snapshot.manifest)
        def change(payload):
            for step in payload["steps"]:
                if step["arguments_redacted"] == {"customer_id": "synthetic-001"}:
                    step["arguments_redacted"]["customer_id"] = value
        exact = reseal(loaded.trajectory, change)
        ref = persist_trajectory_evidence(session, exact, contract)
        rows = deepcopy(snapshot.items)
        rows[0]["trajectory_input"]["acquisition"]["evidence_ref"] = ref.model_dump(mode="json")
        rows[0]["evaluation_results"][0]["evidence"]["evidence_ref"] = ref.model_dump(mode="json")
        revision = RunResultSnapshotRecord(id=str(uuid4()), launch_id=snapshot.launch_id, agent_id=snapshot.agent_id,
            revision=snapshot.revision + 1, manifest=snapshot.manifest, manifest_digest=snapshot.manifest_digest,
            items=rows, source_result_digest=_canonical_digest(rows), summary=snapshot.summary,
            evidence_state=snapshot.evidence_state, evidence_reasons=snapshot.evidence_reasons, created_at=snapshot.created_at)
        session.add(revision)
    with _api_client(monkeypatch, db) as client:
        response = request(client, revision)
    assert response.status_code == 200
    body = json.loads(response.text, parse_float=Decimal)
    step = next(s for s in body["trajectory"]["steps"] if s["step_id"] == "exec-a")
    assert step["arguments_redacted"]["customer_id"] == value
    assert body["step_argument_display_json"]["exec-a"] == canonical_dumps(exact.steps[step["index"]].arguments_redacted)
    assert json.loads(body["step_argument_display_json"]["exec-a"], parse_float=Decimal)["customer_id"] == value


@pytest.mark.parametrize("damage", ["policy_conclusion", "result_reference", "result_provenance", "item_conclusion"])
def test_resealed_result_cannot_claim_foreign_or_inconsistent_quality(required_trajectory_snapshot, monkeypatch, damage):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        rows = deepcopy(stored.items)
        result = rows[0]["evaluation_results"][0]
        if damage == "policy_conclusion":
            result["value"] = False
        elif damage == "result_reference":
            result["evidence"]["evidence_ref"]["artifact_id"] = "foreign-artifact"
        elif damage == "result_provenance":
            result["provenance"]["binding_id"] = "foreign-binding"
        else:
            rows[0]["quality_conclusion"] = "fail"
        stored.items, stored.source_result_digest = rows, _canonical_digest(rows)
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"


@pytest.mark.parametrize("manifest,reason", [({"schema_version": "1.3"}, "HISTORICAL_TRAJECTORY_UNAVAILABLE"),
    ({"schema_version": "9.0"}, "TRAJECTORY_INPUT_MANIFEST_INVALID")])
def test_no_contract_and_unsupported_version_are_explicit(required_trajectory_snapshot, monkeypatch, manifest, reason):
    db, snapshot, _ = required_trajectory_snapshot
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        stored.manifest, stored.manifest_digest = manifest, _canonical_digest(manifest)
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 200 and response.json()["reason_code"] == reason
    assert response.json()["required_evidence_status"] is None


def test_openapi_matches_canonical_numeric_response():
    from app.main import app
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    diagnosis = schemas["TrajectoryDiagnosisResponse"]
    trajectory = schemas[diagnosis["properties"]["trajectory"]["anyOf"][0]["$ref"].split("/")[-1]]
    step = schemas[trajectory["properties"]["steps"]["items"]["$ref"].split("/")[-1]]
    value = schemas[step["properties"]["arguments_redacted"]["$ref"].split("/")[-1]]
    assert any(branch.get("type") == "number" for branch in value["anyOf"])
    assert any(branch.get("type") == "number" for branch in step["properties"]["duration_ms"]["anyOf"])


@pytest.mark.parametrize("damage", ["foreign_reference", "missing_reference"])
def test_optional_measurement_cannot_borrow_another_artifact(required_trajectory_snapshot, monkeypatch, damage):
    from app.evaluator_binding import freeze_binding, manifest_measurement_digest
    from app.evaluator_results import ResultProvenance
    from app.evaluators import default_evaluator_registry
    from app.quality_policy import QualityPolicy, QualityRule, freeze_quality_policy

    db, snapshot, acquired = required_trajectory_snapshot
    with db.get_session() as session:
        stored = session.get(RunResultSnapshotRecord, snapshot.id)
        manifest = deepcopy(stored.manifest)
        registry = default_evaluator_registry
        optional = freeze_binding(registry.definition("trajectory.forbid_tool"), registry.version("trajectory.forbid_tool", "1.0.0"),
            runner_identity=manifest["runner"], params={"rule": "forbid_tool", "tool_identity": "banking.balance.read"})
        manifest["evaluators"].append(optional.to_payload())
        original = QualityPolicy.from_payload(manifest["quality_policy"])
        from app.evaluator_binding import bindings_from_manifest
        policy = freeze_quality_policy(policy_id=original.policy_id, version=original.version,
            rules=[*original.rules, QualityRule(evaluator_id=optional.evaluator_id, result_type="boolean", operator="==", expected_value=True, required=False)],
            bindings={b.evaluator_id: b for b in bindings_from_manifest(manifest)})
        manifest["quality_policy"] = policy.to_payload()
        manifest["measurement_digest"] = manifest_measurement_digest(manifest)
        manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
        manifest["contract_digests"]["quality_policy"]["digest"] = policy.policy_digest
        rows = deepcopy(stored.items)
        row = deepcopy(rows[0]["evaluation_results"][0])
        row.update(evaluator_id=optional.evaluator_id, provenance=ResultProvenance.from_binding(optional, manifest_schema_version="1.3").payload())
        row["evidence"]["evidence_step_ids"] = ["exec-a"]
        if damage == "foreign_reference":
            row["evidence"]["evidence_ref"] = acquired.evidence_ref.model_dump(mode="json") | {"artifact_id": "foreign-artifact"}
        else:
            row["evidence"].pop("evidence_ref")
        rows[0]["evaluation_results"].append(row)
        decision = required_quality_decision(manifest, rows[0]["evaluation_results"], trajectory_input=rows[0]["trajectory_input"])
        rows[0].update(quality_evaluation=decision.payload(), quality_conclusion=decision.conclusion,
            required_evidence_status=decision.required_evidence_status)
        assert decision.required_evidence_status == "COMPLETE"
        stored.manifest, stored.manifest_digest = manifest, _canonical_digest(manifest)
        stored.items, stored.source_result_digest = rows, _canonical_digest(rows)
    with _api_client(monkeypatch, db) as client:
        response = request(client, snapshot)
    assert response.status_code == 409 and response.json()["detail"] == "EVIDENCE_CONTRACT_INVALID"


def test_openapi_documents_finite_identity_and_evidence_errors():
    from app.main import app
    responses = app.openapi()["paths"]["/api/v1/experiment-launches/{launch_id}/trajectory"]["get"]["responses"]
    assert {"200", "404", "409", "422"}.issubset(responses)
