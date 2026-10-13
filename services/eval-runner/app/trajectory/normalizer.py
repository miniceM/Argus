"""冻结字段映射生成脱敏候选 Step；不证明闭包、因果或完整度。"""

import hashlib
import re
from datetime import UTC, datetime
from typing import get_args

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from .codec import canonical_digest, canonical_dumps, canonical_loads, validate_json_value
from .contract import _STRUCTURAL_SEMANTIC_FIELDS, SourceProfile, TrajectoryContract
from .redaction import project_json, validate_bounded_json
from .schema import (
    Capability,
    CapabilityCoverage,
    Digest,
    Duration,
    Identity,
    ReasonCode,
    Timestamp,
    TrajectoryScope,
    TrajectoryStep,
    _ContractModel,
)

_MISSING = object()
_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")


class NormalizedObservation(_ContractModel):
    step: TrajectoryStep | None
    capabilities: dict[Capability, CapabilityCoverage]
    warnings: list[ReasonCode]
    structural: bool = False
    event_id: Identity | None = None
    call_id: Identity | None = None
    sequence: int | None = Field(default=None, ge=1, le=1000)
    causal_event_ids: list[Identity] = Field(default_factory=list, max_length=1000)
    terminal_event_count: int | None = Field(default=None, ge=0, le=1000)
    terminal_event_ids: list[Identity] | None = Field(default=None, max_length=1000)
    terminal_final_sequence: int | None = Field(default=None, ge=1, le=1000)


class NormalizedRecord(_ContractModel):
    """仅保留字段映射确认的身份；拒绝行也不丢失父链归属线索。"""

    observation_id: Identity | None
    trace_id: Identity | None
    parent_step_id: Identity | None
    parent_known: bool
    observation: NormalizedObservation

    @model_validator(mode="after")
    def consistent_identity(self):
        step = self.observation.step
        if step is not None and (
            step.observation_id != self.observation_id
            or not self.parent_known
            or step.parent_step_id != self.parent_step_id
        ):
            raise ValueError("TRAJECTORY_NORMALIZED_IDENTITY_MISMATCH")
        return self


class ObservationConvergence(_ContractModel):
    """仅由内部 Acquisition 提供的分页/静默证明；不从 Observation metadata 读取。"""

    normalized_digest: Digest
    paging_complete: bool
    quiet_window_seconds: Duration = Field(le=60)


class NormalizedObservations(_ContractModel):
    scope: TrajectoryScope
    trajectory_contract_digest: Digest
    source_profile_id: Identity
    source_profile_version: Identity
    records: list[NormalizedRecord] = Field(max_length=10000)
    source_bytes: int = Field(ge=0, le=8 * 1024 * 1024)
    source_bytes_complete: bool
    truncated: bool
    warnings: list[ReasonCode] = Field(max_length=1000)
    convergence: ObservationConvergence | None = Field(default=None, exclude_if=lambda proof: proof is None)

    @property
    def semantic_digest(self) -> str:
        # 证明绑定全部候选与拒绝行；到达顺序和临时 index 不构成输入身份。
        data = self.model_dump(exclude={"convergence"})
        for record in data["records"]:
            observation = record["observation"]
            observation["warnings"] = sorted(set(observation["warnings"]))
            observation["causal_event_ids"] = sorted(set(observation["causal_event_ids"]))
            if observation["terminal_event_ids"] is not None:
                observation["terminal_event_ids"].sort()
            if observation["step"] is not None:
                observation["step"]["index"] = 0
        data["records"].sort(key=lambda record: canonical_dumps(record, max_bytes=8 * 1024 * 1024))
        data["warnings"] = sorted(set(data["warnings"]))
        raw = canonical_dumps(data, max_bytes=8 * 1024 * 1024).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    @model_validator(mode="after")
    def retained_steps_match_scope(self):
        if any(
            record.observation.step is not None and record.trace_id != self.scope.trace_id for record in self.records
        ):
            raise ValueError("TRAJECTORY_NORMALIZED_SCOPE_MISMATCH")
        if self.convergence is not None and self.convergence.normalized_digest != self.semantic_digest:
            raise ValueError("TRAJECTORY_CONVERGENCE_BATCH_MISMATCH")
        return self


class _InvalidField(ValueError):
    pass


