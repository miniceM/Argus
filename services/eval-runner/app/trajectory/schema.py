"""StandardTrajectory v1 的受控类型；不负责证明来源、脱敏或因果。"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .codec import MAX_STRING_BYTES, validate_json_depth, validate_json_value
from .paths import compile_jsonpath

AcquisitionState = Literal["PENDING", "READY", "UNAVAILABLE"]
Completeness = Literal["OBSERVED_COMPLETE", "PARTIAL", "ROOT_ONLY"]
CapabilityCoverage = Literal["COMPLETE", "PARTIAL", "NONE"]
Capability = Literal["tool_executions", "tool_requests", "tool_arguments", "human_events", "ordering", "timing"]
StepType = Literal["AGENT", "LLM", "TOOL", "RETRIEVER", "GUARDRAIL", "HUMAN", "CUSTOM"]
StepPhase = Literal["REQUESTED", "EXECUTED", "BLOCKED", "EVENT"]
StepStatus = Literal["SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
ArgumentVisibility = Literal["AVAILABLE", "REDACTED", "MISSING", "INVALID"]
SchemaVersion = Literal["1.0"]
Identity = Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,64}$", max_length=64)]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$", min_length=71, max_length=71)]
ReasonCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$", max_length=64)]

type JsonValue = None | bool | int | Decimal | str | list[JsonValue] | dict[str, JsonValue]
BoundedJson = Annotated[JsonValue, BeforeValidator(validate_json_value)]
BoundedString = Annotated[str, BeforeValidator(validate_json_value), Field(max_length=MAX_STRING_BYTES)]


def _timestamp_input(value: Any) -> Any:
    if not isinstance(value, datetime) and not (type(value) is str and re.match(r"^\d{4}-\d{2}-\d{2}T", value)):
        raise ValueError("TRAJECTORY_TIMESTAMP_INVALID")
    return value


Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp_input), Field(strict=False)]


def _duration_input(value: Any) -> Decimal:
    if type(value) is int:
        value = Decimal(value)
    if type(value) is not Decimal:
        raise ValueError("TRAJECTORY_DURATION_INVALID")
    validate_json_value(value)
    if value < 0:
        raise ValueError("TRAJECTORY_DURATION_NEGATIVE")
    return value


Duration = Annotated[Decimal, BeforeValidator(_duration_input)]


class _ContractModel(BaseModel):
    # frozen 仅保护字段赋值；不承诺 dict/list 深冻结，持久化必须使用 insert-only 制品。
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, revalidate_instances="always"
    )

    @model_validator(mode="after")
    def bounded_document_depth(self):
        validate_json_depth(self.model_dump(mode="python"))
        return self


class TrajectoryScope(_ContractModel):
    launch_id: Identity
    item_execution_id: Identity
    dataset_item_id: Identity
    attempt_id: Identity
    dispatch_generation: Annotated[int, Field(ge=1)]
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)]
    root_observation_id: Identity

    @field_validator("trace_id")
    @classmethod
    def nonzero_trace(cls, value: str) -> str:
        if value == "0" * 32:
            raise ValueError("TRAJECTORY_TRACE_ID_ZERO")
        return value


class TrajectoryStep(_ContractModel):
    step_id: Identity
    observation_id: Identity
    parent_step_id: Identity | None
    index: Annotated[int, Field(ge=0)]
    type: StepType
    raw_name: BoundedString
    tool_identity: Identity | None
    phase: StepPhase
    status: StepStatus
    started_at: Timestamp | None
    ended_at: Timestamp | None
    duration_ms: Duration | None
    arguments_redacted: BoundedJson
    argument_visibility: dict[str, ArgumentVisibility]
    result_summary: BoundedJson
    error_code: ReasonCode | None
    causal_predecessor_ids: list[Identity] = Field(default_factory=list, max_length=1000)
    workflow_scope_id: BoundedString | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @field_validator("argument_visibility")
    @classmethod
    def static_paths(cls, value: dict[str, ArgumentVisibility]) -> dict[str, ArgumentVisibility]:
        if len(value) > 1000:
            raise ValueError("TRAJECTORY_ARGUMENT_PATH_LIMIT")
        for path in value:
            compile_jsonpath(path)
        return value

    @field_validator("causal_predecessor_ids")
    @classmethod
    def stable_references(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @model_validator(mode="after")
    def matching_step_identity(self):
        if self.step_id != self.observation_id:
            raise ValueError("TRAJECTORY_STEP_ID_MISMATCH")
        return self


class TrajectoryCompleteness(_ContractModel):
    status: Completeness
    capabilities: dict[Capability, CapabilityCoverage]
    warnings: list[ReasonCode] = Field(max_length=1000)

    @field_validator("capabilities")
    @classmethod
    def explicit_capabilities(cls, value: dict) -> dict:
        if set(value) != {"tool_executions", "tool_requests", "tool_arguments", "human_events", "ordering", "timing"}:
            raise ValueError("TRAJECTORY_CAPABILITIES_INCOMPLETE")
        return value

    @field_validator("warnings")
    @classmethod
    def stable_warnings(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class TrajectoryProvenance(_ContractModel):
    provider: Identity
    source_profile_id: Identity
    source_profile_version: Identity
    schema_version: SchemaVersion
    assembler_version: Identity
    tool_mapping_version: Identity
    tool_mapping_digest: Digest
    redaction_policy_version: Identity
    redaction_policy_digest: Digest
    trajectory_contract_digest: Digest
    trajectory_digest: Digest


class StandardTrajectory(_ContractModel):
    schema_version: SchemaVersion
    scope: TrajectoryScope
    input_redacted: BoundedJson
    output_redacted: BoundedJson
    completeness: TrajectoryCompleteness
    steps: list[TrajectoryStep] = Field(min_length=1, max_length=1000)
    provenance: TrajectoryProvenance

    @model_validator(mode="after")
    def unique_steps_and_root(self):
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)) or len({step.index for step in self.steps}) != len(self.steps):
            raise ValueError("TRAJECTORY_STEPS_DUPLICATE")
        if self.scope.root_observation_id not in ids:
            raise ValueError("TRAJECTORY_ROOT_MISSING")
        return self


class TrajectoryEvidenceRef(_ContractModel):
    artifact_id: Identity
    trajectory_digest: Digest
    trajectory_contract_digest: Digest
    schema_version: SchemaVersion


class TrajectoryAcquisition(_ContractModel):
    scope: TrajectoryScope
    state: AcquisitionState
    completeness: TrajectoryCompleteness | None
    evidence_ref: TrajectoryEvidenceRef | None
    reason_code: ReasonCode | None
    retryable: bool
    deadline_at: Timestamp

    @field_validator("deadline_at")
    @classmethod
    def utc_deadline(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def state_evidence_contract(self):
        if self.state == "READY":
            if self.completeness is None or self.evidence_ref is None or self.retryable:
                raise ValueError("TRAJECTORY_READY_EVIDENCE_REQUIRED")
        elif self.state == "PENDING":
            if self.evidence_ref is not None:
                raise ValueError("TRAJECTORY_PENDING_HAS_EVIDENCE")
        elif self.completeness is not None or self.evidence_ref is not None or self.reason_code is None:
            raise ValueError("TRAJECTORY_UNAVAILABLE_STATE_INVALID")
        return self
