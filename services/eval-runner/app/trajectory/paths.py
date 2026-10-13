"""仅编译静态 JSONPath，不访问对象属性或执行表达式。"""

import re

MAX_JSONPATH_SEGMENTS = 32
_MEMBER = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")
_INDEX = re.compile(r"\[(0|[1-9][0-9]{0,63})\]")


def compile_jsonpath(path: str) -> tuple[str | int, ...]:
    if not isinstance(path, str) or not path.startswith("$") or len(path) > 4096:
        raise ValueError("TRAJECTORY_JSONPATH_INVALID")
    tokens: list[str | int] = []
    offset = 1
    while offset < len(path):
        member = _MEMBER.match(path, offset)
        index = _INDEX.match(path, offset)
        match = member or index
        if match is None:
            raise ValueError("TRAJECTORY_JSONPATH_INVALID")
        tokens.append(match[1] if member else int(match[1]))
        offset = match.end()
        if len(tokens) > MAX_JSONPATH_SEGMENTS:
            raise ValueError("TRAJECTORY_JSONPATH_SEGMENT_LIMIT")
    return tuple(tokens)
