"""局部 canonical JSON：保留 Decimal 数值，不改变 ORM/既有 API serializer。"""

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from typing import Any

from pydantic import BaseModel

MAX_JSON_DEPTH = 32
MAX_STRING_BYTES = 4096
MAX_NUMERIC_DIGITS = 64
MAX_NUMERIC_EXPONENT = 64
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def _utf8(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise ValueError("TRAJECTORY_JSON_INVALID_UTF8") from None


def validate_json_value(value: Any, depth: int = 0) -> Any:
    """只校验值与容量；allowlist/内容脱敏必须由上游 redaction 完成。"""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("TRAJECTORY_JSON_DEPTH_LIMIT")
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        if len(_utf8(value)) > MAX_STRING_BYTES:
            raise ValueError("TRAJECTORY_JSON_STRING_LIMIT")
    elif type(value) is int:
        # 先按 bit_length 拒绝巨整数，避免 str() 触发 Python 的十进制转换限额。
        if value.bit_length() > 213 or len(str(abs(value))) > MAX_NUMERIC_DIGITS:
            raise ValueError("TRAJECTORY_JSON_NUMBER_LIMIT")
    elif type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("TRAJECTORY_JSON_NONFINITE_NUMBER")
        _, digits, exponent = value.as_tuple()
        if len(digits) > MAX_NUMERIC_DIGITS or abs(exponent) > MAX_NUMERIC_EXPONENT or value.adjusted() >= 64:
            raise ValueError("TRAJECTORY_JSON_NUMBER_LIMIT")
    elif type(value) is list:
        for child in value:
            validate_json_value(child, depth + 1)
    elif type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("TRAJECTORY_JSON_INVALID_KEY")
            validate_json_value(key)
            validate_json_value(child, depth + 1)
    else:
        raise ValueError("TRAJECTORY_JSON_UNSUPPORTED_TYPE")
    return value


def validate_json_depth(value: Any, depth: int = 0) -> None:
    """校验完整文档深度，包含领域模型的外层结构；datetime 按标量计。"""
    if depth > MAX_JSON_DEPTH:
        raise ValueError("TRAJECTORY_JSON_DEPTH_LIMIT")
    if type(value) is dict:
        for child in value.values():
            validate_json_depth(child, depth + 1)
    elif type(value) is list:
        for child in value:
            validate_json_depth(child, depth + 1)


def _encode_chunks(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("TRAJECTORY_JSON_DEPTH_LIMIT")
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("TRAJECTORY_JSON_NAIVE_TIMESTAMP")
        yield json.dumps(value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"))
    elif type(value) is dict:
        for key in value:
            validate_json_value(key)
            if type(key) is not str:
                raise ValueError("TRAJECTORY_JSON_INVALID_KEY")
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False) + ":"
            yield from _encode_chunks(value[key], depth + 1)
        yield "}"
    elif type(value) is list:
        yield "["
        for index, child in enumerate(value):
            if index:
                yield ","
            yield from _encode_chunks(child, depth + 1)
        yield "]"
    else:
        validate_json_value(value, depth)
        if type(value) is Decimal:
            encoded = "0" if value.is_zero() else format(value, "f")
            yield encoded.rstrip("0").rstrip(".") if "." in encoded else encoded
        else:
            yield json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def canonical_dumps(value: Any, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> str:
    chunks = []
    size = 0
    for chunk in _encode_chunks(value):
        size += len(_utf8(chunk))
        if size > max_bytes:
            raise ValueError("TRAJECTORY_JSON_BYTE_LIMIT")
        chunks.append(chunk)
    return "".join(chunks)


def _check_depth(raw: str) -> None:
    """在 json.loads 前限界嵌套；字符串中的括号和转义不计入深度。"""
    depth = 0
    in_string = escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            # 根容器值深度为 0；容器计数比值深度多一层。
            if depth > MAX_JSON_DEPTH + 1:
                raise ValueError("TRAJECTORY_JSON_DEPTH_LIMIT")
        elif char in "]}":
            depth -= 1


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("TRAJECTORY_JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("TRAJECTORY_JSON_NONFINITE_NUMBER")


def canonical_loads(raw: str, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> Any:
    if type(raw) is not str:
        raise ValueError("TRAJECTORY_JSON_UNSUPPORTED_TYPE")
    if len(_utf8(raw)) > max_bytes:
        raise ValueError("TRAJECTORY_JSON_BYTE_LIMIT")
    _check_depth(raw)
    try:
        value = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError, DecimalException) as error:
        code = str(error)
        if not code.startswith("TRAJECTORY_JSON_"):
            code = "TRAJECTORY_JSON_INVALID"
        raise ValueError(code) from None
    return validate_json_value(value)


def canonical_digest(value: Any) -> str:
    """哈希仅适用于获准语义数据；不是敏感值脱敏或 trajectory 自身摘要构造器。"""
    return "sha256:" + hashlib.sha256(_utf8(canonical_dumps(value))).hexdigest()
