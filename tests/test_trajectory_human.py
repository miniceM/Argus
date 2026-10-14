"""签名 authority 接入；合成来源不计作真实 Reader/HUMAN 准入。"""

import base64

import pytest
from app.evaluator_binding import EvaluatorBinding
from app.trajectory.codec import canonical_dumps
from app.trajectory.contract import SourceProfile, TrajectoryContract
from app.trajectory.human import DOMAIN
from app.trajectory.normalizer import normalize_observations
from app.trajectory.rules import evaluate_trajectory_rule
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_trajectory_arguments import governed_contract
from test_trajectory_assembler import assemble, prepared
from test_trajectory_contract import version
from test_trajectory_manifest import context as _launch_context
from test_trajectory_rules import assert_measured, assert_unknown, context

launch_context = _launch_context
PARAMS = {"rule": "require_human_escalation", "scope": "ROOT_SUBTREE", "workflow_scope_id": None, "authority_id": None}


def inputs(*, human=True, inventory=True, inventory_ids=None):
    rows, _, scope = prepared()
    original_root = scope.root_observation_id
    root = "a" * 16
    scope = scope.model_copy(update={"root_observation_id": root})
    for row in rows:
        if row["id"] == original_root:
            row["id"] = root
        if row.get("parentObservationId") == original_root:
            row["parentObservationId"] = root
    key = Ed25519PrivateKey.generate()
    policy = {"authority_id": "isolated-approval-system-v1", "algorithm": "Ed25519",
        "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
    payload = governed_contract().model_dump()
    for profile in payload["source_profile_set"]:
        profile["human_authority_id"] = policy["authority_id"]
        profile["human_authority"] = policy
        profile["capabilities"]["human_events"] = "COMPLETE"
        profile["fields"]["human_receipt"] = [["metadata", "attributes.approval.receipt"]]
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    workflow = rows[1]["metadata"]["attributes.workflow.scope_id"]
    def authority_row(kind, identity, event_id=None):
        body = {"schema_version": "human-authority-1.0", "authority_id": policy["authority_id"], "kind": kind,
            "trace_id": scope.trace_id, "workflow_scope_id": workflow, "observation_id": identity, "root_observation_id": root}
        if kind == "ESCALATION":
            body.update(event_id=event_id, outcome="APPROVED")
        else:
            body.update(event_ids=inventory_ids if inventory_ids is not None else ([f"{5:032x}"] if human else []), scope_closed=True)
        receipt = {"body": body, "signature": base64.b64encode(key.sign(DOMAIN + canonical_dumps(body).encode())).decode()}
        return {"id": identity, "traceId": scope.trace_id, "parentObservationId": root, "type": "SPAN",
            "name": "approval.decision" if kind == "ESCALATION" else "approval.inventory", "input": None, "output": None,
            "startTime": "2026-10-13T04:00:05Z", "endTime": "2026-10-13T04:00:05.500000Z",
            "metadata": {"attributes.workflow.scope_id": workflow, "attributes.approval.receipt": canonical_dumps(receipt),
                **({"attributes.workflow.event_id": event_id, "attributes.workflow.sequence": 5} if event_id else {})}}
    if human:
        rows.insert(-1, authority_row("ESCALATION", "b" * 16, f"{5:032x}"))
        rows[-1]["metadata"].update({"attributes.workflow.sequence": 6, "attributes.workflow.event_id": f"{6:032x}",
            "attributes.workflow.event_count": 6, "attributes.workflow.final_sequence": 6,
            "attributes.workflow.event_ids": [f"{i:032x}" for i in range(1, 7)]})
        rows[-1].update(startTime="2026-10-13T04:00:06Z", endTime="2026-10-13T04:00:06.500000Z")
    if inventory:
        rows.append(authority_row("INVENTORY", "c" * 16))
    return rows, contract, scope


def binding(params):
    return EvaluatorBinding.from_payload({"id": "trajectory.require_human_escalation", "version": "1.0.0",
        "binding_id": "synthetic-human", "scope": "item", "result_type": "boolean",
        "definition_digest": "sha256:" + "a" * 64, "params": params,
        "input_contract": {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "human-events-1.0", "rule_id": "trajectory.require_human_escalation",
            "required_capabilities": ["human_events"]}, "output_contract": {"type": "boolean"}})


def run(value, contract, **changes):
    params = PARAMS | changes
    return evaluate_trajectory_rule(context(value), params, binding=binding(params), manifest_schema_version="1.3",
        authority_policy=contract.source_profile.human_authority)


def test_signed_authority_becomes_human_without_importing_personal_fields():
    rows, contract, scope = inputs()
    value = assemble(rows, contract, scope)
    human = next(step for step in value.steps if step.type == "HUMAN")
    assert human.step_id == "b" * 16 and human.phase == "EVENT" and human.tool_identity is None
    assert human.arguments_redacted is None and human.argument_visibility == {}
    assert value.completeness.capabilities["human_events"] == "COMPLETE"
    result = run(value, contract)
    assert_measured(result, True, "HUMAN_ESCALATION_OBSERVED")
    assert result.evidence["evidence_step_ids"] == [human.step_id]


def test_signed_empty_closed_inventory_is_negative_evidence():
    rows, contract, scope = inputs(human=False)
    value = assemble(rows, contract, scope)
    assert value.completeness.capabilities["human_events"] == "COMPLETE"
    assert_measured(run(value, contract), False, "HUMAN_ESCALATION_ABSENT")


@pytest.mark.parametrize("human", [True, False])
def test_signed_human_artifact_passes_storage_validation(human):
    from app.trajectory.evidence import _validate
    rows, contract, scope = inputs(human=human)
    value = assemble(rows, contract, scope)
    _validate(value, contract, contract.source_profile)


@pytest.mark.parametrize("change", ["signature", "name", "extra", "causal", "scope"])
def test_storage_revalidates_signed_protocol_before_allowing_summary(change):
    from app.trajectory.assembler import trajectory_digest
    from app.trajectory.evidence import TrajectoryEvidenceUnavailableError, _validate
    from app.trajectory.schema import StandardTrajectory
    rows, contract, scope = inputs()
    value = assemble(rows, contract, scope)
    payload = value.model_dump()
    step = next(step for step in payload["steps"] if step["type"] == "HUMAN")
    if change == "signature":
        step["result_summary"]["human_receipt"]["signature"] = "A" * 86 + "=="
    elif change == "name":
        step["raw_name"] = "personal free text"
    elif change == "extra":
        step["result_summary"]["human_receipt"]["body"]["note"] = "private note"
    elif change == "causal":
        step["causal_predecessor_ids"] = [scope.root_observation_id]
    else:
        step["workflow_scope_id"] = "d" * 32
    payload["provenance"]["trajectory_digest"] = trajectory_digest(StandardTrajectory.model_validate(payload))
    with pytest.raises(TrajectoryEvidenceUnavailableError, match="REDACTION_INVALID"):
        _validate(StandardTrajectory.model_validate(payload), contract, contract.source_profile)


@pytest.mark.parametrize("field", ["arguments_redacted", "result_summary"])
def test_storage_reapplies_frozen_content_policy_to_safe_aliases(field):
    from app.trajectory.assembler import trajectory_digest
    from app.trajectory.evidence import TrajectoryEvidenceUnavailableError, _validate
    from app.trajectory.schema import StandardTrajectory
    rows, contract, scope = inputs()
    payload = assemble(rows, contract, scope).model_dump()
    step = next(step for step in payload["steps"] if step["type"] == "TOOL")
    if field == "arguments_redacted":
        step[field] = {"customer_id": "4111111111111111"}
        step["argument_visibility"] = {"$.customer_id": "AVAILABLE"}
    else:
        step[field] = {"balance": "4111111111111111"}
    payload["provenance"]["trajectory_digest"] = trajectory_digest(StandardTrajectory.model_validate(payload))
    with pytest.raises(TrajectoryEvidenceUnavailableError, match="REDACTION_INVALID"):
        _validate(StandardTrajectory.model_validate(payload), contract, contract.source_profile)


@pytest.mark.parametrize("human", [True, False])
def test_missing_inventory_cannot_prove_absence_but_observed_event_remains_true(human):
    rows, contract, scope = inputs(human=human, inventory=False)
    value = assemble(rows, contract, scope)
    assert value.completeness.capabilities["human_events"] != "COMPLETE"
    result = run(value, contract)
    assert_measured(result, True) if human else assert_unknown(result)


@pytest.mark.parametrize("ids", [[], ["d" * 32]])
def test_closed_inventory_must_match_observed_human_events(ids):
    rows, contract, scope = inputs(inventory_ids=ids)
    value = assemble(rows, contract, scope)
    assert value.completeness.capabilities["human_events"] != "COMPLETE"
    assert "HUMAN_INVENTORY_MISMATCH" in value.completeness.warnings
    assert_measured(run(value, contract), True)


def test_wrong_signature_rejects_the_event_and_does_not_create_human_evidence():
    rows, contract, scope = inputs()
    row = next(row for row in rows if row["id"] == "b" * 16)
    from app.trajectory.codec import canonical_loads
    receipt = canonical_loads(row["metadata"]["attributes.approval.receipt"])
    receipt["body"]["outcome"] = "DECLINED"
    row["metadata"]["attributes.approval.receipt"] = canonical_dumps(receipt)
    normalized = normalize_observations(rows, contract, scope=scope)
    rejected = next(record.observation for record in normalized.records if record.observation_id == row["id"])
    assert rejected.step is None and rejected.warnings == ["HUMAN_RECEIPT_SIGNATURE_INVALID"]
    value = assemble(rows, contract, scope)
    assert not any(step.type == "HUMAN" for step in value.steps)
    assert_unknown(run(value, contract))


def test_event_field_cannot_disagree_with_the_signed_event_identity():
    rows, contract, scope = inputs()
    next(row for row in rows if row["id"] == "b" * 16)["metadata"]["attributes.workflow.event_id"] = "e" * 32
    normalized = normalize_observations(rows, contract, scope=scope)
    rejected = next(record.observation for record in normalized.records if record.observation_id == "b" * 16)
    assert rejected.step is None and rejected.warnings == ["HUMAN_RECEIPT_SCOPE_MISMATCH"]


def test_generic_type_mapping_is_still_forbidden_and_unsigned_capability_is_rejected():
    rows, contract, scope = inputs(human=False)
    profile = contract.source_profile.model_dump()
    with pytest.raises(ValueError, match="TRAJECTORY_HUMAN_REQUIRES_VERIFIED_AUTHORITY_EVENT"):
        SourceProfile.model_validate(profile | {"type_map": profile["type_map"] | {"SPAN": "HUMAN"}})
    with pytest.raises(ValueError, match="TRAJECTORY_HUMAN_POLICY_REQUIRED"):
        SourceProfile.model_validate(profile | {"human_authority": None})


def test_old_profile_serialization_and_digest_are_unchanged_without_new_policy():
    profile = governed_contract().source_profile
    assert "human_authority" not in profile.model_dump()
    raw = profile.model_dump()
    assert SourceProfile.model_validate(raw).model_dump() == raw


@pytest.mark.parametrize("human,expected", [(True, True), (False, False)])
def test_frozen_dispatcher_uses_frozen_authority_policy_and_preserves_synthetic_diagnostic(launch_context, human, expected):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_manifest import create
    rows, contract, scope = inputs(human=human)
    value = assemble(rows, contract, scope)
    launch = create(launch_context, contract=contract, evaluator_selections=[{
        "id": "trajectory.require_human_escalation", "version": "1.0.0", "params": PARAMS}])
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=context(value))
    assert_measured(result.typed_results[0], expected)
    assert result.required_evidence_status != "COMPLETE"


