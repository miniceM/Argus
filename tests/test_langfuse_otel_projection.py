"""Issue 198 S6.2 方向 A：Langfuse v4 run-item 投影层（OTel replay 路径）。

合同来源（docs/validation/issue198-token-usage-langfuse-runs.md P1/P2）：
- v4 新组织不能用 legacy `/dataset-run-items`（200 但不投影）。
- OTel POST（`/api/public/otel/v1/traces`）+ `langfuse.experiment.*` span 属性
  一次写入即物化 run + run item。
- 重复重放同 trace / 同 span id 由 Langfuse backward merge 语义处理；本地只按
  HTTP status / duplicate 文案把 409 归类为 SKIP，不再重试炸出。
- 不依赖 langfuse SDK 的 OTel exporter shim，改用手工构建 `opentelemetry.proto`
  protobuf + 原生 HTTP POST。
- 凭据只从运行时参数走（与 backfill 脚本相同边界），不读取 SDK 内部字段。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.langfuse_otel_projection import (  # noqa: E402
    LangfuseProjectionError,
    build_experiment_replay_request,
    post_langfuse_projection,
    replay_experiment_item,
)

_CREDENTIALS = SimpleNamespace(
    base_url="https://langfuse.example.test",
    public_key="pk-fake",
    secret_key="sk-fake",
)

_PAYLOAD = {
    "trace_id": "8dbe7811b119e45872bb29ce30d9b7e5",
    "span_id": "88b26ef71792fbe9",
    "dataset_id": "cmu5gbnus0006ad0kjc2u7r1k",
    "dataset_item_id": "00000000-0000-4000-8000-000000000001",
    "run_id": "fb7f61b8-761e-4ce7-b980-880c58fa51d4",
    "run_name": "issue198-token-e2e-v3",
    "dataset_version": "2026-10-15T08:00:00+00:00",
}


def _decode_readable_attrs(span):
    """把 protobuf AnyValue 解回可读 dict。"""
    out = {}
    for attr in span.attributes:
        out[attr.key] = getattr(attr.value, attr.value.WhichOneof("value"), None)
    return out


def _parse_request(body: bytes) -> trace_service_pb2.ExportTraceServiceRequest:
    request = trace_service_pb2.ExportTraceServiceRequest()
    request.ParseFromString(body)
    return request


# ---------------------------------------------------------------------------
# payload 工厂
# ---------------------------------------------------------------------------


def test_build_experiment_replay_request_carries_langfuse_contract_attributes():
    payload = build_experiment_replay_request(**_PAYLOAD, start_unix_nano=1_700_000_000_000_000_000)
    assert isinstance(payload, bytes) and payload
    request = _parse_request(payload)
    assert len(request.resource_spans) == 1
    assert len(request.resource_spans[0].scope_spans) == 1
    assert len(request.resource_spans[0].scope_spans[0].spans) == 1
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id.hex() == _PAYLOAD["trace_id"]
    assert span.span_id.hex() == _PAYLOAD["span_id"]
    attrs = _decode_readable_attrs(span)
    assert attrs["langfuse.experiment.id"] == _PAYLOAD["run_id"]
    assert attrs["langfuse.experiment.name"] == _PAYLOAD["run_name"]
    assert attrs["langfuse.experiment.dataset.id"] == _PAYLOAD["dataset_id"]
    assert attrs["langfuse.experiment.item.id"] == _PAYLOAD["dataset_item_id"]
    assert attrs["langfuse.experiment.item.root_observation_id"] == _PAYLOAD["span_id"]
    assert attrs["langfuse.experiment.item.version"] == _PAYLOAD["dataset_version"]


def test_build_experiment_replay_request_deterministic_when_pinned():
    a = build_experiment_replay_request(**_PAYLOAD, start_unix_nano=1)
    b = build_experiment_replay_request(**_PAYLOAD, start_unix_nano=1)
    assert a == b


def test_build_experiment_replay_request_requires_uuid_shape_run_id():
    with pytest.raises(ValueError):
        build_experiment_replay_request(
            **{**_PAYLOAD, "run_id": "not-a-uuid"}, start_unix_nano=1
        )


# ---------------------------------------------------------------------------
# HTTP POST 路径
# ---------------------------------------------------------------------------


class _FakePost:
    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200
        self.body = b"ok"
        self.error: Exception | None = None

    def __call__(self, url, payload, headers, timeout_seconds):
        self.calls.append(
            {"url": url, "payload": payload, "headers": dict(headers), "timeout_seconds": timeout_seconds}
        )
        if self.error is not None:
            raise self.error
        return self.status, self.body


def test_post_langfuse_projection_posts_otel_contract(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr("app.langfuse_otel_projection.projection_http_post", fake)
    payload = b"\x01\x02"
    post_langfuse_projection(payload, credentials=_CREDENTIALS, timeout_seconds=17.5)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://langfuse.example.test/api/public/otel/v1/traces"
    assert call["headers"].get("x-langfuse-ingestion-version") == "4"
    assert "content-type" in call["headers"]
    assert call["headers"].get("authorization", "").startswith("Basic ")
    assert call["timeout_seconds"] == 17.5
    assert call["payload"] == payload


def test_post_langfuse_projection_raises_with_status_on_http_error(monkeypatch):
    fake = _FakePost()
    fake.error = LangfuseProjectionError("method not allowed", status_code=405)
    monkeypatch.setattr("app.langfuse_otel_projection.projection_http_post", fake)
    with pytest.raises(LangfuseProjectionError) as excinfo:
        post_langfuse_projection(b"\x01\x02", credentials=_CREDENTIALS, timeout_seconds=5)
    assert excinfo.value.status_code == 405


def test_replay_experiment_item_builds_and_posts(monkeypatch):
    fake = _FakePost()
    monkeypatch.setattr("app.langfuse_otel_projection.projection_http_post", fake)
    replay_experiment_item(
        trace_id=_PAYLOAD["trace_id"],
        span_id=_PAYLOAD["span_id"],
        dataset_id=_PAYLOAD["dataset_id"],
        dataset_item_id=_PAYLOAD["dataset_item_id"],
        run_id=_PAYLOAD["run_id"],
        run_name=_PAYLOAD["run_name"],
        dataset_version=_PAYLOAD["dataset_version"],
        credentials=_CREDENTIALS,
    )
    assert len(fake.calls) == 1
    body = fake.calls[0]["payload"]
    request = _parse_request(body)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id.hex() == _PAYLOAD["trace_id"]
