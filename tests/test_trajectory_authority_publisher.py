"""独立审批 publisher 的通用签名协议；不启动 Langfuse，不宣称真实准入。"""

from pathlib import Path

import pytest
from app.trajectory.human import HumanReceiptError, verify_human_receipt


@pytest.fixture
def issuer(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "fixtures" / "trajectory_agents"))
    import approval_system
    return approval_system.ApprovalAuthority()


@pytest.mark.parametrize("kind", ["ESCALATION", "INVENTORY"])
def test_standalone_issuer_receipts_match_consumer_protocol(issuer, kind):
    scope = {"trace_id": "1" * 32, "workflow_scope_id": "2" * 32,
        "observation_id": "3" * 16, "root_observation_id": "4" * 16}
    receipt = issuer.receipt(kind, **scope, **({"event_id": "5" * 32} if kind == "ESCALATION" else {"event_ids": []}))
    checked = verify_human_receipt(issuer.public_policy(), receipt, **scope)
    assert checked.body.kind == kind
    assert checked.body.scope_closed is True if kind == "INVENTORY" else checked.body.outcome == "APPROVED"
    assert set(issuer.public_policy()) == {"authority_id", "algorithm", "public_key"}


def test_separate_process_identity_keys_cannot_sign_for_each_other(issuer):
    import approval_system
    other = approval_system.ApprovalAuthority()
    scope = {"trace_id": "1" * 32, "workflow_scope_id": "2" * 32,
        "observation_id": "3" * 16, "root_observation_id": "4" * 16}
    receipt = other.receipt("ESCALATION", **scope, event_id="5" * 32)
    with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_SIGNATURE_INVALID"):
        verify_human_receipt(issuer.public_policy(), receipt, **scope)


def test_issuer_refuses_unknown_kind_instead_of_signing_arbitrary_metadata(issuer):
    with pytest.raises(ValueError, match="AUTHORITY_RECEIPT_INVALID"):
        issuer.receipt("CUSTOM", trace_id="1" * 32, workflow_scope_id="2" * 32,
            observation_id="3" * 16, root_observation_id="4" * 16)


def test_issuer_refuses_zero_event_identity(issuer):
    with pytest.raises(ValueError, match="AUTHORITY_RECEIPT_INVALID"):
        issuer.receipt("ESCALATION", trace_id="1" * 32, workflow_scope_id="2" * 32,
            observation_id="3" * 16, root_observation_id="4" * 16, event_id="0" * 32)
