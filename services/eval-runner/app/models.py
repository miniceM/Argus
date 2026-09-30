from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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







class UsageCostMapping(BaseModel):
    """Explicit dot-path mapping for usage and invocation-total cost in a JSON response."""

    model_config = ConfigDict(extra="forbid")

    input_tokens_path: str | None = None
    output_tokens_path: str | None = None
    total_tokens_path: str | None = None
    amount_path: str | None = None
    currency_path: str | None = None
    source: Literal["provider_reported"] = "provider_reported"
    measurement_scope: Literal["agent_invocation_total"]

    @field_validator(
        "input_tokens_path", "output_tokens_path", "total_tokens_path", "amount_path", "currency_path"
    )
    @classmethod
    def validate_json_path(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
            raise ValueError("Usage/cost paths must be simple dot-separated JSON object keys")
        return value

    @model_validator(mode="after")
    def validate_mapping(self) -> UsageCostMapping:
        if bool(self.amount_path) != bool(self.currency_path):
            raise ValueError("amount_path and currency_path must be configured together")
        if not any((self.input_tokens_path, self.output_tokens_path, self.total_tokens_path, self.amount_path)):
            raise ValueError("At least one usage or cost response path must be configured")
        return self


class AgentVersionSpecValidator(BaseModel):
    endpoint: str = Field(..., description="HTTP POST URL of the agent")
    protocol: str = Field(default="HTTP_JSON", description="Invocation protocol, currently HTTP_JSON only")
    method: str = Field(default="POST", description="HTTP method, currently POST only")
    request_mapping: dict[str, str] = Field(default_factory=dict, description="Dot-path field mapping")
    usage_cost_mapping: UsageCostMapping | None = None
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
    usage_cost_mapping: UsageCostMapping | None = None
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
    environment: str = Field(default="production", min_length=1, max_length=64)
    baseline_snapshot_id: str | None = Field(default=None, min_length=1, max_length=64)
    evaluator_ids: list[str] | None = Field(
        default=None,
        description=(
            "Legacy convenience field: item-scope evaluator IDs resolved to their current "
            "default version at submission time and frozen into the Manifest. Prefer "
            "`evaluator_selections`, which pins an explicit user-confirmed version per id."
        ),
    )
    evaluator_selections: list[EvaluatorSelection] | None = Field(
        default=None,
        description=(
            "Exact Evaluator id + immutable version selections confirmed by the user. "
            "Each entry is validated for release eligibility and scope server-side."
        ),
    )
    max_concurrency: int | None = Field(default=None, ge=1, le=50, description="Optional concurrency override; if omitted, inherits from AgentVersion")
    quality_policy: QualityPolicyRequest | None = Field(
        default=None,
        description=(
            "Issue #83: the judgement rules frozen with this Launch. Omit it to "
            "accept the default all-required policy over the selected metrics. "
            "An illegal rule (unknown operator, type mismatch, unknown metric) is "
            "rejected at creation instead of failing silently at run time."
        ),
    )


    idempotency_key: str | None = Field(default=None, description="Optional idempotency key (can also be passed via Idempotency-Key header)")



class EvaluatorVersionInfo(BaseModel):
    """One immutable Evaluator version exposed in the catalog (Issue #80)."""

    version: str
    result_type: str
    scope: str
    threshold: float
    direction: str = "higher_is_better"
    critical: bool = False
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    param_schema: dict[str, Any] = Field(default_factory=dict)
    implementation_ref: str | None = None
    executor_type: str
    category_values: list[str] | None = None
    ordered_category_values: list[str] | None = None
    content_digest: str
    release_eligible: bool = False
    eligibility_reasons: list[str] = Field(default_factory=list)
    eligibility_messages: list[str] = Field(default_factory=list)


class EvaluatorResponse(BaseModel):
    """Catalog entry for one Evaluator, carrying its immutable version list."""

    id: str
    name: str
    version: str
    scope: str
    threshold: float
    description: str | None = None
    default_selected: bool = False
    composed_of: list[str] = Field(default_factory=list)
    direction: str = "higher_is_better"
    critical: bool = False
    # Issue #80: identity / provenance / eligibility
    result_type: str = "numeric"
    definition_source: str = "ARGUS_BUILTIN"
    execution_owner: str = "ARGUS"
    implementation_ref: str | None = None
    executor_type: str = "builtin_python"
    content_digest: str
    release_eligible: bool = False
    eligibility_reasons: list[str] = Field(default_factory=list)
    default_version: str
    versions: list[EvaluatorVersionInfo] = Field(default_factory=list)


class QualityRuleRequest(BaseModel):
    """One user-authored judgement rule over a selected metric (Issue #83)."""

    evaluator_id: str = Field(..., min_length=1, max_length=128)
    operator: str | None = Field(
        default=None,
        description=(
            "Comparison operator. numeric accepts >= / <=, boolean and categorical "
            "accept ==. A metric with no operator is recorded as evidence only."
        ),
    )
    threshold: float | None = Field(default=None, description="numeric 规则的阈值")
    expected_value: Any | None = Field(
        default=None, description="boolean / categorical 规则的显式期望取值"
    )
    result_type: str = Field(default="numeric", description="被引用指标的结果类型")
    required: bool = Field(default=True, description="是否为必要规则；必要规则的证据不足会得到 UNKNOWN")
    critical: bool = Field(default=False, description="是否为关键规则")
    note: str | None = None


class QualityPolicyRequest(BaseModel):
    """The independent quality policy frozen with a new Launch (Issue #83)."""

    rules: list[QualityRuleRequest] = Field(..., min_length=1)


class QualityRuleEvaluationResponse(BaseModel):
    """One rule's outcome for one case, with the reason in plain language."""

    model_config = ConfigDict(extra="allow")

    evaluator_id: str
    result_type: str | None = None
    required: bool = True
    critical: bool = False
    operator: str | None = None
    expected: Any | None = None
    observed_value: Any | None = None
    observed_status: str | None = None
    conclusion: str = "unknown"
    reason_code: str | None = None
    explanation: str | None = None


class QualityEvaluationResponse(BaseModel):
    """The per-case quality decision recorded under the frozen policy."""

    model_config = ConfigDict(extra="allow")

    conclusion: str
    policy_id: str | None = None
    policy_version: str | None = None
    policy_digest: str | None = None
    decided_by: str | None = None
    releasable: bool = False
    unknown_reasons: list[str] = Field(default_factory=list)
    rules: list[QualityRuleEvaluationResponse] = Field(default_factory=list)


class EvaluatorSelection(BaseModel):
    """An exact Evaluator id + version the user confirmed on the create form."""

    id: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=64)