def _field(row, profile, name, max_bytes):
    values = []
    for path in profile.fields.get(name, []):
        value = row
        for key in path:
            if type(value) is not dict or key not in value:
                value = _MISSING
                break
            value = value[key]
        if value is not _MISSING:
            values.append(value)
    if not values:
        return _MISSING
    if len(values) > 1:
        try:
            encodings = set()
            for value in values:
                if name in {"started_at", "ended_at"}:
                    if type(value) is not datetime:
                        validate_json_value(value)
                    try:
                        value = TypeAdapter(Timestamp).validate_python(value).astimezone(UTC)
                    except (ValidationError, ValueError, OverflowError):
                        return None  # 无有效时间不能挑一个 alias 当真；保留其它工具事实。
                else:
                    validate_json_value(value)
                encodings.add(canonical_dumps(value, max_bytes=max_bytes))
        except (ValueError, UnicodeError, RecursionError):
            raise _InvalidField("SOURCE_FIELD_INVALID") from None
        if len(encodings) != 1:
            raise _InvalidField("SOURCE_FIELD_ALIAS_CONFLICT")
    return values[0]


def _id(value):
    if type(value) is not str:
        raise _InvalidField("SOURCE_IDENTITY_INVALID")
    try:
        return TypeAdapter(Identity).validate_python(value, strict=True)
    except ValidationError:
        raise _InvalidField("SOURCE_IDENTITY_INVALID") from None


def _opaque(value):
    return value if type(value) is str and _OPAQUE_ID.fullmatch(value) else None


def _integer(value, maximum, *, minimum=1):
    return value if type(value) is int and minimum <= value <= maximum else None


def _ids(value, limits):
    if value is _MISSING:
        return None
    try:
        value = canonical_loads(value, max_bytes=limits.max_source_bytes) if type(value) is str else value
    except (ValueError, UnicodeError, RecursionError):
        return None
    if type(value) is not list or len(value) > limits.max_steps or any(_opaque(item) is None for item in value):
        return None
    return sorted(value) if len(value) == len(set(value)) else None


def _optional(value, adapter, warning, warnings):
    if value is _MISSING or value is None:
        warnings.append(warning)
        return None
    try:
        parsed = TypeAdapter(adapter).validate_python(value)
        return parsed.astimezone(UTC) if adapter is Timestamp else parsed
    except (ValidationError, ValueError, OverflowError):
        warnings.append(warning)
        return None


def normalize_observation(
    row, contract: TrajectoryContract, scope: TrajectoryScope, *, index: int
) -> NormalizedObservation:
    return ObservationNormalizer(contract).normalize(row, scope, index=index)


class ObservationNormalizer:
    """一个采集批次持有一份独立配置快照；每行仍校验身份与容量。

    创建时重新校验完整合同与已绑定来源，不接受 Trace 自报 profile。
    不保留原始行或结果，不证明采集闭包，也不跨合同复用。
    """

    __slots__ = ("_contract", "_profile")

    def __init__(self, contract: TrajectoryContract):
        self._contract = TrajectoryContract.model_validate(contract.model_dump())
        profile_id = contract.source_profile.profile_id
        self._profile = next(
            profile for profile in self._contract.source_profile_set if profile.profile_id == profile_id
        )

    def normalize(self, row, scope: TrajectoryScope, *, index: int) -> NormalizedObservation:
        return _normalize_observation(row, self._contract, self._profile, scope, index=index)


