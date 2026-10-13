"""冻结字段映射生成脱敏候选 Step；不证明闭包、因果或完整度。"""

import re
from typing import get_args

from pydantic import Field, TypeAdapter, ValidationError

from .codec import canonical_dumps, canonical_loads
from .contract import TrajectoryContract
from .redaction import project_json, validate_bounded_json
from .schema import (
    Capability,
    CapabilityCoverage,
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
    event_id: Identity | None = None
    call_id: Identity | None = None
    sequence: int | None = Field(default=None, ge=1, le=1000)
    causal_event_ids: list[Identity] = Field(default_factory=list, max_length=1000)
    terminal_event_count: int | None = Field(default=None, ge=0, le=1000)
    terminal_event_ids: list[Identity] | None = Field(default=None, max_length=1000)
    terminal_final_sequence: int | None = Field(default=None, ge=1, le=1000)


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
            encodings = {canonical_dumps(value, max_bytes=max_bytes) for value in values}
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
        return TypeAdapter(adapter).validate_python(value)
    except (ValidationError, ValueError, OverflowError):
        warnings.append(warning)
        return None


def normalize_observation(
    row, contract: TrajectoryContract, scope: TrajectoryScope, *, index: int
) -> NormalizedObservation:
    # 调用边界重新校验可变配置；声明选择仍从已绑定合同读取，不能来自 Trace。
    checked = TrajectoryContract.model_validate(contract.model_dump())
    profile_id = contract.source_profile.profile_id
    profile = next(profile for profile in checked.source_profile_set if profile.profile_id == profile_id)
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
        if not arguments.complete:
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
