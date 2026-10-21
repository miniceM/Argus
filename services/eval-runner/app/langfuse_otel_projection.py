"""Langfuse v4 experiment projection layer (OTel replay path).

Concrete contract (docs/validation/issue198-token-usage-langfuse-runs.md P1/P2):
Langfuse v4 organizations created after 2026-09-16 expose *no* public REST
endpoint to create a Dataset Run / run item. The legacy
``POST /api/public/dataset-run-items`` returns HTTP 200 on such organizations but
does not project into ``GET /api/public/experiments``, so the run silently stays
invisible. The only supported write path is the OTel trace ingestion endpoint
with the ``langfuse.experiment.*`` span attribute suite.

Because the Argus venv has a composable langfuse SDK whose OTel exporter
namespace is patched by ``opentelemetry-exporter-http-transport``, this module
builds the OTLP protobuf payload itself (``opentelemetry.proto.*`` from
`opentelemetry-api`) and posts it with plain urllib. That keeps the projection
self-contained and independent of SDK version drift.

Hard boundaries:
- credentials never appear in logs, in the DB, in this repo, or in this module.
- retries are never performed here: dedupe and backward merge semantics belong
  to the Langfuse side.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from typing import Any

from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2

logger = logging.getLogger("argus.langfuse_otel_projection")

# Langfuse v4 span-attribute contract (see issue198 probe P1/P2).
EXPERIMENT_ID_ATTR = "langfuse.experiment.id"
EXPERIMENT_NAME_ATTR = "langfuse.experiment.name"
EXPERIMENT_DATASET_ID_ATTR = "langfuse.experiment.dataset.id"
EXPERIMENT_ITEM_ID_ATTR = "langfuse.experiment.item.id"
EXPERIMENT_ITEM_ROOT_OBS_ATTR = "langfuse.experiment.item.root_observation_id"
EXPERIMENT_ITEM_VERSION_ATTR = "langfuse.experiment.item.version"

OTEL_ENDPOINT_PATH = "/api/public/otel/v1/traces"
OTEL_INGESTION_VERSION = "4"
_REPLAY_SPAN_NAME = "argus-run-item-replay"


class LangfuseProjectionError(RuntimeError):
    """Raised for projection errors where the HTTP status, if any, is known."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _attr_str(key: str, value: str) -> common_pb2.KeyValue:
    """One OTel span attribute (string value), matching the Langfuse contract."""
    return common_pb2.KeyValue(key=key, value=common_pb2.AnyValue(string_value=value))


