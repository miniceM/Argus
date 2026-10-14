"""冻结 authority 公钥验证有限回执；不把 Agent metadata 当作来源授权。"""

import base64
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, ValidationError, field_validator, model_validator

from .codec import canonical_dumps, canonical_loads
from .schema import _ContractModel

DOMAIN = b"Argus/HumanAuthority/1.0\x00"
MAX_RECEIPT_BYTES = 65536
AuthorityId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$", max_length=64)]
Opaque32 = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)]
ObservationId = Annotated[str, Field(pattern=r"^[0-9a-f]{16}$", min_length=16, max_length=16)]


class HumanReceiptError(ValueError):
    """有限 reason code；不包含输入、上游异常或签名原文。"""


def _decode(value, size):
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != size or base64.b64encode(decoded).decode() != value:
        raise ValueError("HUMAN_ENCODING_INVALID")
    return decoded


class HumanAuthorityPolicy(_ContractModel):
    authority_id: AuthorityId
    algorithm: Literal["Ed25519"]
    public_key: str = Field(min_length=44, max_length=44)

    @field_validator("public_key")
    @classmethod
    def canonical_public_key(cls, value):
        _decode(value, 32)
        return value


class HumanReceiptBody(_ContractModel):
    schema_version: Literal["human-authority-1.0"]
    authority_id: AuthorityId
    kind: Literal["ESCALATION", "INVENTORY"]
    trace_id: Opaque32
    workflow_scope_id: Opaque32
    observation_id: ObservationId
    root_observation_id: ObservationId
    event_id: Opaque32 | None = None
    outcome: Literal["APPROVED", "DECLINED"] | None = None
    event_ids: list[Opaque32] | None = Field(default=None, max_length=1000)
    scope_closed: Literal[True] | None = None

    @field_validator("scope_closed", mode="before")
    @classmethod
    def strict_closed_marker(cls, value):
        if value is not None and value is not True:
            raise ValueError("HUMAN_RECEIPT_INVALID")
        return value

    @field_validator("trace_id", "workflow_scope_id", "observation_id", "root_observation_id", "event_id")
    @classmethod
    def nonzero_identity(cls, value):
        if value is not None and set(value) == {"0"}:
            raise ValueError("HUMAN_RECEIPT_INVALID")
        return value

    @model_validator(mode="after")
    def disjoint_semantics(self):
        if self.kind == "ESCALATION":
            if self.event_id is None or self.outcome is None or self.event_ids is not None or self.scope_closed is not None:
                raise ValueError("HUMAN_RECEIPT_INVALID")
        elif (self.event_id is not None or self.outcome is not None or self.event_ids is None or self.scope_closed is not True
            or len(set(self.event_ids)) != len(self.event_ids) or any(set(identity) == {"0"} for identity in self.event_ids)):
            raise ValueError("HUMAN_RECEIPT_INVALID")
        return self


class HumanReceipt(_ContractModel):
    body: HumanReceiptBody
    signature: str = Field(min_length=88, max_length=88)

    @field_validator("signature")
    @classmethod
    def canonical_signature(cls, value):
        _decode(value, 64)
        return value


def receipt_message(body):
    """消费方构造固定域消息；authority 按协议独立签名，无需导入 Argus SDK。"""
    checked = HumanReceiptBody.model_validate(body.model_dump() if isinstance(body, HumanReceiptBody) else body)
    return DOMAIN + canonical_dumps(checked.model_dump(exclude_none=True), max_bytes=MAX_RECEIPT_BYTES).encode()


def verify_human_receipt(policy, payload, *, trace_id, workflow_scope_id, observation_id, root_observation_id):
    """只按调用方冻结公钥验签；回执不携带可替换公钥，不回源、不记录原文。"""
    try:
        authority = HumanAuthorityPolicy.model_validate(policy.model_dump() if isinstance(policy, HumanAuthorityPolicy) else policy)
    except (ValueError, TypeError):
        raise HumanReceiptError("HUMAN_AUTHORITY_POLICY_INVALID") from None
    try:
        raw = canonical_loads(payload, max_bytes=MAX_RECEIPT_BYTES) if type(payload) is str else payload
        if isinstance(raw, HumanReceipt):
            raw = raw.model_dump()
        receipt = HumanReceipt.model_validate(raw)
        # 先拒绝额外字段与超长列表，不序列化未获准的上游容器。
        canonical_dumps(receipt.model_dump(exclude_none=True), max_bytes=MAX_RECEIPT_BYTES)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise HumanReceiptError("HUMAN_RECEIPT_INVALID") from None
    if receipt.body.authority_id != authority.authority_id:
        raise HumanReceiptError("HUMAN_RECEIPT_AUTHORITY_MISMATCH")
    if (receipt.body.trace_id, receipt.body.workflow_scope_id, receipt.body.observation_id, receipt.body.root_observation_id) != (
        trace_id, workflow_scope_id, observation_id, root_observation_id):
        raise HumanReceiptError("HUMAN_RECEIPT_SCOPE_MISMATCH")
    try:
        Ed25519PublicKey.from_public_bytes(_decode(authority.public_key, 32)).verify(
            _decode(receipt.signature, 64), receipt_message(receipt.body))
    except (InvalidSignature, UnsupportedAlgorithm, ValueError):
        raise HumanReceiptError("HUMAN_RECEIPT_SIGNATURE_INVALID") from None
    return receipt


def human_evidence(steps, scope, policy):
    """重验冻结 Step 的回执与形状；只返回 scope/event/Step 引用，不复制参数。"""
    events, inventories, invalid = {}, {}, set()
    for step in steps:
        summary = step.result_summary
        if step.type != "HUMAN" and not (type(summary) is dict and "human_receipt" in summary):
            continue
        workflow = step.workflow_scope_id
        try:
            if (type(summary) is not dict or set(summary) != {"human_receipt"} or step.phase != "EVENT"
                or step.tool_identity is not None or step.arguments_redacted is not None or step.argument_visibility):
                raise HumanReceiptError("HUMAN_RECEIPT_INVALID")
            receipt = verify_human_receipt(policy, summary["human_receipt"], trace_id=scope.trace_id,
                workflow_scope_id=workflow, observation_id=step.step_id, root_observation_id=scope.root_observation_id)
            if receipt.body.kind == "ESCALATION":
                if step.type != "HUMAN" or step.status != ("FAILED" if receipt.body.outcome == "DECLINED" else "SUCCEEDED"):
                    raise HumanReceiptError("HUMAN_RECEIPT_INVALID")
                group = events.setdefault(workflow, {})
                if receipt.body.event_id in group:
                    invalid.add(workflow)
                group[receipt.body.event_id] = step.step_id
            else:
                if step.type != "CUSTOM" or step.status != "SUCCEEDED":
                    raise HumanReceiptError("HUMAN_RECEIPT_INVALID")
                inventories.setdefault(workflow, []).append(set(receipt.body.event_ids))
        except HumanReceiptError:
            invalid.add(workflow)
    return events, inventories, invalid


def closed_human_workflows(steps, scope, policy):
    events, inventories, invalid = human_evidence(steps, scope, policy)
    closed = {workflow for workflow, declarations in inventories.items() if workflow is not None
        and workflow not in invalid and len(declarations) == 1 and declarations[0] == set(events.get(workflow, {}))}
    return events, closed, invalid
