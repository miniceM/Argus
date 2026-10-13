"""S4.0 观测链路原语：W3C traceparent 解析、同 Trace 判定、字段可用性审计.

本模块是只读实测工具，不参与评测判定，不写 Langfuse，
不产生 Assembler 或规则。S4.1 的 Assembler 将复用这里的解析与审计语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Assembler 必需字段。缺失其中任一即视为证据不完整，
# 但全部缺失仍可容忍为 DEGRADED_ONLY_ROOT（仅有根节点）。
REQUIRED_OBSERVATION_FIELDS: tuple[str, ...] = (
    "observation_id",
    "trace_id",
    "type",
    "name",
    "started_at",
    "ended_at",
)


@dataclass(frozen=True)
class ParsedTraceparent:
    """解析后的 W3C traceparent（https://www.w3.org/TR/trace-context/）."""

    version: str
    trace_id: str
    parent_span_id: str
    flags: str


def parse_traceparent(value: str) -> ParsedTraceparent:
    """解析并校验 W3C traceparent，格式非法时抛 ValueError."""
    parts = (value or "").strip().split("-")
    if len(parts) != 4:
        raise ValueError(f"traceparent 必须为 version-traceid-spanid-flags 四段式，实际：{value!r}")
    version, trace_id, span_id, flags = parts
    if len(trace_id) != 32 or not _is_hex(trace_id):
        raise ValueError(f"trace_id 必须为 32 位十六进制，实际：{trace_id!r}")
    if len(span_id) != 16 or not _is_hex(span_id):
        raise ValueError(f"parent span id 必须为 16 位十六进制，实际：{span_id!r}")
    return ParsedTraceparent(
        version=version, trace_id=trace_id.lower(), parent_span_id=span_id.lower(), flags=flags
    )


def _is_hex(text: str) -> bool:
    try:
        int(text, 16)
        return True
    except ValueError:
        return False


def same_trace(first: ParsedTraceparent, second: ParsedTraceparent) -> bool:
    """两次观测是否归属同一 Trace：只比较 trace_id.

    内部转发允许更换 parent span id（LangGraph 风格 router span），
    但 trace_id 必须不变，否则 Assembler 无法跨框架归一。
    """
    return first.trace_id == second.trace_id


def audit_observation_fields(observation: dict[str, Any]) -> dict[str, Any]:
    """审计单个 Observation 的字段可用性，输出 S4.1 需要的缺失清单.

    返回 ``{"missing": [...], "tolerable": bool}``：
    全部字段齐全则缺失为空；仅有根标识（observation_id/trace_id）
    而缺业务字段时仍可容忍为 DEGRADED_ONLY_ROOT。
    """
    if not isinstance(observation, dict):
        return {"missing": list(REQUIRED_OBSERVATION_FIELDS), "tolerable": False}
    missing = [field for field in REQUIRED_OBSERVATION_FIELDS if not observation.get(field)]
    has_root = bool(observation.get("observation_id")) and bool(observation.get("trace_id"))
    return {"missing": missing, "tolerable": (not missing) or has_root}


def summarize_field_matrix(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总一批 Observation 的字段审计，输出 S4.1 可消费的可用性矩阵报告.

    ``must_tolerate`` 是所有可容忍行缺失字段的并集，即 Assembler
    必须能容忍缺失的字段集合；任一行不可容忍（连根标识都没有）
    会在该行以 ``tolerable: False`` 标出，但不污染并集。
    非字典行按空观测处理，避免一条脏数据打断整批审计。
    """
    normalized = rows if isinstance(rows, list) else []
    audited = []
    for index, row in enumerate(normalized):
        record = row if isinstance(row, dict) else {}
        audited.append(
            {
                "name": record.get("name") or record.get("observation_id") or f"row-{index}",
                **audit_observation_fields(record),
            }
        )
    must_tolerate = sorted(
        {field for row in audited if row["tolerable"] for field in row["missing"]}
    )
    return {"rows": audited, "must_tolerate": must_tolerate}