def normalize_observations(
    rows: list[dict[str, object]] | tuple[dict[str, object], ...],
    contract: TrajectoryContract,
    *,
    scope: TrajectoryScope,
) -> NormalizedObservations:
    """归一化一个有界 JSON-wire 批次；不做闭包、去重或完整度判断。

    source_bytes 计当前成功编码行的 canonical UTF-8 大小；不是 HTTP wire
    大小。无法编码或未读完时显式标为不完整，传输总量仍由 Reader/Acquisition
    单独核算。index 固定为 0，后续 Assembler 在去重和限界后分配展示序号。
    """
    if type(rows) not in (list, tuple):
        raise ValueError("SOURCE_COLLECTION_INVALID")
    scope = TrajectoryScope.model_validate(scope.model_dump())
    normalizer = ObservationNormalizer(contract)
    limits, profile = normalizer._contract.limits, normalizer._profile
    records, warnings = [], []
    source_bytes, truncated, bytes_complete = 0, False, True
    empty = {key: "NONE" for key in get_args(Capability)}
    for index, row in enumerate(rows):
        if index >= limits.page_size * limits.max_pages:
            warnings.append("SOURCE_ROW_LIMIT")
            truncated, bytes_complete = True, False
            break
        lineage = _normalized_lineage(row, profile, limits.max_source_bytes)
        try:
            if type(row) is not dict:
                raise ValueError("SOURCE_FIELD_INVALID")
            # 整行遵守 wire codec 的硬限额；合同字段预算由单行归一化按字段降级。
            validate_json_value(row)
            encoded = canonical_dumps(row, max_bytes=limits.max_source_bytes - source_bytes)
        except (ValueError, UnicodeError, RecursionError) as error:
            bytes_complete = False
            if str(error) == "TRAJECTORY_JSON_BYTE_LIMIT":
                warnings.append("SOURCE_BYTE_LIMIT")
                truncated = True
                break
            observation = NormalizedObservation(step=None, capabilities=empty, warnings=["SOURCE_FIELD_INVALID"])
        else:
            source_bytes += len(encoded.encode("utf-8"))
            observation = normalizer.normalize(row, scope, index=0)
        records.append(NormalizedRecord(**lineage, observation=observation))
    return NormalizedObservations(
        scope=scope,
        trajectory_contract_digest=canonical_digest(normalizer._contract),
        source_profile_id=profile.profile_id,
        source_profile_version=profile.version,
        records=records,
        source_bytes=source_bytes,
        source_bytes_complete=bytes_complete,
        truncated=truncated,
        warnings=warnings,
    )


def _normalized_lineage(row, profile, max_bytes):
    lineage = {"observation_id": None, "trace_id": None, "parent_step_id": None, "parent_known": False}
    if type(row) is not dict:
        return lineage
    for field in ("observation_id", "trace_id", "parent_step_id"):
        try:
            value = _field(row, profile, field, max_bytes)
            if field == "parent_step_id" and value is None:
                lineage["parent_known"] = True
            else:
                lineage[field] = _id(value)
                if field == "parent_step_id":
                    lineage["parent_known"] = True
        except _InvalidField:
            pass  # 不从冲突 alias、缺字段或非法值中挑选身份。
    return lineage


