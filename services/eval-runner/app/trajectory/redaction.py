"""只投影获准标量路径；完整采集、字段缺失与隐藏是不同事实。"""

from decimal import Decimal

from pydantic import Field

from .codec import canonical_dumps, canonical_loads, validate_json_value
from .contract import RedactionPolicy, TrajectoryLimits
from .paths import compile_jsonpath
from .schema import ArgumentVisibility, BoundedJson, ReasonCode, _ContractModel


class JsonProjection(_ContractModel):
    value: BoundedJson
    visibility: dict[str, ArgumentVisibility]
    complete: bool
    warnings: list[ReasonCode] = Field(default_factory=list)


def validate_bounded_json(value, limits, depth=0):
    if depth == 0:
        validate_json_value(value)
    if depth > limits.max_json_depth:
        raise ValueError("TRAJECTORY_JSON_DEPTH_LIMIT")
    if type(value) is str and len(value.encode("utf-8")) > limits.max_string_bytes:
        raise ValueError("TRAJECTORY_JSON_STRING_LIMIT")
    if type(value) is int and len(str(abs(value))) > limits.max_numeric_digits:
        raise ValueError("TRAJECTORY_JSON_NUMBER_LIMIT")
    if type(value) is Decimal and (
        len(value.as_tuple().digits) > limits.max_numeric_digits or value.adjusted() >= limits.max_numeric_digits
    ):
        raise ValueError("TRAJECTORY_JSON_NUMBER_LIMIT")
    if type(value) is dict:
        for key, child in value.items():
            validate_bounded_json(key, limits)
            validate_bounded_json(child, limits, depth + 1)
    elif type(value) is list:
        for child in value:
            validate_bounded_json(child, limits, depth + 1)


def _resolve(value, tokens, *, scalar_arrays=False):
    for token in tokens:
        if type(token) is str:
            if type(value) is not dict:
                return "INVALID", None
            if token not in value:
                return "MISSING", None
        else:
            if type(value) is not list:
                return "INVALID", None
            if token >= len(value):
                return "MISSING", None
        value = value[token]
    if (type(value) is list and scalar_arrays and len(value) <= 1000
        and all(type(item) in (str, bool, int, Decimal, type(None)) for item in value)):
        return "AVAILABLE", value
    if type(value) in (dict, list):
        return "INVALID", None  # 不允许一个获准容器把未获准子字段整体带入证据。
    return "AVAILABLE", value


def _insert(target, tokens, value):
    token = tokens[0]
    if type(token) is int:
        # 最大扩展受实际来源数组及制品预算限制，不按任意 JSONPath 数字分配。
        if token >= 1000:
            raise ValueError("TRAJECTORY_PROJECTION_ARRAY_LIMIT")
        while len(target) <= token:
            target.append(None)
    if len(tokens) == 1:
        target[token] = value
        return
    child = target.get(token) if type(target) is dict else target[token]
    if child is None:
        child = {} if type(tokens[1]) is str else []
        target[token] = child
    _insert(child, tokens[1:], value)


def project_json(
    value, paths, *, limits: TrajectoryLimits, hidden_paths=(), source_visibility="AVAILABLE", content_policy_version=None,
    string_policies=None,
) -> JsonProjection:
    limits = TrajectoryLimits.model_validate(limits.model_dump())
    policy = RedactionPolicy(
        version="projection-v1", input_paths=[], output_paths=[], argument_paths=paths, result_paths=[],
        content_policy_version=content_policy_version,
        string_policies={path: kind for path, kind in string_policies.items() if path in paths} if string_policies is not None else None,
    )
    paths = policy.argument_paths
    compiled = {path: compile_jsonpath(path) for path in paths}
    hidden = [compile_jsonpath(path) for path in hidden_paths]
    if any(len(tokens) > limits.max_jsonpath_segments for tokens in [*compiled.values(), *hidden]):
        raise ValueError("TRAJECTORY_CONTRACT_PATH_SEGMENT_LIMIT")
    if type(source_visibility) is str and source_visibility == "REDACTED" and not hidden:
        return JsonProjection(value={}, visibility={path: "REDACTED" for path in paths}, complete=False)
    if type(source_visibility) is str and source_visibility == "UNCOLLECTED":
        return JsonProjection(value=None, visibility={}, complete=False, warnings=["ARGUMENTS_NOT_COLLECTED"])
    if type(source_visibility) is not str or source_visibility not in {"AVAILABLE", "REDACTED", "MISSING"}:
        return JsonProjection(
            value={}, visibility={path: "INVALID" for path in paths}, complete=False, warnings=["ARGUMENTS_INVALID"]
        )
    try:
        if type(value) is str:
            parsed = canonical_loads(value, max_bytes=limits.max_source_bytes)
        elif value is None:
            parsed = None
        else:
            # 领域 codec 可以编码受控模型；外部来源必须先证明是精确 JSON 值。
            validate_bounded_json(value, limits)
            parsed = canonical_loads(
                canonical_dumps(value, max_bytes=limits.max_source_bytes), max_bytes=limits.max_source_bytes
            )
        if parsed is None and value is None:
            if source_visibility == "REDACTED":
                visibility = {
                    path: "REDACTED" for path, tokens in compiled.items()
                    if any(tokens[: len(prefix)] == prefix for prefix in hidden)
                }
                return JsonProjection(
                    value={} if visibility else None, visibility=visibility, complete=False,
                    warnings=[] if visibility and len(visibility) == len(paths) else ["ARGUMENTS_NOT_COLLECTED"],
                )
            return JsonProjection(value=None, visibility={}, complete=False, warnings=["ARGUMENTS_NOT_COLLECTED"])
        if type(parsed) not in (dict, list):
            raise ValueError("TRAJECTORY_ARGUMENTS_SHAPE_INVALID")
        validate_bounded_json(parsed, limits)
    except (ValueError, UnicodeError, RecursionError):
        return JsonProjection(
            value={}, visibility={path: "INVALID" for path in paths}, complete=False, warnings=["ARGUMENTS_INVALID"]
        )
    projected = {} if type(parsed) is dict else []
    visibility = {}
    for path, tokens in compiled.items():
        if any(tokens[: len(prefix)] == prefix for prefix in hidden):
            visibility[path] = "REDACTED"
            continue
        visibility[path], selected = _resolve(parsed, tokens, scalar_arrays=content_policy_version is not None)
        if visibility[path] == "AVAILABLE":
            if content_policy_version is not None:
                from .privacy import sensitive_value, strings_allowed
                if sensitive_value(selected) or not strings_allowed(selected, (policy.string_policies or {}).get(path)):
                    visibility[path] = "REDACTED"
                    continue
            try:
                _insert(projected, tokens, selected)
            except ValueError:
                visibility[path] = "INVALID"
    try:
        canonical_dumps(projected, max_bytes=limits.max_artifact_bytes)
    except ValueError:
        return JsonProjection(
            value={}, visibility={path: "INVALID" for path in paths}, complete=False, warnings=["ARGUMENTS_INVALID"]
        )
    return JsonProjection(
        value=projected,
        visibility=visibility,
        complete=all(state in {"AVAILABLE", "MISSING"} for state in visibility.values()),
    )
