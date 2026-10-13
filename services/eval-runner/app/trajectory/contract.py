"""冻结测量配置与执行时来源绑定；配置形状不证明来源资格或观测完整。"""

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal, get_args

from pydantic import Field, PrivateAttr, field_validator, model_validator

from .codec import canonical_digest, canonical_dumps
from .paths import compile_jsonpath
from .schema import (
    Capability,
    CapabilityCoverage,
    Duration,
    Identity,
    SchemaVersion,
    StepPhase,
    StepStatus,
    StepType,
    _ContractModel,
)

if TYPE_CHECKING:
    from app.registry import AgentVersionSpec

SourceField = Literal[
    "observation_id",
    "trace_id",
    "parent_step_id",
    "started_at",
    "ended_at",
    "provider_type",
    "raw_name",
    "input",
    "output",
    "phase",
    "status",
    "tool_raw_name",
    "workflow_scope_id",
    "event_id",
    "sequence",
    "call_id",
    "duration_ms",
    "argument_visibility",
    "hidden_argument_paths",
    "causal_event_ids",
    "terminal_event_count",
    "terminal_event_ids",
    "terminal_final_sequence",
]
SourcePath = Annotated[list[Identity], Field(min_length=1, max_length=32)]
SourceAliases = Annotated[list[SourcePath], Field(min_length=1, max_length=4)]
_CORE_FIELDS = {"observation_id", "trace_id", "parent_step_id", "provider_type", "raw_name"}
_INVENTORY_FIELDS = {
    "workflow_scope_id",
    "event_id",
    "sequence",
    "terminal_event_count",
    "terminal_event_ids",
    "terminal_final_sequence",
}
_STRUCTURAL_SEMANTIC_FIELDS = {
    "phase",
    "status",
    "event_id",
    "sequence",
    "call_id",
    "causal_event_ids",
    "terminal_event_count",
    "terminal_event_ids",
    "terminal_final_sequence",
}


def _secret_field(name: str) -> bool:
    # 同时识别点分 metadata、snake_case、CamelCase 与头部容器；不禁止普通 customer_key。
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    tokens = set(re.split(r"[^a-z0-9]+", split.casefold()))
    if tokens & {
        "authorization",
        "cookie",
        "cookies",
        "headers",
        "env",
        "environment",
        "password",
        "secret",
        "token",
        "credential",
        "credentials",
    }:
        return True
    compact = re.sub(r"[^a-z0-9]", "", name.casefold())
    return compact.endswith(
        ("token", "password", "secret", "headers", "apikey", "privatekey", "secretkey", "accesskey")
    )


class TrajectoryContractError(ValueError):
    pass


class TrajectoryLimits(_ContractModel):
    max_steps: int = Field(default=1000, ge=1, le=1000)
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=20, ge=1, le=100)
    max_source_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    max_artifact_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    request_timeout_seconds: Duration = Field(default=Decimal(5), gt=0, le=5)
    deadline_seconds: Duration = Field(default=Decimal(60), gt=0, le=300)
    quiet_window_seconds: Duration = Field(default=Decimal(2), ge=0, le=60)
    max_inflight_acquisitions: int = Field(default=4, ge=1, le=4)
    max_json_depth: int = Field(default=32, ge=1, le=32)
    max_string_bytes: int = Field(default=4096, ge=1, le=4096)
    max_numeric_digits: int = Field(default=64, ge=1, le=64)
    max_jsonpath_segments: int = Field(default=32, ge=1, le=32)

    @model_validator(mode="after")
    def usable_quiet_window(self):
        if self.quiet_window_seconds >= self.deadline_seconds:
            raise ValueError("TRAJECTORY_QUIET_WINDOW_EXCEEDS_DEADLINE")
        return self


class StructuralRole(_ContractModel):
    provider_type: Identity
    raw_name: Annotated[str, Field(pattern=r"^[\x20-\x7e]{1,64}$", max_length=64)]
    step_type: Literal["CUSTOM", "AGENT", "LLM"]
    safe_name: Identity
    metadata_equals: dict[Identity, Identity] = Field(min_length=1, max_length=32)

    @field_validator("raw_name")
    @classmethod
    def nonblank_name(cls, name):
        if not name.strip():
            raise ValueError("TRAJECTORY_STRUCTURAL_NAME_EMPTY")
        return name

    @field_validator("metadata_equals")
    @classmethod
    def safe_predicate_keys(cls, predicates):
        if any(_secret_field(key) for key in predicates):
            raise ValueError("TRAJECTORY_SOURCE_SECRET_PATH_FORBIDDEN")
        return predicates