def test_mutated_trajectory_human_without_a_valid_receipt_never_passes():
    rows, contract, scope = inputs()
    from test_trajectory_rules import reseal
    value = assemble(rows, contract, scope)
    changed = reseal(value, lambda p: next(step for step in p["steps"] if step["type"] == "HUMAN").update(result_summary={}))
    assert_unknown(run(changed, contract))


def test_sequence_descriptor_cannot_use_unsigned_human_as_a_proven_step():
    from dataclasses import replace

    from test_trajectory_rules import reseal
    rows, contract, scope = inputs()
    value = assemble(rows, contract, scope)
    params = {"rule": "required_step_sequence", "scope": "ROOT_SUBTREE", "workflow_scope_id": None,
        "steps": [{"type": "HUMAN", "tool_identity": None, "phase": "EVENT"}]}
    bound = replace(binding(PARAMS), evaluator_id="trajectory.required_step_sequence", params=params,
        input_contract={"input_kind": "trajectory", "trajectory_schema_version": "1.0", "algorithm_version": "step-sequence-1.0",
            "rule_id": "trajectory.required_step_sequence", "required_capabilities": ["ordering", "human_events"]})
    assert_measured(evaluate_trajectory_rule(context(value), params, binding=bound, manifest_schema_version="1.3",
        authority_policy=contract.source_profile.human_authority), True)
    changed = reseal(value, lambda p: next(step for step in p["steps"] if step["type"] == "HUMAN").update(result_summary={}))
    assert_unknown(evaluate_trajectory_rule(context(changed), params, binding=bound, manifest_schema_version="1.3",
        authority_policy=contract.source_profile.human_authority))