def _hex_to_bytes(value: str, *, length: int, label: str) -> bytes:
    """OTel identity helpers that surface the caller's typo instead of magic."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a 32-hex-character string, got {type(value).__name__}")
    text = value.strip().lower()
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid hex string: {value!r}") from exc
    if len(raw) != length:
        raise ValueError(f"{label} must be a {length * 2}-hex-character string, got {len(raw) * 2}")
    return raw


def build_experiment_replay_request(
    *,
    trace_id: str,
    span_id: str,
    dataset_id: str,
    dataset_item_id: str,
    run_id: str,
    run_name: str,
    dataset_version: str | None = None,
    span_name: str = _REPLAY_SPAN_NAME,
    start_unix_nano: int | None = None,
    attributes: list[tuple[str, str]] | None = None,
) -> bytes:
    """Build the OTLP ``ExportTraceServiceRequest`` for one run item.

    ``trace_id`` / ``span_id`` are the existing Argus-side trace identity so the
    projection merges into the same Langfuse observation tree (P2 probe
    semantics). ``start_unix_nano`` must be pinned in tests so the payload is
    deterministic; the default is "now".
    """
    if start_unix_nano is None:
        start_unix_nano = time.time_ns()
    if start_unix_nano <= 0:
        raise ValueError("start_unix_nano must be a positive nanosecond count")
    try:
        uuid.UUID(str(run_id))
    except ValueError as exc:
        raise ValueError(f"run_id must be a valid UUID, got {run_id!r}") from exc

    span = trace_pb2.Span(
        trace_id=_hex_to_bytes(trace_id, length=16, label="trace_id"),
        span_id=_hex_to_bytes(span_id, length=8, label="span_id"),
        start_time_unix_nano=start_unix_nano,
        end_time_unix_nano=start_unix_nano,
        name=span_name,
    )
    attrs = [
        _attr_str(EXPERIMENT_ID_ATTR, run_id),
        _attr_str(EXPERIMENT_NAME_ATTR, run_name),
        _attr_str(EXPERIMENT_DATASET_ID_ATTR, dataset_id),
        _attr_str(EXPERIMENT_ITEM_ID_ATTR, dataset_item_id),
        _attr_str(EXPERIMENT_ITEM_ROOT_OBS_ATTR, span_id),
    ]
    if dataset_version:
        attrs.append(_attr_str(EXPERIMENT_ITEM_VERSION_ATTR, dataset_version))
    if attributes:
        attrs.extend(_attr_str(key, value) for key, value in attributes)
    span.attributes.extend(attrs)

    scope = common_pb2.InstrumentationScope(name="argus-eval-runner", version="1.0.0")
    scope_span = trace_pb2.ScopeSpans(scope=scope, spans=[span])
    resource = resource_pb2.Resource(
        attributes=[_attr_str("service.name", "argus-eval-runner")]
    )
    resource_spans = trace_pb2.ResourceSpans(resource=resource, scope_spans=[scope_span])
    return trace_service_pb2.ExportTraceServiceRequest(resource_spans=[resource_spans]).SerializeToString()


def projection_http_post(
    url: str,
    payload: bytes,
    headers: dict[str, str],
    timeout_seconds: float,
) -> tuple[int, bytes]:
    """Raw HTTP POST; exposed so tests can stub it without reimplementing urllib."""
    import urllib.request

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as response:  # noqa: S310
        return response.status, response.read()


def post_langfuse_projection(
    payload: bytes,
    *,
    credentials: Any,
    timeout_seconds: float = 30.0,
) -> tuple[int, bytes]:
    """POST an OTLP payload to Langfuse using basic auth (never logs the auth)."""
    import urllib.error

    base_url = str(credentials.base_url or "").rstrip("/")
    if not base_url:
        raise LangfuseProjectionError("Langfuse base URL is required")
    if not getattr(credentials, "public_key", None) or not getattr(credentials, "secret_key", None):
        raise LangfuseProjectionError("Langfuse public key and secret key are required")
    url = f"{base_url}{OTEL_ENDPOINT_PATH}"
    auth_token = base64.b64encode(
        f"{credentials.public_key}:{credentials.secret_key}".encode()
    ).decode("ascii")
    headers = {
        "content-type": "application/x-protobuf",
        "authorization": f"Basic {auth_token}",
        "x-langfuse-ingestion-version": OTEL_INGESTION_VERSION,
    }
    try:
        return projection_http_post(url, payload, headers, timeout_seconds)
    except urllib.error.HTTPError as exc:  # keep the status for callers to classify
        raise LangfuseProjectionError(
            f"Langfuse OTel projection POST failed for {url}: HTTP {exc.code}",
            status_code=exc.code,
        ) from exc
    except LangfuseProjectionError:
        raise
    except Exception as exc:
        raise LangfuseProjectionError(f"Langfuse OTel projection POST failed for {url}: {exc}") from exc


def replay_experiment_item(
    *,
    trace_id: str,
    span_id: str,
    dataset_id: str,
    dataset_item_id: str,
    run_id: str,
    run_name: str,
    dataset_version: str | None = None,
    credentials: Any,
    timeout_seconds: float = 30.0,
    start_unix_nano: int | None = None,
    span_name: str = _REPLAY_SPAN_NAME,
    attributes: list[tuple[str, str]] | None = None,
) -> tuple[int, bytes]:
    """Replay one existing Argus trace as a Langfuse v4 run item."""
    payload = build_experiment_replay_request(
        trace_id=trace_id,
        span_id=span_id,
        dataset_id=dataset_id,
        dataset_item_id=dataset_item_id,
        run_id=run_id,
        run_name=run_name,
        dataset_version=dataset_version,
        start_unix_nano=start_unix_nano,
        span_name=span_name,
        attributes=attributes,
    )
    return post_langfuse_projection(payload, credentials=credentials, timeout_seconds=timeout_seconds)


def is_http_error(exc: BaseException) -> int | None:
    """Best-effort status-code read to fail-closed detection of duplicates."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    return None