class SourceProfile(_ContractModel):
    profile_id: Identity
    version: Identity
    provider: Literal["langfuse"]
    api_version: Literal["v2"]
    provider_major_version: int = Field(ge=1, le=64)
    fields: dict[SourceField, SourceAliases]
    metadata_equals: dict[Identity, Identity] = Field(max_length=32)
    type_map: dict[Identity, StepType] = Field(max_length=32)
    phase_map: dict[Identity, StepPhase] = Field(max_length=32)
    status_map: dict[Identity, StepStatus] = Field(max_length=32)
    safe_names: dict[Identity, Identity] = Field(max_length=1000)
    capabilities: dict[Capability, CapabilityCoverage]
    completion_protocol: Literal["workflow-inventory-v1", "root-only-v1"]
    human_authority_id: Identity | None
    # 空值不进入旧合同序列化；有声明时全部规则进入测量摘要。
    structural_roles: list[StructuralRole] = Field(
        default_factory=list, max_length=64, exclude_if=lambda roles: not roles
    )

    @field_validator("structural_roles")
    @classmethod
    def unambiguous_structural_roles(cls, roles):
        keys = [(role.provider_type, role.raw_name) for role in roles]
        if len(keys) != len(set(keys)):
            raise ValueError("TRAJECTORY_STRUCTURAL_ROLE_DUPLICATE")
        return sorted(roles, key=lambda role: (role.provider_type, role.raw_name))

    @field_validator("fields")
    @classmethod
    def explicit_field_paths(cls, fields):
        if not _CORE_FIELDS <= fields.keys():
            raise ValueError("TRAJECTORY_SOURCE_FIELDS_INCOMPLETE")
        if any(_secret_field(part) for aliases in fields.values() for path in aliases for part in path):
            raise ValueError("TRAJECTORY_SOURCE_SECRET_PATH_FORBIDDEN")
        return {
            key: [list(path) for path in sorted({tuple(path) for path in aliases})] for key, aliases in fields.items()
        }

    @field_validator("metadata_equals")
    @classmethod
    def safe_predicate_keys(cls, predicates):
        if any(_secret_field(key) for key in predicates):
            raise ValueError("TRAJECTORY_SOURCE_SECRET_PATH_FORBIDDEN")
        return predicates

    @model_validator(mode="after")
    def coherent_capabilities(self):
        if self.structural_roles and not _STRUCTURAL_SEMANTIC_FIELDS <= self.fields.keys():
            raise ValueError("TRAJECTORY_STRUCTURAL_GUARD_FIELDS_INCOMPLETE")
        identity_paths = {
            tuple(path)
            for field in ("provider_type", "raw_name", "tool_raw_name")
            for path in self.fields.get(field, [])
        }
        if any(tuple(path) in identity_paths for field in ("phase", "status") for path in self.fields.get(field, [])):
            raise ValueError("TRAJECTORY_SEMANTIC_SOURCE_CONFLICT")
        if self.completion_protocol == "workflow-inventory-v1" and not _INVENTORY_FIELDS <= self.fields.keys():
            raise ValueError("TRAJECTORY_INVENTORY_FIELDS_INCOMPLETE")
        if set(self.capabilities) != set(get_args(Capability)):
            raise ValueError("TRAJECTORY_CAPABILITIES_INCOMPLETE")
        if "HUMAN" in self.type_map.values():
            raise ValueError("TRAJECTORY_HUMAN_REQUIRES_VERIFIED_AUTHORITY_EVENT")
        if self.capabilities["human_events"] != "NONE" and self.human_authority_id is None:
            raise ValueError("TRAJECTORY_HUMAN_AUTHORITY_REQUIRED")
        if self.completion_protocol == "root-only-v1" and any(
            self.capabilities[key] != "NONE" for key in get_args(Capability) if key != "timing"
        ):
            raise ValueError("TRAJECTORY_ROOT_ONLY_CAPABILITIES_INVALID")
        if self.completion_protocol == "root-only-v1" and self.structural_roles:
            raise ValueError("TRAJECTORY_ROOT_ONLY_STRUCTURE_FORBIDDEN")
        return self


class ToolMappingEntry(_ContractModel):
    source_profile_id: Identity
    source_profile_version: Identity
    raw_name: Identity
    tool_identity: Identity


class ToolMapping(_ContractModel):
    version: Identity
    entries: list[ToolMappingEntry] = Field(max_length=1000)

    @field_validator("entries")
    @classmethod
    def unambiguous_entries(cls, entries):
        keys = [(entry.source_profile_id, entry.source_profile_version, entry.raw_name) for entry in entries]
        if len(keys) != len(set(keys)):
            raise ValueError("TRAJECTORY_TOOL_MAPPING_DUPLICATE")
        return sorted(
            entries, key=lambda entry: (entry.source_profile_id, entry.source_profile_version, entry.raw_name)
        )

    @property
    def digest(self) -> str:
        return canonical_digest(ToolMapping.model_validate(self.model_dump()))