def test_frozen_sequence_can_consume_verified_human_without_copying_its_policy_into_params(launch_context):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_manifest import create
    rows, contract, scope = inputs()
    params = {"rule": "required_step_sequence", "steps": [{"type": "HUMAN", "phase": "EVENT"}]}
    launch = create(launch_context, contract=contract, evaluator_selections=[{
        "id": "trajectory.required_step_sequence", "version": "1.0.0", "params": params}])
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=context(assemble(rows, contract, scope)))
    assert_measured(result.typed_results[0], True)
    assert "public_key" not in str(launch.manifest["evaluators"][0]["params"])


def test_unintegrated_human_capability_is_unknown_even_if_output_claims_escalation(launch_context):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_manifest import create
    rows, _, scope = prepared()
    contract = governed_contract()
    launch = create(launch_context, contract=contract, evaluator_selections=[{
        "id": "trajectory.require_human_escalation", "version": "1.0.0", "params": PARAMS}])
    value = assemble(rows, contract, scope)
    result = evaluate_frozen_item(launch.manifest, output={"escalated": True, "tool_calls": [{"name": "escalate"}]},
        expected_output={}, context=context(value))
    assert_unknown(result.typed_results[0])
    assert result.typed_results[0].evidence["reason_code"] == "HUMAN_COVERAGE_UNPROVEN"