class EvaluatorSelectionErrorResponse(BaseModel):
    """Structured rejection returned when a selection cannot be used (Issue #80)."""

    code: str = Field(..., description="Machine-readable error code")
    message: str = Field(..., description="Human-readable explanation")
    evaluator_id: str | None = None
    version: str | None = None
    eligibility_reasons: list[str] = Field(default_factory=list)


class BaselineCreateRequest(BaseModel):
    environment: str = Field(..., min_length=1, max_length=64)
    result_snapshot_id: str = Field(..., min_length=1, max_length=64)
    expected_revision: int = Field(..., ge=0)


class BaselineResponse(BaseModel):
    agent_id: str
    environment: str
    result_snapshot_id: str
    revision: int
    # Issue #85: the binding revision above is the *pointer* revision. The
    # result revision below is the frozen report it points at. Both are
    # returned explicitly so a success response never leaves "latest" implied.
    result_revision: int = 0
    result_evidence_state: str = "COMPLETE"
    updated_by: str | None = None
    updated_at: datetime
    launch_id: str
    agent_version: str
    dataset_name: str
    dataset_version: str | None = None
    summary: dict[str, Any]


class RunCostUnavailableReason(StrEnum):
    COST_NOT_RECORDED = "COST_NOT_RECORDED"
    INVALID_COST_EVIDENCE = "INVALID_COST_EVIDENCE"
    INCOMPLETE_ATTEMPT_COST = "INCOMPLETE_ATTEMPT_COST"
    MIXED_CURRENCIES = "MIXED_CURRENCIES"
    COST_SOURCE_MISMATCH = "COST_SOURCE_MISMATCH"
    COST_SCOPE_MISMATCH = "COST_SCOPE_MISMATCH"
    COST_POLICY_MISMATCH = "COST_POLICY_MISMATCH"
    PARTIAL_COST_COVERAGE = "PARTIAL_COST_COVERAGE"


class CostComparisonReason(StrEnum):
    COST_NOT_RECORDED = "COST_NOT_RECORDED"
    INVALID_COST_EVIDENCE = "INVALID_COST_EVIDENCE"
    INCOMPLETE_ATTEMPT_COST = "INCOMPLETE_ATTEMPT_COST"
    MIXED_CURRENCIES = "MIXED_CURRENCIES"
    COST_SOURCE_MISMATCH = "COST_SOURCE_MISMATCH"
    COST_SCOPE_MISMATCH = "COST_SCOPE_MISMATCH"
    COST_POLICY_MISMATCH = "COST_POLICY_MISMATCH"
    PARTIAL_COST_COVERAGE = "PARTIAL_COST_COVERAGE"
    COST_CURRENCY_MISMATCH = "COST_CURRENCY_MISMATCH"
    BASELINE_NOT_BOUND = "BASELINE_NOT_BOUND"
    NO_COMPARABLE_CASES = "NO_COMPARABLE_CASES"


