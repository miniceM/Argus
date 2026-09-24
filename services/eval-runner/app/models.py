from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .evaluators import default_evaluator_registry
from .security import validate_credential_ref, validate_endpoint_url


# ---------------------------------------------------------
# Backward Compatibility Models
# ---------------------------------------------------------
class ExperimentRequest(BaseModel):
    agent_id: str = "banking-agent"
    agent_version: str
    dataset_name: str = "banking-agent-regression"
    experiment_name: str | None = None
    max_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description="Optional concurrency override; if omitted, inherits from AgentVersion",
    )



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


class AgentSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None = None
    owner: str | None = None
    status: str
    version_count: int = 0
    latest_version: str | None = None
    launch_count: int = 0
    active_launch_count: int = 0
    created_at: datetime
    updated_at: datetime


class AgentResponse(AgentSummaryResponse):
    pass


class AgentDeleteResponse(BaseModel):
    id: str = Field(..., description="ID of deleted Agent")
    deleted: bool = Field(default=True, description="Whether the Agent was successfully deleted")
    launches_deleted: int = Field(default=0, description="Number of associated experiment launches cleaned up")
    message: str = Field(default="Agent deleted successfully")


class AgentPurgeRequest(BaseModel):
    agent_id: str = Field(..., description="Agent ID to permanently purge")
    confirm_name: str = Field(..., description="Exact agent name to confirm irreversible purge")


class DomainErrorResponse(BaseModel):
    code: str = Field(..., description="Machine-readable error code")
    detail: str = Field(..., description="Human-readable explanation")
    launch_count: int | None = Field(default=None, description="Associated launch count if applicable")
    active_launch_count: int | None = Field(default=None, description="Active launch count if applicable")


# ---------------------------------------------------------
# Domain Exceptions with Machine-Readable Codes
# ---------------------------------------------------------
class AgentRegistryError(Exception):
    def __init__(
        self,
        message: str,
        code: str,
        launch_count: int | None = None,
        active_launch_count: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.launch_count = launch_count
        self.active_launch_count = active_launch_count


class AgentNotFoundError(AgentRegistryError, KeyError):
    def __init__(self, agent_id: str):
        super().__init__(
            message=f"Agent '{agent_id}' not found",
            code="AGENT_NOT_FOUND",
        )
        self.agent_id = agent_id


class AgentHasLaunchesError(AgentRegistryError, ValueError):
    def __init__(self, agent_id: str, launch_count: int, active_launch_count: int = 0):
        super().__init__(
            message=(
                f"无法删除 Agent '{agent_id}'：存在 {launch_count} 条关联的评测记录 (Experiment Launches)。"
                "为防止误删历史评测数据，如确认清理，请通过 Purge 接口并确认 Agent 全称进行不可逆强制清理。"
            ),
            code="AGENT_HAS_LAUNCHES",
            launch_count=launch_count,
            active_launch_count=active_launch_count,
        )
        self.agent_id = agent_id


class AgentHasActiveLaunchesError(AgentRegistryError, ValueError):
    def __init__(self, agent_id: str, active_summary: str, active_launch_count: int):
        super().__init__(
            message=(
                f"无法删除 Agent '{agent_id}'：存在正在执行或排队中的评测任务（如 {active_summary}）。"
                "为防止任务执行中产生未定义副作用，请先取消或等待所有关联评测任务结束（状态为 COMPLETED、FAILED、CANCELLED 等终态）后，再进行删除。"
            ),
            code="AGENT_HAS_ACTIVE_LAUNCHES",
            active_launch_count=active_launch_count,
        )
        self.agent_id = agent_id


class AgentNameMismatchError(AgentRegistryError, ValueError):
    def __init__(self, agent_id: str, expected_name: str, provided_name: str):
        super().__init__(
            message=f"Agent 全称确认不匹配：期望 '{expected_name}'，实际提供 '{provided_name}'",
            code="AGENT_NAME_MISMATCH",
        )
        self.agent_id = agent_id
        self.expected_name = expected_name
        self.provided_name = provided_name


class AgentConcurrencyError(AgentRegistryError, ValueError):
    def __init__(self, agent_id: str, reason: str):
        super().__init__(
            message=f"Agent '{agent_id}' 并发操作冲突：{reason}",
            code="AGENT_CONCURRENCY_CONFLICT",
        )
        self.agent_id = agent_id







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
        default_factory=default_evaluator_registry.default_item_ids,
        min_length=1,
        description="List of item-scope evaluator IDs to run; must contain at least one evaluator. Run-scope evaluators are not supported by the standalone launch runner.",
    )
    max_concurrency: int | None = Field(default=None, ge=1, le=50, description="Optional concurrency override; if omitted, inherits from AgentVersion")


    idempotency_key: str | None = Field(default=None, description="Optional idempotency key (can also be passed via Idempotency-Key header)")



class EvaluatorResponse(BaseModel):
    id: str
    version: str
    scope: str
    threshold: float
    description: str | None = None
    default_selected: bool = False
    composed_of: list[str] = Field(default_factory=list)


class SystemInfoResponse(BaseModel):
    service: str = "argus-control-plane"
    version: str
    build_id: str
    environment: str
    langfuse_dashboard_url: str | None = None


class ExperimentLaunchProgressResponse(BaseModel):
    total: int = 0
    pending: int = 0
    queued: int = 0
    running: int = 0
    retry_wait: int = 0
    succeeded: int = 0
    failed: int = 0
    timed_out: int = 0
    cancelled: int = 0
    completed: int = 0
    percentage: float = 0.0
    attempts: int = 0
    retries: int = 0
    allowed_actions: list[str] = Field(default_factory=list)
    action_reasons: dict[str, str] = Field(default_factory=dict)


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
    langfuse_experiment_url: str | None = None
    langfuse_sync_status: str
    langfuse_sync_error: str | None = None
    links: dict[str, str | None] | None = None
    progress: ExperimentLaunchProgressResponse | None = None
    cancel_requested_at: datetime | None = None
    status_reason: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    created_by: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def populate_links(self) -> ExperimentLaunchResponse:
        if self.links is None:
            self.links = {
                "langfuse_experiment": self.langfuse_experiment_url,
                "langfuse_trace": None,
            }
        return self


class ExperimentLaunchRunRequest(BaseModel):
    launch_id: str = Field(..., description="ID of the launch to execute")


class ExperimentLaunchRunActionResponse(BaseModel):
    launch_id: str
    status: str
    message: str = "Launch queued for asynchronous execution"


class RetryFailedRequest(BaseModel):
    force: bool = Field(default=False, description="Force retry even if ambiguous non-idempotent outcomes exist")


class ExecutionAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    item_execution_id: str
    attempt_no: int
    status: str
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    latency_ms: int = 0
    trace_context_received: bool
    worker_id: str | None = None
    request_phase: str = "PREPARED"
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
    attempt_count: int = 0
    final_attempt_http_status: int | None = None
    final_attempt_latency_ms: int | None = None
    queued_at: datetime | None = None
    available_at: datetime | None = None
    lease_owner: str | None = None
    dispatch_generation: int = 1
    started_at: datetime | None = None
    completed_at: datetime | None = None
