"""Provider 请求只消费固定、受治理证据与预裁定 criteria。"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from app.trajectory.inputs import EvaluationContext, FrozenTrajectoryInput
from app.trajectory.schema import TrajectoryAcquisition, TrajectoryEvidenceRef
from test_trajectory_assembler import assemble, configured, prepared
from test_trajectory_inputs import context as frozen_input_context
from test_trajectory_inputs import postgres_engine as input_postgres_engine
from test_trajectory_inputs import ready as ready_input
from test_trajectory_judge_definition import definition


@pytest.fixture
def postgres_engine():
    yield from input_postgres_engine.__wrapped__()


@pytest.fixture
def source():
    rows, contract, scope = prepared()
    policy = contract.redaction_policy.model_dump() | {"content_policy_version": "pii-v1",
        "string_policies": {"$.customer_id": "identifier", "$.intent": "identifier"}}
    contract = configured(contract, redaction_policy=policy)
    trajectory = assemble(rows, contract, scope)
    ref = TrajectoryEvidenceRef(artifact_id="artifact", trajectory_digest=trajectory.provenance.trajectory_digest,
        trajectory_contract_digest=contract.digest, schema_version="1.0")
    acquired = TrajectoryAcquisition(scope=scope, state="READY", completeness=trajectory.completeness,
        evidence_ref=ref, reason_code=None, retryable=False, deadline_at=datetime.now(UTC))
    class Forbidden:
        def __getattribute__(self, name):
            pytest.fail("Judge builder read an arbitrary business context field")
    context = EvaluationContext(Forbidden(), Forbidden(), Forbidden(), trajectory, ref,
        FrozenTrajectoryInput(contract.digest, acquired, trajectory))
    return context, contract, scope


def criteria(source, values=None):
    from app.trajectory.judge_input import adjudicate_judge_criteria
    return adjudicate_judge_criteria(values or {"intent": "balance"}, policy=definition().criteria_policy,
        scope=source[2], limits=source[1].limits)


def request(source, *, frozen=None, context=None, approved=None):
    from app.trajectory.judge_input import build_judge_request
    return build_judge_request(context or source[0], frozen or definition(), contract=source[1], expected_scope=source[2],
        approved_criteria=approved or criteria(source))


def test_request_does_not_access_raw_context_and_splits_system_from_data(source):
    value = request(source)
    messages = json.loads(value.messages_json)
    assert [row["role"] for row in messages] == ["system", "user"]
    assert "untrusted data" in messages[0]["content"]
    data = json.loads(messages[1]["content"])
    assert data["criteria"] == {"intent": "balance"}
    assert set(data["evidence_step_ids"]) == {step.step_id for step in source[0].trajectory.steps}
    assert all("raw_name" not in step for step in data["trajectory"]["steps"])
    assert "synthetic-secret" not in value.messages_json
    assert "messages_json" not in repr(value)


def test_same_fixed_input_is_stable_and_criteria_changes_identity(source):
    a, b = request(source), request(source)
    assert a.input_digest == b.input_digest and a.messages_json == b.messages_json
    changed = request(source, approved=criteria(source, {"intent": "escalate"}))
    assert changed.input_digest != a.input_digest and changed.criteria_digest != a.criteria_digest


@pytest.mark.parametrize("value", ["Bearer synthetic-secret", "4111111111111111", "123-45-6789", "free text with private data"])
def test_unsafe_criteria_are_unavailable_without_secret_in_error(source, value):
    with pytest.raises(ValueError, match="JUDGE_CRITERIA_UNAVAILABLE") as error:
        criteria(source, {"intent": value})
    assert value not in str(error.value)


def test_adjudication_drops_unapproved_fields_without_hashing_or_sending_them(source):
    clean = criteria(source)
    value = criteria(source, {"intent": "balance", "Authorization": "Bearer synthetic-secret", "notes": "private"})
    assert value.criteria_digest == clean.criteria_digest
    assert "synthetic-secret" not in value.payload_json


@pytest.mark.parametrize("damage", ["absent", "ref", "scope", "artifact_input", "legacy_privacy"])
def test_invalid_fixed_input_never_produces_a_request(source, damage):
    context, contract, scope = source
    if damage == "absent":
        context = replace(context, trajectory=None)
    elif damage == "ref":
        context = replace(context, trajectory_evidence_ref=context.trajectory_evidence_ref.model_copy(update={"trajectory_digest": "sha256:" + "f" * 64}))
    elif damage == "scope":
        context = replace(context, trajectory=context.trajectory.model_copy(update={"scope": scope.model_copy(update={"attempt_id": "sibling"})}))
    elif damage == "artifact_input":
        context = replace(context, trajectory_input=None)
    else:
        contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {"content_policy_version": None, "string_policies": None})
    with pytest.raises(ValueError, match="JUDGE_INPUT_"):
        request((context, contract, scope))


@pytest.mark.parametrize("budget", [{"max_steps": 1}, {"max_input_bytes": 256}])
def test_over_budget_input_is_unknown_not_truncated(source, budget):
    with pytest.raises(ValueError, match="JUDGE_INPUT_BUDGET_EXCEEDED"):
        request(source, frozen=definition(**budget))


def test_foreign_case_criteria_cannot_be_reused(source):
    value = criteria(source)
    foreign = replace(value, scope=source[2].model_copy(update={"dataset_item_id": "sibling"}))
    with pytest.raises(ValueError, match="JUDGE_CRITERIA_SCOPE_INVALID"):
        request(source, approved=foreign)


def test_mutated_criteria_or_evidence_are_revalidated(source):
    value = criteria(source)
    damaged = replace(value, payload_json='{"intent":"escalate"}')
    with pytest.raises(ValueError, match="JUDGE_CRITERIA_DIGEST_INVALID"):
        request(source, approved=damaged)
    source[0].trajectory.steps[1].arguments_redacted["private"] = "synthetic-secret"
    with pytest.raises(ValueError, match="JUDGE_INPUT_EVIDENCE_INVALID"):
        request(source)


def test_required_capability_missing_blocks_judge_before_provider(source):
    with pytest.raises(ValueError, match="JUDGE_INPUT_CAPABILITY_UNAVAILABLE"):
        request(source, frozen=definition(judge_kind="wasted_stage", required_capabilities=["timing"]))


@pytest.mark.parametrize("damage", [None, "missing", "corrupt", "criteria_substitution", "unsafe_expected", "missing_expected"])
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_preparer_loads_owned_artifact_from_real_checkpoint_transaction(request, tmp_path, backend, damage):
    from copy import deepcopy
    from types import SimpleNamespace

    from app.db_models import (
        AgentVersionRecord,
        ExecutionCheckpointRecord,
        ExperimentLaunchRecord,
        TrajectoryEvidenceArtifactRecord,
    )
    from app.evaluator_binding import canonical_digest, manifest_measurement_digest
    from app.execution_checkpoint import binding_provenance_from_manifest
    from app.trajectory.judge_input import adjudicate_judge_criteria, prepare_judge_request
    from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract

    proxy = SimpleNamespace(param=backend, getfixturevalue=request.getfixturevalue)
    setup = frozen_input_context.__wrapped__(proxy, tmp_path)
    db, scope, contract, started_at = next(setup)
    try:
        contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
            "content_policy_version": "pii-v1", "string_policies": {"$.customer_id": "identifier", "$.intent": "identifier"}})
        with db.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            manifest = deepcopy(launch.manifest)
            cp = session.get(ExecutionCheckpointRecord, "checkpoint")
            # 两处在冻结前写入同一份真实 Dataset 条件；不能只修测试传入的 criteria。
            cp.expected_output = {"intent": "Bearer synthetic-secret" if damage == "unsafe_expected" else "balance"}
            if damage == "missing_expected":
                cp.expected_output = {}
            manifest["dataset"]["items"][0]["expected_output"] = cp.expected_output
            manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract), session.get(AgentVersionRecord, launch.agent_version_id))
            manifest["measurement_digest"] = manifest_measurement_digest(manifest)
            manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
            launch.manifest = manifest
            cp.manifest_digest, cp.binding_provenance = canonical_digest(manifest), binding_provenance_from_manifest(manifest)
        acquired = ready_input.__wrapped__((db, scope, contract, started_at))
        approved = adjudicate_judge_criteria({"intent": "escalate" if damage == "criteria_substitution" else "balance"},
            policy=definition().criteria_policy, scope=scope, limits=contract.limits)
        if damage in {"missing", "corrupt"}:
            with db.get_session() as session:
                artifact = session.get(TrajectoryEvidenceArtifactRecord, acquired.evidence_ref.artifact_id)
                if damage == "missing":
                    session.delete(artifact)
                else:
                    artifact.payload_json = "{}"
        with db.get_session() as session:
            if damage:
                expected_reason = {"criteria_substitution": "JUDGE_CRITERIA_SOURCE_MISMATCH",
                    "unsafe_expected": "JUDGE_CRITERIA_UNAVAILABLE", "missing_expected": "JUDGE_CRITERIA_UNAVAILABLE"}.get(
                        damage, "JUDGE_INPUT_EVIDENCE_UNAVAILABLE")
                with pytest.raises(ValueError, match=expected_reason) as error:
                    prepare_judge_request(session, item_id=scope.item_execution_id, generation=scope.dispatch_generation,
                        manifest=manifest, definition=definition(), approved_criteria=approved)
                assert "synthetic-secret" not in str(error.value)
            else:
                value = prepare_judge_request(session, item_id=scope.item_execution_id, generation=scope.dispatch_generation,
                    manifest=manifest, definition=definition(), approved_criteria=approved)
                assert value.evidence_ref == acquired.evidence_ref and value.scope == scope
                assert "synthetic-secret" not in value.messages_json
    finally:
        setup.close()