def _normalize_observation(
    row, checked: TrajectoryContract, profile: SourceProfile, scope: TrajectoryScope, *, index: int
) -> NormalizedObservation:
    limits = checked.limits
    scope = TrajectoryScope.model_validate(scope.model_dump())
    warnings = []
    capabilities = dict(profile.capabilities)
    empty = {key: "NONE" for key in get_args(Capability)}
    try:
        if type(row) is not dict or type(index) is not int or not 0 <= index < limits.max_steps:
            raise _InvalidField("SOURCE_FIELD_INVALID")
        values = {name: _field(row, profile, name, limits.max_source_bytes) for name in profile.fields}
        observation_id, trace_id = _id(values["observation_id"]), _id(values["trace_id"])
        if trace_id != scope.trace_id:
            raise _InvalidField("SOURCE_TRACE_SCOPE_MISMATCH")
        parent = values["parent_step_id"]
        if parent is _MISSING:
            raise _InvalidField("SOURCE_PARENT_UNAVAILABLE")
        parent = _id(parent) if parent is not None else None
        raw_name, provider_type = values["raw_name"], values["provider_type"]
        if type(raw_name) is not str or type(provider_type) is not str:
            raise _InvalidField("SOURCE_FIELD_INVALID")
        root = observation_id == scope.root_observation_id
        if root:
            if raw_name != "remote-agent-http" or profile.safe_names.get(raw_name) != "remote-agent-http":
                raise _InvalidField("SOURCE_ROOT_NAME_INVALID")
            step_type, phase, status = "CUSTOM", "EXECUTED", "UNKNOWN"
            capabilities = dict(empty)
        else:
            metadata = row.get("metadata")
            structural_role = _structural_role(profile, values, metadata)
            if structural_role is not None:
                return _structural_observation(values, structural_role, observation_id, parent, index, limits, empty)
            if type(metadata) is not dict or any(
                type(metadata.get(key, _MISSING)) is not str or metadata[key] != value
                for key, value in profile.metadata_equals.items()
            ):
                raise _InvalidField("SOURCE_PREDICATE_MISMATCH")
            phase = profile.phase_map.get(values.get("phase")) if type(values.get("phase")) is str else None
            status = profile.status_map.get(values.get("status")) if type(values.get("status")) is str else None
            step_type = profile.type_map.get(provider_type)
            if phase is None or status is None or step_type is None:
                raise _InvalidField("STEP_SEMANTICS_UNAVAILABLE")
        tool_identity = None
        raw_tool = values.get("tool_raw_name")
        if step_type == "TOOL":
            tool_identity = next(
                (
                    entry.tool_identity
                    for entry in checked.tool_mapping.entries
                    if entry.source_profile_id == profile.profile_id
                    and entry.source_profile_version == profile.version
                    and type(raw_tool) is str
                    and entry.raw_name == raw_tool
                ),
                None,
            )
            if tool_identity is None:
                warnings.append("TOOL_MAPPING_UNRESOLVED")
                capabilities["tool_executions"] = capabilities["tool_requests"] = "PARTIAL"
        safe_name = profile.safe_names.get(raw_name)
        if safe_name is None:
            # 获准物理工具名可保留，其余自由文本使用固定占位，不哈希敏感原文。
            safe_name = raw_tool if tool_identity is not None else "unapproved-name"
            warnings.append("SOURCE_NAME_NOT_APPROVED")
        start = _optional(values.get("started_at", _MISSING), Timestamp, "STEP_TIME_INVALID", warnings)
        end = _optional(values.get("ended_at", _MISSING), Timestamp, "STEP_TIME_INVALID", warnings)
        duration = _optional(values.get("duration_ms", _MISSING), Duration, "STEP_DURATION_UNAVAILABLE", warnings)
        if duration is not None:
            try:
                validate_bounded_json(duration, limits)
            except ValueError:
                duration = None
                warnings.append("STEP_DURATION_UNAVAILABLE")
        if duration is None:
            capabilities["timing"] = "NONE"
        if start is None or end is None or end < start:
            capabilities["ordering"] = "PARTIAL" if capabilities["ordering"] != "NONE" else "NONE"
            warnings.append("STEP_TIME_INVALID")
        hidden = values.get("hidden_argument_paths", _MISSING)
        argument_paths = checked.redaction_policy.input_paths if root else checked.redaction_policy.argument_paths
        argument_input = values.get("input", _MISSING)
        argument_input = None if argument_input is _MISSING else argument_input
        argument_marker = values.get("argument_visibility", _MISSING)
        if root or (argument_marker is _MISSING and argument_input is None):
            argument_marker = "AVAILABLE"
        try:
            hidden = (
                []
                if hidden is _MISSING
                else canonical_loads(hidden, max_bytes=limits.max_source_bytes)
                if type(hidden) is str
                else hidden
            )
            if type(hidden) is not list or len(hidden) > 1000 or any(type(path) is not str for path in hidden):
                raise ValueError("ARGUMENTS_INVALID")
            arguments = project_json(
                argument_input,
                argument_paths,
                limits=limits,
                hidden_paths=hidden,
                source_visibility=argument_marker,
            )
        except (ValueError, UnicodeError, RecursionError):
            arguments = project_json(None, argument_paths, limits=limits, source_visibility="INVALID")
        if not root and not arguments.complete:
            capabilities["tool_arguments"] = "NONE" if arguments.value is None else "PARTIAL"
        warnings.extend(arguments.warnings)
        output = project_json(
            None if values.get("output", _MISSING) is _MISSING else values["output"],
            checked.redaction_policy.output_paths if root else checked.redaction_policy.result_paths,
            limits=limits,
        )
        result_summary = output.value
        if not output.complete:
            warnings.append("OUTPUT_SUMMARY_UNAVAILABLE")
        workflow = _opaque(values.get("workflow_scope_id"))
        event_id, call_id = _opaque(values.get("event_id")), _opaque(values.get("call_id"))
        sequence = _integer(values.get("sequence"), limits.max_steps)
        predecessors = _ids(values.get("causal_event_ids", _MISSING), limits)
        if not root and (workflow is None or event_id is None or sequence is None or predecessors is None):
            capabilities["ordering"] = "NONE"
            warnings.append("WORKFLOW_IDENTITY_UNAVAILABLE")
        count = terminal_ids = final = None
        if phase == "EVENT" and any(
            values.get(key, _MISSING) is not _MISSING
            for key in ("terminal_event_count", "terminal_event_ids", "terminal_final_sequence")
        ):
            count = _integer(values.get("terminal_event_count"), limits.max_steps, minimum=0)
            terminal_ids = _ids(values.get("terminal_event_ids", _MISSING), limits)
            final = _integer(values.get("terminal_final_sequence"), limits.max_steps)
            if count is None or terminal_ids is None or final is None or count != len(terminal_ids):
                count = terminal_ids = final = None
                warnings.append("TERMINAL_INVENTORY_INVALID")
            else:
                result_summary = {
                    "terminal_inventory": {"event_count": count, "event_ids": terminal_ids, "final_sequence": final}
                }
        for text in (safe_name, workflow, event_id, call_id):
            validate_bounded_json(text, limits)
        step = TrajectoryStep(
            step_id=observation_id,
            observation_id=observation_id,
            parent_step_id=parent,
            index=index,
            type=step_type,
            raw_name=safe_name,
            tool_identity=tool_identity,
            phase=phase,
            status=status,
            started_at=start,
            ended_at=end,
            duration_ms=duration,
            arguments_redacted=arguments.value,
            argument_visibility=arguments.visibility,
            result_summary=result_summary,
            error_code=None,
            causal_predecessor_ids=[],
            workflow_scope_id=workflow,
        )
        return NormalizedObservation(
            step=step,
            capabilities=capabilities,
            warnings=sorted(set(warnings)),
            event_id=event_id,
            call_id=call_id,
            sequence=sequence,
            causal_event_ids=predecessors or [],
            terminal_event_count=count,
            terminal_event_ids=terminal_ids,
            terminal_final_sequence=final,
        )
    except _InvalidField as error:
        return NormalizedObservation(step=None, capabilities=empty, warnings=[str(error)])
    except (ValidationError, ValueError, UnicodeError, RecursionError):
        return NormalizedObservation(step=None, capabilities=empty, warnings=["SOURCE_FIELD_INVALID"])


