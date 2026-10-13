"""同步调用必须把当前 HTTP span 写入 traceparent，且保持无追踪模式。"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.execution import _execute_single_item
from app.observation_link import parse_traceparent
from app.registry import AgentVersionSpec
from app.runner_identity import current_runner_identity
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


@pytest.mark.parametrize("with_observation", [True, False])
def test_sync_header_parent_is_current_http_span(setup_runtime, monkeypatch, with_observation):
    db, _queue, _limiter, orchestrator, _worker, _reconciler = setup_runtime
    manifest = {"runner": current_runner_identity().model_dump()}
    launch = orchestrator.create_launch("test-agent", "v1", "sync-parent", "v1", "Sync parent", manifest)
    spec = AgentVersionSpec(agent_id="test-agent", version="v1", endpoint="http://127.0.0.1/invoke",
                            method="POST", timeout_seconds=5, rate_limit_per_minute=60,
                            request_mapping={}, max_retries=0)
    provider = TracerProvider()
    tracer = provider.get_tracer("sync-parent-regression")
    captured = {}

    class ObservationClient:
        @contextmanager
        def start_as_current_observation(self, **kwargs):
            with tracer.start_as_current_span(kwargs["name"]) as span:
                captured["http_span_id"] = f"{span.get_span_context().span_id:016x}"
                yield SimpleNamespace(update=lambda **kwargs: None)
        def get_current_trace_id(self):
            return f"{trace.get_current_span().get_span_context().trace_id:032x}"
        def get_current_observation_id(self):
            return f"{trace.get_current_span().get_span_context().span_id:016x}"

    class Executor:
        async def invoke(self, payload, headers, **kwargs):
            captured["header"] = parse_traceparent(headers["traceparent"])
            captured["parent_while_invoking"] = f"{trace.get_current_span().get_span_context().span_id:016x}"
            captured["dataset_item"] = headers["X-Eval-Dataset-Item-Id"]
            return SimpleNamespace(body={"intent": "balance"}, status_code=200, attempts=1,
                                   duration_ms=1, trace_context_received=True)

    monkeypatch.setattr("app.execution.evaluate_frozen_item", lambda *args, **kwargs: SimpleNamespace(
        eval_status="succeeded", eval_error=None, scores={}, quality_conclusion="unknown",
        typed_results=[], quality_decision=None))
    async def run():
        with tracer.start_as_current_span("outer") as outer:
            captured["outer_id"] = f"{outer.get_span_context().span_id:016x}"
            captured["trace_id"] = f"{outer.get_span_context().trace_id:032x}"
            await _execute_single_item(db, Executor(), launch.id,
                                      {"id": "case-1", "input": {}, "expected_output": {}},
                                      spec, manifest, ObservationClient() if with_observation else None)
    try:
        asyncio.run(run())
    finally:
        provider.shutdown()
    assert captured["dataset_item"] == "case-1"
    assert captured["header"].trace_id == captured["trace_id"]
    assert captured["header"].parent_span_id == captured["parent_while_invoking"]
    if with_observation:
        assert captured["header"].parent_span_id == captured["http_span_id"] != captured["outer_id"]
    else:
        assert captured["header"].parent_span_id == captured["outer_id"]