class RedactionPolicy(_ContractModel):
    version: Identity
    input_paths: list[str] = Field(max_length=1000)
    output_paths: list[str] = Field(max_length=1000)
    argument_paths: list[str] = Field(max_length=1000)
    result_paths: list[str] = Field(max_length=1000)

    @field_validator("input_paths", "output_paths", "argument_paths", "result_paths")
    @classmethod
    def controlled_leaf_paths(cls, paths):
        for path in paths:
            tokens = compile_jsonpath(path)
            if not tokens:
                raise ValueError("TRAJECTORY_REDACTION_ROOT_FORBIDDEN")
            if any(isinstance(token, str) and _secret_field(token) for token in tokens):
                raise ValueError("TRAJECTORY_REDACTION_SECRET_PATH_FORBIDDEN")
        return sorted(set(paths))

    @property
    def digest(self) -> str:
        return canonical_digest(RedactionPolicy.model_validate(self.model_dump()))


class TrajectoryContract(_ContractModel):
    contract_version: Literal["1.0"]
    schema_version: SchemaVersion
    assembler_version: Identity
    source_profile_set: list[SourceProfile] = Field(min_length=1, max_length=16)
    source_profile_selector_version: Literal["agent-version-declaration-v1"]
    tool_mapping: ToolMapping
    redaction_policy: RedactionPolicy
    required_capabilities: list[Capability] = Field(max_length=6)
    rule_scope_version: Literal["attempt-root-parent-closure-v1"]
    limits: TrajectoryLimits
    # 执行绑定元数据不进入 model_dump/合同摘要；从不可变 AgentVersion 取得。
    _source_profile_id: str | None = PrivateAttr(default=None)

    @field_validator("source_profile_set")
    @classmethod
    def unique_profiles(cls, profiles):
        if len(profiles) != len({profile.profile_id for profile in profiles}):
            raise ValueError("TRAJECTORY_SOURCE_PROFILE_DUPLICATE")
        return sorted(profiles, key=lambda profile: profile.profile_id)

    @field_validator("required_capabilities")
    @classmethod
    def stable_capabilities(cls, capabilities):
        return sorted(set(capabilities))

    @model_validator(mode="after")
    def mapping_membership(self):
        profiles = {(profile.profile_id, profile.version) for profile in self.source_profile_set}
        if any(
            (entry.source_profile_id, entry.source_profile_version) not in profiles
            for entry in self.tool_mapping.entries
        ):
            raise ValueError("TRAJECTORY_TOOL_MAPPING_PROFILE_NOT_APPROVED")
        segment_limit = self.limits.max_jsonpath_segments
        if any(
            len(path) > segment_limit
            for profile in self.source_profile_set
            for aliases in profile.fields.values()
            for path in aliases
        ) or any(
            len(compile_jsonpath(path)) > segment_limit
            for field in ("input_paths", "output_paths", "argument_paths", "result_paths")
            for path in getattr(self.redaction_policy, field)
        ):
            raise ValueError("TRAJECTORY_CONTRACT_PATH_SEGMENT_LIMIT")
        # 合同自身始终使用 canonical 的 2 MiB 上限；artifact 预算单独约束后续采集制品。
        canonical_dumps(self)
        return self

    @property
    def digest(self) -> str:
        # frozen 不深冻结容器；摘要前校验当前配置，不能凭 model_copy 绕过。
        return canonical_digest(TrajectoryContract.model_validate(self.model_dump()))

    def bind_source_version(self, version: "AgentVersionSpec") -> "TrajectoryContract":
        profile_id = version.trajectory_source_profile_id
        if profile_id is None or not any(profile.profile_id == profile_id for profile in self.source_profile_set):
            raise TrajectoryContractError("TRAJECTORY_SOURCE_PROFILE_NOT_APPROVED")
        owned = TrajectoryContract.model_validate(self.model_dump())
        owned._source_profile_id = profile_id
        return owned

    @property
    def source_profile(self) -> SourceProfile:
        if self._source_profile_id is None:
            raise TrajectoryContractError("TRAJECTORY_SOURCE_PROFILE_UNBOUND")
        for profile in self.source_profile_set:
            if profile.profile_id == self._source_profile_id:
                return SourceProfile.model_validate(profile.model_dump())
        raise TrajectoryContractError("TRAJECTORY_SOURCE_PROFILE_NOT_APPROVED")
