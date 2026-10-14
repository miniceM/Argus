"""Authority 签名与重放边界；合成签名不等于真实人员或 Reader 准入。"""

import base64
from copy import deepcopy

import pytest
from app.trajectory.codec import canonical_dumps
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DOMAIN = b"Argus/HumanAuthority/1.0\x00"
BODY = {"schema_version": "human-authority-1.0", "authority_id": "isolated-approval-system-v1",
    "kind": "ESCALATION", "trace_id": "1" * 32, "workflow_scope_id": "2" * 32,
    "observation_id": "3" * 16, "root_observation_id": "a" * 16, "event_id": "4" * 32, "outcome": "APPROVED"}


def signed(body=None):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    payload = deepcopy(body or BODY)
    signature = base64.b64encode(key.sign(DOMAIN + canonical_dumps(payload).encode())).decode()
    return {"authority_id": BODY["authority_id"], "algorithm": "Ed25519", "public_key": public}, {"body": payload, "signature": signature}


def verify(policy, payload, **changes):
    from app.trajectory.human import verify_human_receipt
    return verify_human_receipt(policy, payload, **({"trace_id": BODY["trace_id"], "workflow_scope_id": BODY["workflow_scope_id"],
        "observation_id": BODY["observation_id"], "root_observation_id": BODY["root_observation_id"]} | changes))


def test_valid_signed_event_can_be_verified_from_exact_json_without_sensitive_fields():
    policy, payload = signed()
    receipt = verify(policy, canonical_dumps(payload))
    assert receipt.body.kind == "ESCALATION" and receipt.body.event_id == BODY["event_id"]
    assert receipt.body.outcome == "APPROVED"
    assert set(receipt.body.model_dump(exclude_none=True)) == set(BODY)


@pytest.mark.parametrize("field,value", [("trace_id", "5" * 32), ("workflow_scope_id", "5" * 32),
    ("observation_id", "5" * 16), ("root_observation_id", "5" * 16)])
def test_valid_signature_cannot_be_replayed_in_another_scope(field, value):
    from app.trajectory.human import HumanReceiptError
    policy, payload = signed()
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_SCOPE_MISMATCH"):
        verify(policy, payload, **{field: value})


@pytest.mark.parametrize("field,value", [("event_id", "5" * 32), ("outcome", "DECLINED"), ("authority_id", "forged-service")])
def test_metadata_tampering_cannot_reuse_a_signature(field, value):
    from app.trajectory.human import HumanReceiptError
    policy, payload = signed()
    payload["body"][field] = value
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_(SIGNATURE_INVALID|AUTHORITY_MISMATCH)"):
        verify(policy, payload)


def test_agent_generated_key_and_unsigned_self_report_do_not_prove_authority():
    from app.trajectory.human import HumanReceiptError
    policy, _ = signed()
    _, forged = signed()
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_SIGNATURE_INVALID"):
        verify(policy, forged)
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_INVALID"):
        verify(policy, {"body": BODY, "service_name": BODY["authority_id"]})


@pytest.mark.parametrize("extra", ["employee_name", "account", "note", "conversation", "tool_calls"])
def test_even_authority_signed_unapproved_fields_are_rejected(extra):
    from app.trajectory.human import HumanReceiptError
    policy, payload = signed(BODY | {extra: "synthetic-sensitive-sentinel"})
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_INVALID") as failure:
        verify(policy, payload)
    assert "synthetic-sensitive-sentinel" not in str(failure.value)


def test_signed_closed_empty_inventory_proves_only_its_frozen_scope():
    body = {k: v for k, v in BODY.items() if k not in {"event_id", "outcome"}}
    body.update(kind="INVENTORY", event_ids=[], scope_closed=True)
    policy, payload = signed(body)
    receipt = verify(policy, payload)
    assert receipt.body.event_ids == [] and receipt.body.scope_closed is True


def test_integer_closed_marker_cannot_reuse_a_signature_for_boolean_true():
    from app.trajectory.human import HumanReceiptError
    body = {k: v for k, v in BODY.items() if k not in {"event_id", "outcome"}}
    body.update(kind="INVENTORY", event_ids=[], scope_closed=True)
    policy, payload = signed(body)
    payload["body"]["scope_closed"] = 1
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_INVALID"):
        verify(policy, payload)


@pytest.mark.parametrize("changes", [{"scope_closed": False}, {"event_ids": ["4" * 32, "4" * 32]},
    {"event_ids": ["4" * 32] * 1001}, {"event_id": "4" * 32}, {"outcome": "APPROVED"}])
def test_inventory_cannot_claim_open_duplicate_overflow_or_mixed_event_semantics(changes):
    from app.trajectory.human import HumanReceiptError
    body = {k: v for k, v in BODY.items() if k not in {"event_id", "outcome"}}
    body.update(kind="INVENTORY", event_ids=[], scope_closed=True)
    policy, payload = signed(body | changes)
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_INVALID"):
        verify(policy, payload)


@pytest.mark.parametrize("changes", [{"event_id": None}, {"scope_closed": True}, {"event_ids": []},
    {"trace_id": "0" * 32}, {"workflow_scope_id": "synthetic-person-name"}, {"observation_id": "3" * 32}])
def test_event_receipt_ids_and_shape_are_strict(changes):
    from app.trajectory.human import HumanReceiptError
    policy, payload = signed(BODY | changes)
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_INVALID"):
        verify(policy, payload)


@pytest.mark.parametrize("change", [{"algorithm": "none"}, {"public_key": "bad"}, {"public_key": "A" * 44},
    {"authority_id": "name with spaces"}, {"private_key": "synthetic-secret"}])
def test_pinned_public_key_policy_is_finite_and_cannot_accept_a_private_key(change):
    from app.trajectory.human import HumanReceiptError
    policy, payload = signed()
    with pytest.raises(HumanReceiptError, match="HUMAN_AUTHORITY_POLICY_INVALID") as failure:
        verify(policy | change, payload)
    assert "synthetic-secret" not in str(failure.value)


@pytest.mark.parametrize("signature", ["bad", "A" * 88, "", "A" * 200])
def test_signature_encoding_is_strict_and_bounded(signature):
    from app.trajectory.human import HumanReceiptError
    policy, payload = signed()
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_INVALID"):
        verify(policy, payload | {"signature": signature})


def test_signature_has_domain_separation():
    from app.trajectory.human import HumanReceiptError
    key = Ed25519PrivateKey.generate()
    policy = {"authority_id": BODY["authority_id"], "algorithm": "Ed25519",
        "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
    payload = {"body": BODY, "signature": base64.b64encode(key.sign(canonical_dumps(BODY).encode())).decode()}
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_SIGNATURE_INVALID"):
        verify(policy, payload)