def test_observed_escalate_tool_is_not_a_human_event():
    rows, contract, scope = inputs(human=False)
    payload = contract.model_dump()
    for entry in payload["tool_mapping"]["entries"]:
        entry["tool_identity"] = "banking.escalate"
    configured = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    value = assemble(rows, configured, scope)
    assert any(step.type == "TOOL" and step.tool_identity == "banking.escalate" for step in value.steps)
    assert not any(step.type == "HUMAN" for step in value.steps)
    assert_measured(run(value, configured), False)


def test_freeze_checks_selected_profile_not_other_frameworks_authority(launch_context):
    from app.evaluator_binding import EvaluatorBindingError
    from test_trajectory_manifest import create
    _, contract, _ = inputs()
    payload = contract.model_dump()
    for profile in payload["source_profile_set"]:
        if profile["profile_id"] == "pydantic-ai-v1":
            profile["human_authority_id"] = "other-issuer"
            profile["human_authority"]["authority_id"] = "other-issuer"
    configured = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    def freeze(identity):
        return create(launch_context, contract=configured, evaluator_selections=[{
            "id": "trajectory.require_human_escalation", "version": "1.0.0", "params": PARAMS | {"authority_id": identity}}])
    assert freeze("isolated-approval-system-v1").manifest["schema_version"] == "1.3"
    with pytest.raises(EvaluatorBindingError, match="TRAJECTORY_HUMAN_AUTHORITY_MISMATCH"):
        freeze("other-issuer")


def test_key_rotation_changes_future_measurement_but_not_a_frozen_evaluation(launch_context):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_manifest import create
    rows, contract, scope = inputs()
    value = assemble(rows, contract, scope)
    selection = [{"id": "trajectory.require_human_escalation", "version": "1.0.0", "params": PARAMS}]
    launch = create(launch_context, contract=contract, evaluator_selections=selection)
    payload = contract.model_dump()
    public_key = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    for profile in payload["source_profile_set"]:
        profile["human_authority"]["public_key"] = public_key
    rotated = TrajectoryContract.model_validate(payload).bind_source_version(version("native-otel-v1"))
    candidate = create(launch_context, contract=rotated, evaluator_selections=selection)
    assert launch.manifest["measurement_digest"] != candidate.manifest["measurement_digest"]
    assert contract.tool_mapping.digest == rotated.tool_mapping.digest
    assert_measured(evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=context(value)).typed_results[0], True)
    assert_unknown(run(value, rotated))