class CostComparisonStatus(StrEnum):
    COMPARABLE = "COMPARABLE"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class RunCostSummaryResponse(BaseModel):
    """Typed cost portion of a frozen run summary; retain other metrics for compatibility."""

    model_config = ConfigDict(extra="allow")

    total_cost: float | None = None
    cost_per_case: float | None = None
    cost_currency: str | None = None
    cost_case_count: int | None = None
    cost_coverage: float | None = None
    cost_source: str | None = None
    cost_scope: str | None = None
    cost_policy_version: str | None = None
    cost_partial: bool | None = None
    cost_unavailable_reason: RunCostUnavailableReason | None = None


class CostComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: CostComparisonStatus
    reason: CostComparisonReason | None
    cohort: Literal["quality_comparable_cases"]
    case_count: int
    currency: str | None
    baseline_cost_per_case: float | None
    candidate_cost_per_case: float | None
    delta: float | None
    baseline_coverage: float | None
    candidate_coverage: float | None
    policy_version: str | None


class ComparableCohortResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    baseline: RunCostSummaryResponse
    candidate: RunCostSummaryResponse


class ComparisonSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate: RunCostSummaryResponse
    baseline: RunCostSummaryResponse | None
    comparable_case_count: int
    classification_counts: dict[str, int]
    comparable_cohort: ComparableCohortResponse | None
    cost_comparison: CostComparisonResponse
    pass_rate_delta: float | None
    score_mean_deltas: dict[str, float]


class RunSummaryResponse(BaseModel):
    launch_id: str
    snapshot_id: str
    revision: int
    created_at: datetime
    manifest_digest: str
    # Issue #85: the digest of the frozen items themselves (typed results plus
    # their provenance), so a shared link identifies exactly these results.
    source_result_digest: str = ""
    # Issue #85: this revision's own evidence verdict. COMPLETE means it may be
    # used as formal release / Baseline evidence; DIAGNOSTIC means it only
    # explains a failure.
    evidence_state: str = "COMPLETE"
    evidence_reasons: list[str] = Field(default_factory=list)
    versions: dict[str, Any]
    summary: RunCostSummaryResponse
    langfuse_score_sync_status: str = "PENDING"
    # Issue #87: quality results and sync state are separate facts. A Langfuse
    # outage never changes the Snapshot, its digest or the quality conclusion,
    # and the two sync scopes are reported independently.
    langfuse_sync: LangfuseSyncStatusResponse | None = None


class ResultSnapshotRevisionResponse(BaseModel):
    """One frozen revision of a Launch (Issue #85)."""

    snapshot_id: str
    revision: int
    created_at: datetime
    source_result_digest: str
    manifest_digest: str
    evidence_state: str
    evidence_reasons: list[str] = Field(default_factory=list)
    total_cases: int = 0
    quality_pass_count: int = 0
    quality_fail_count: int = 0
    quality_unknown_count: int = 0
    is_latest: bool = False


class ResultSnapshotListResponse(BaseModel):
    launch_id: str
    latest_snapshot_id: str | None = None
    latest_revision: int | None = None
    revisions: list[ResultSnapshotRevisionResponse] = Field(default_factory=list)


class ResultSnapshotDetailResponse(BaseModel):
    """The immutable contents of one revision, addressed by its own id."""

    launch_id: str
    snapshot_id: str
    revision: int
    created_at: datetime
    source_result_digest: str
    manifest_digest: str
    evidence_state: str
    evidence_reasons: list[str] = Field(default_factory=list)
    releasable: bool = False
    versions: dict[str, Any]
    summary: RunCostSummaryResponse
    items: list[dict[str, Any]] = Field(default_factory=list)


class LangfuseSyncScopeResponse(BaseModel):
    """One independently reportable Langfuse sync scope."""

    status: str
    reason: str | None = None
    task_count: int = 0
    failed_count: int = 0
    pending_count: int = 0


class LangfuseSyncStatusResponse(BaseModel):
    """Sync state that never lets one scope hide a broken one (Issue #87)."""

    overall: str
    item_trace: LangfuseSyncScopeResponse
    run_score: LangfuseSyncScopeResponse


class ComparisonContractDimension(BaseModel):
    """One independently versioned contract, and whether it matches."""

    dimension: str
    status: str
    baseline_digest: str | None = None
    candidate_digest: str | None = None
    baseline_version: str | None = None
    candidate_version: str | None = None


class ComparisonComparability(BaseModel):
    """Whether a formal comparison is allowed, and what to do about it."""

    comparable: bool
    reason_codes: list[str]
    provenance: str
    dimensions: list[ComparisonContractDimension]
    suggestions: list[str] = Field(default_factory=list)