def _structural_role(profile, values, metadata):
    # 业务语义无论值是否有效都不能退回结构角色，防止漏埋字段被洗成完整结构。
    if type(metadata) is not dict or any(
        values.get(key, _MISSING) is not _MISSING for key in _STRUCTURAL_SEMANTIC_FIELDS
    ):
        return None
    if profile.metadata_equals and all(
        type(metadata.get(key, _MISSING)) is str and metadata[key] == expected
        for key, expected in profile.metadata_equals.items()
    ):
        return None
    return next(
        (
            role
            for role in profile.structural_roles
            if role.provider_type == values["provider_type"]
            and role.raw_name == values["raw_name"]
            and all(
                type(metadata.get(key, _MISSING)) is str and metadata[key] == expected
                for key, expected in role.metadata_equals.items()
            )
        ),
        None,
    )


def _structural_observation(values, role, observation_id, parent, index, limits, empty):
    warnings = []
    start = _optional(values.get("started_at", _MISSING), Timestamp, "STEP_TIME_INVALID", warnings)
    end = _optional(values.get("ended_at", _MISSING), Timestamp, "STEP_TIME_INVALID", warnings)
    if start is not None and end is not None and end < start:
        warnings.append("STEP_TIME_INVALID")
    validate_bounded_json(role.safe_name, limits)
    return NormalizedObservation(
        structural=True,
        step=TrajectoryStep(
            step_id=observation_id,
            observation_id=observation_id,
            parent_step_id=parent,
            index=index,
            type=role.step_type,
            raw_name=role.safe_name,
            tool_identity=None,
            phase="EVENT",
            status="UNKNOWN",
            started_at=start,
            ended_at=end,
            duration_ms=None,
            arguments_redacted=None,
            argument_visibility={},
            result_summary=None,
            error_code=None,
            causal_predecessor_ids=[],
            workflow_scope_id=None,
        ),
        capabilities=empty,
        warnings=sorted(set(warnings)),
    )
