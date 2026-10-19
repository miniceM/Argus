from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel

from .execution import get_langfuse_client_safe

logger = logging.getLogger("argus.result_outputs")
MAX_OUTPUT_CHARS = 20_000


class OutputSideResponse(BaseModel):
    output_status: Literal["AVAILABLE", "NO_REFERENCE", "NOT_FOUND", "FETCH_FAILED"]
    output: Any | None = None
    reason: str | None = None
    retryable: bool = False
    truncated: bool = False
    scores: dict[str, Any] = {}
    trace_url: str | None = None


class ComparisonCaseOutputResponse(BaseModel):
    launch_id: str
    candidate_snapshot_id: str
    baseline_snapshot_id: str | None = None
    dataset_item_id: str
    classification: str
    reason: str | None = None
    baseline: OutputSideResponse
    candidate: OutputSideResponse


def _safe_output(raw: Any) -> tuple[Any, bool]:
    output = raw
    if isinstance(raw, str):
        try:
            output = json.loads(raw)
        except (TypeError, ValueError):
            output = raw
    try:
        serialized = json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        serialized = str(output)
    if len(serialized) > MAX_OUTPUT_CHARS:
        return serialized[:MAX_OUTPUT_CHARS] + "…", True
    return output, False


def fetch_observation_output(
    output_ref: Any,
    *,
    scores: dict[str, Any] | None = None,
    trace_url: str | None = None,
    client: Any | None = None,
) -> OutputSideResponse:
    """Read only the Observation frozen into a result Snapshot; never accept caller-provided refs."""
    base = {"scores": scores or {}, "trace_url": trace_url}
    if not isinstance(output_ref, dict):
        return OutputSideResponse(output_status="NO_REFERENCE", reason="NO_OUTPUT_REFERENCE", **base)
    observation_id = output_ref.get("observation_id")
    trace_id = output_ref.get("trace_id")
    if not isinstance(observation_id, str) or not observation_id.strip() or not isinstance(trace_id, str) or not trace_id.strip():
        return OutputSideResponse(output_status="NO_REFERENCE", reason="NO_OUTPUT_REFERENCE", **base)

    if "cached_output" in output_ref and output_ref["cached_output"] is not None:
        output, truncated = _safe_output(output_ref["cached_output"])
        return OutputSideResponse(
            output_status="AVAILABLE", output=output, truncated=truncated, **base
        )

    langfuse = client if client is not None else get_langfuse_client_safe()
    observations = getattr(getattr(getattr(langfuse, "api", None), "observations", None), "get_many", None)
    if not callable(observations):
        return OutputSideResponse(
            output_status="FETCH_FAILED", reason="LANGFUSE_UNAVAILABLE", retryable=True, **base
        )

    filters = [
        {"type": "string", "column": "id", "operator": "=", "value": observation_id},
        {"type": "string", "column": "traceId", "operator": "=", "value": trace_id},
    ]
    try:
        response = observations(
            fields="core,io",
            filter=json.dumps(filters),
            limit=10,
            request_options={"timeout_in_seconds": 15, "max_retries": 2},
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404:
            return OutputSideResponse(
                output_status="NOT_FOUND", reason="OBSERVATION_NOT_FOUND", retryable=True, **base
            )
        # Keep upstream exception text/headers and any payload out of logs and responses.
        logger.warning("Langfuse observation lookup failed (%s)", type(exc).__name__)
        return OutputSideResponse(
            output_status="FETCH_FAILED", reason="UPSTREAM_ERROR", retryable=True, **base
        )

    data = getattr(response, "data", None)
    matching = [
        item for item in (data or [])
        if getattr(item, "id", None) == observation_id and getattr(item, "trace_id", None) == trace_id
    ]
    if not matching:
        return OutputSideResponse(
            output_status="NOT_FOUND", reason="OBSERVATION_NOT_FOUND", retryable=True, **base
        )
    if len(matching) == 1:
        target_item = matching[0]
    else:
        # Multiple records can share the same (id, trace_id), e.g. an OTel projection SPAN
        # alongside the actual business TOOL/CHAIN observation. Prioritize records with output.
        with_output = [item for item in matching if getattr(item, "output", None) is not None]
        if len(with_output) == 1:
            target_item = with_output[0]
        elif len(with_output) > 1:
            first_out = getattr(with_output[0], "output", None)
            if all(getattr(item, "output", None) == first_out for item in with_output):
                target_item = with_output[0]
            else:
                return OutputSideResponse(
                    output_status="FETCH_FAILED", reason="AMBIGUOUS_OBSERVATION_REFERENCE", retryable=False, **base
                )
        else:
            non_span = [item for item in matching if getattr(item, "type", None) != "SPAN"]
            target_item = non_span[0] if non_span else matching[0]

    output, truncated = _safe_output(getattr(target_item, "output", None))
    return OutputSideResponse(
        output_status="AVAILABLE", output=output, truncated=truncated, **base
    )