class ComparisonFormalVerdict(BaseModel):
    """The run-level release-grade conclusion, withheld unless fully supported."""

    available: bool
    verdict: str | None = None
    reason: str | None = None
    required_cases: int = 0
    comparable_cases: int = 0
    coverage: float = 0.0
    withheld_reasons: list[str] = Field(default_factory=list)


class ComparisonDiagnostic(BaseModel):
    """Best-effort per-case diagnosis. Never a release-grade conclusion."""

    note: str
    comparable_cases: int
    classification_counts: dict[str, int]


class ComparisonResponse(BaseModel):
    launch_id: str
    candidate_snapshot_id: str
    baseline_snapshot_id: str | None = None
    baseline_binding_revision: int | None = None
    versions: dict[str, Any]
    summary: ComparisonSummaryResponse
    # Diagnostic case counts, kept at the top level for existing readers;
    # `diagnostic` is the labelled home for the same numbers.
    classification_counts: dict[str, int]
    comparability: ComparisonComparability
    formal: ComparisonFormalVerdict
    diagnostic: ComparisonDiagnostic
    items: list[dict[str, Any]]
    next_cursor: int | None = None


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
    # Issue #84: how many cases can be re-judged without re-calling the Agent.
    recoverable_evaluation_count: int = 0


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


class EvaluationRetryBlockedItem(BaseModel):
    """One case that cannot be re-judged, with the reason (Issue #84)."""

    item_execution_id: str
    dataset_item_id: str
    code: str
    message: str
    hint: str | None = None


class RetryEvaluationResponse(BaseModel):
    """Result of an evaluation-only retry submission (Issue #84)."""

    launch: ExperimentLaunchResponse
    # Cases whose failed/missing evaluation was dispatched for recovery.
    submitted: list[str] = Field(default_factory=list)
    # Cases already being re-evaluated (idempotent double-click / race).
    already_running: list[str] = Field(default_factory=list)
    # Cases that cannot be re-judged (missing/expired/corrupt checkpoint, etc.).
    blocked: list[EvaluationRetryBlockedItem] = Field(default_factory=list)
    message: str = ""


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


class EvaluationResultProvenance(BaseModel):
    """Frozen evidence source of one typed result (Issue #82)."""

    model_config = ConfigDict(from_attributes=True)

    binding_id: str | None = None
    evaluator_id: str | None = None
    evaluator_version: str | None = None
    definition_digest: str | None = None
    executor_type: str | None = None
    manifest_schema_version: str | None = None
    contract_status: str | None = None


class EvaluationResultResponse(BaseModel):
    """One typed, explainable measurement (Issue #82).

    ``value`` keeps its original JSON type; ``normalized_value`` is populated
    only by an explicitly frozen rule and may legitimately be null.
    """

    model_config = ConfigDict(from_attributes=True)

    evaluator_id: str
    evaluator_version: str | None = None
    result_type: str
    status: str
    value: Any | None = None
    normalized_value: float | None = None
    comment: str | None = None
    evidence: dict[str, Any] | None = None
    duration_ms: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    provenance: EvaluationResultProvenance | None = None


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
    # Issue #82: the Console links a result to its Langfuse trace.
    langfuse_trace_url: str | None = None
    final_attempt_id: str | None = None
    scores: dict[str, Any] | None = None
    # Issue #82: authoritative typed results; `scores` is only a projection.
    evaluation_results: list[EvaluationResultResponse] = Field(default_factory=list)
    # Issue #83: the frozen policy's per-rule decision. Null for items judged
    # before QualityPolicy existed — their verdict stands, it is just not
    # re-explained under a policy they never had.
    quality_evaluation: QualityEvaluationResponse | None = None
    # Issue #84: the independent evaluation-recovery lifecycle. `eval_status`
    # stays the measurement outcome; these describe whether a re-evaluation is
    # idle / running / recovered / failed, and which output digest it reused.
    evaluation_status: str = "none"
    evaluation_generation: int = 0
    evaluation_error: str | None = None
    evaluation_reused_output_digest: str | None = None
    # Whether a recoverable Agent output checkpoint exists for this case, so the
    # Console can explain "reuse the original Agent output" before offering a
    # re-evaluation. A missing/expired/corrupt checkpoint must block recovery
    # (and is never silently downgraded to re-invoking the Agent).
    evaluation_recoverable: bool = False
    attempt_count: int = 0
    final_attempt_http_status: int | None = None
    final_attempt_latency_ms: int | None = None
    queued_at: datetime | None = None
    available_at: datetime | None = None
    lease_owner: str | None = None
    dispatch_generation: int = 1
    started_at: datetime | None = None
    completed_at: datetime | None = None
