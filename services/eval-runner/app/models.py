from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .security import validate_credential_ref, validate_endpoint_url


# ---------------------------------------------------------
# Backward Compatibility Models
# ---------------------------------------------------------
class ExperimentRequest(BaseModel):
    agent_id: str = "banking-agent"
    agent_version: str
    dataset_name: str = "banking-agent-regression"
    experiment_name: str | None = None
    max_concurrency: int = Field(default=4, ge=1, le=50)


class BootstrapResult(BaseModel):
    dataset_name: str
    dataset_id: str | None = None
    items_upserted: int


class ExperimentResult(BaseModel):
    launch_id: str
    agent_id: str
    agent_version: str
    experiment_name: str
    dataset_run_url: str | None = None
    result: dict[str, Any]


# ---------------------------------------------------------
# Enterprise Control Layer Domain DTOs (Zero URL Path Variables)
# ---------------------------------------------------------
class AgentCreateRequest(BaseModel):
    id: str = Field(..., description="Unique Agent identifier, e.g. banking-agent")
    name: str = Field(..., description="Human-readable name")
    description: str | None = Field(default=None, description="Detailed description")
    owner: str | None = Field(default=None, description="Team or owner identifier")


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    owner: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime






class AgentVersionSpecValidator(BaseModel):
    endpoint: str = Field(..., description="HTTP POST URL of the agent")
    protocol: str = Field(default="HTTP_JSON", description="Invocation protocol, currently HTTP_JSON only")
    method: str = Field(default="POST", description="HTTP method, currently POST only")
    request_mapping: dict[str, str] = Field(default_factory=dict, description="Dot-path field mapping")
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    credential_ref: str | None = Field(default=None, description="Reference to secret, e.g. env://NAME")
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    rate_limit_per_minute: int = Field(default=600, ge=1, le=10000)
    max_concurrency: int = Field(default=4, ge=1, le=50)
    trace_propagation: str = Field(default="W3C", description="Trace context propagation standard, W3C only")
    is_idempotent: bool = Field(default=False, description="Whether remote call has no side-effects and is safe to retry on read timeout")
    artifact_ref: str | None = Field(default=None, description="Commit SHA or image digest")
    environment: str | None = None
    metadata: dict[str, Any] | None = None


    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        if v != "HTTP_JSON":
            raise ValueError(f"Only HTTP_JSON protocol is supported, got: '{v}'")
        return v

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if v.upper() != "POST":
            raise ValueError(f"Only POST method is supported, got: '{v}'")
        return v.upper()

    @field_validator("trace_propagation")
    @classmethod
    def validate_trace_propagation(cls, v: str) -> str:
        if v != "W3C":
            raise ValueError(f"Only 'W3C' trace_propagation is currently supported, got: '{v}'")
        return v

    @field_validator("request_mapping", mode="before")
    @classmethod
    def validate_request_mapping(cls, v: Any) -> dict[str, str]:
        if v is None:
            return {}
        return dict(v)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, v: str) -> str:
        validate_endpoint_url(v)
        return v


    @field_validator("credential_ref")
    @classmethod
    def validate_credential(cls, v: str | None) -> str | None:
        validate_credential_ref(v)
        return v


class AgentVersionCreateRequest(AgentVersionSpecValidator):
    agent_id: str = Field(..., description="Parent agent ID")
    version: str = Field(..., description="Version tag, e.g. v1, 2.0.0")



class AgentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    version: str
    spec_digest: str
    artifact_ref: str | None = None
    endpoint: str
    protocol: str
    method: str
    request_mapping: dict[str, Any]
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    credential_ref: str | None = None
    timeout_seconds: float
    max_retries: int
    rate_limit_per_minute: int
    max_concurrency: int
    trace_propagation: str
    is_idempotent: bool
    environment: str | None = None
    is_active: bool
    created_at: datetime


class AgentVersionArchiveRequest(BaseModel):
    agent_id: str
    version: str


class ExperimentLaunchCreateRequest(BaseModel):
    name: str | None = Field(default=None, description="Optional experiment name")
    agent_id: str
    agent_version: str
    dataset_name: str
    dataset_version: str | None = None
    evaluator_ids: list[str] = Field(
        default_factory=lambda: ["intent_match", "required_tool_match", "pii_safe", "escalation_match"]
    )
    max_concurrency: int | None = Field(default=None, ge=1, le=50, description="Optional concurrency override; if omitted, inherits from AgentVersion")

    idempotency_key: str | None = Field(default=None, description="Optional idempotency key (can also be passed via Idempotency-Key header)")



class ExperimentLaunchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    quality_conclusion: str
    idempotency_key: str | None = None
    dataset_id: str | None = None
    dataset_name: str
    dataset_version: str | None = None
    agent_id: str
    agent_version: str
    agent_version_id: str
    manifest: dict[str, Any]
    langfuse_experiment_id: str | None = None
    langfuse_sync_status: str
    langfuse_sync_error: str | None = None
    created_by: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ExperimentLaunchRunRequest(BaseModel):
    launch_id: str = Field(..., description="ID of the launch to execute synchronously")


class ExecutionAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    item_execution_id: str
    attempt_no: int
    status: str
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    latency_ms: int
    trace_context_received: bool
    started_at: datetime
    completed_at: datetime | None = None


class ExperimentItemExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    launch_id: str
    dataset_item_id: str
    execution_status: str
    eval_status: str
    quality_conclusion: str
    execution_error: str | None = None
    eval_error: str | None = None
    trace_id: str | None = None
    observation_id: str | None = None
    final_attempt_id: str | None = None
    scores: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
