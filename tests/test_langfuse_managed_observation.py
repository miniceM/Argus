"""Issue #214: native HTTP IO versus metadata-only OTLP replay contracts.

These are local contracts with a fake SDK, not proof of hosted evaluation.
The accompanying validation record contains separate real Cloud evidence.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services/eval-runner")]

from app.db_models import ExecutionCheckpointRecord, ExperimentItemExecutionRecord, LangfuseSyncTaskRecord  # noqa: E402
from app.executor import RemoteAgentExecutor, SingleInvocationResult  # noqa: E402
from app.langfuse_otel_projection import build_experiment_replay_request  # noqa: E402


def test_worker_records_native_http_io_and_checkpoint_without_ground_truth(setup_runtime, monkeypatch):
    db, queue, _limiter, orchestrator, worker, _reconciler = setup_runtime
    expected = {"answer": "private-reference-must-not-be-in-agent-request"}
    native = {"text": "1266", "usedTools": [{"tool": "calculator", "toolOutput": "1266"}]}
    manifest = {
        "agent": {"agent_id": "test-agent", "version": "v1", "agent_version_id": "test-agent-v1",
                  "endpoint": "http://localhost/invoke", "method": "POST", "is_idempotent": False,
                  "request_mapping": {"question": "input.question"}},
        "execution_policy": {"timeout_seconds": 60, "max_retries": 0, "max_concurrency": 1,
                             "rate_limit_per_minute": 60},
        "dataset": {"source": "seed", "items": [{"id": "case-214", "input": {"question": "compute"},
                                                  "expected_output": expected}]},
        "evaluators": [{"id": "answer_present", "version": "1.0.0", "scope": "item"}],
    }
    observations = []
    class Observation:
        def __init__(self, **kwargs):
            self.data = kwargs
            observations.append(self.data)
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def update(self, **kwargs): self.data.update(kwargs)
    class SDK:
        def start_as_current_observation(self, **kwargs): return Observation(**kwargs)
        def get_current_trace_id(self): return "a" * 32
        def get_current_observation_id(self): return "b" * 16
        def get_trace_url(self, **_): return "https://example.test/trace"
    monkeypatch.setattr("app.worker.get_langfuse_client_safe", lambda: SDK())
    monkeypatch.setattr("app.worker.inject", lambda headers: headers.update({"traceparent": f"00-{'a' * 32}-{'b' * 16}-01"}))
    invoke = AsyncMock(return_value=SingleInvocationResult(
        status_code=200, body=native, raw_response='{"text":"1266"}', headers={}, duration_ms=10,
        trace_context_received=False, error_category=None, error_message=None,
        is_retryable=False, may_have_side_effects=False))
    monkeypatch.setattr(RemoteAgentExecutor, "invoke_once", invoke)
    launch = orchestrator.create_launch("test-agent", "v1", "dataset", "fixture-v1", "issue214", manifest)
    orchestrator.start_launch(launch.id)
    message = queue.read_group("issue214", count=1)[0]
    assert asyncio.run(worker.execute_item_message(*message)) is True
    assert invoke.await_count == 1
    payload, headers = invoke.call_args.args
    assert payload == {"question": "compute"}
    assert headers["traceparent"] == f"00-{'a' * 32}-{'b' * 16}-01"
    business = [row for row in observations if row["name"] == "remote-agent-http"]
    assert len(business) == 1 and business[0]["as_type"] == "tool"
    assert business[0]["input"] == {"agent": "test-agent:v1", "request": payload}
    assert business[0]["output"] == native and "answer" not in business[0]["output"]
    assert "private-reference" not in str(business[0])
    with db.get_session() as session:
        item = session.scalar(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id))
        checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.item_execution_id == item.id))
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.item_id == item.id))
        assert item.execution_status == "succeeded"
        assert checkpoint.agent_output == native and checkpoint.expected_output == expected
        assert item.trace_id == checkpoint.trace_id == task.trace_id == "a" * 32
        assert item.observation_id == checkpoint.observation_id == task.observation_id == "b" * 16
        assert task.dataset_item_id == item.dataset_item_id == "case-214"


def test_metadata_replay_has_stable_identity_but_is_not_a_second_business_tool():
    kwargs = {"trace_id": "a" * 32, "span_id": "b" * 16, "dataset_id": "dataset-214",
              "dataset_item_id": "item-214", "run_id": "30000000-0000-4000-8000-000000000214",
              "run_name": "argus-214", "dataset_version": "2026-10-19T00:00:00Z", "start_unix_nano": 1}
    payload = build_experiment_replay_request(**kwargs)
    assert payload == build_experiment_replay_request(**kwargs)
    request = trace_service_pb2.ExportTraceServiceRequest.FromString(payload)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    attrs = {a.key: a.value.string_value for a in span.attributes}
    assert span.trace_id.hex() == kwargs["trace_id"] and span.span_id.hex() == kwargs["span_id"]
    assert span.name == "argus-run-item-replay" and span.name != "remote-agent-http"
    assert attrs["langfuse.experiment.id"] == kwargs["run_id"]
    assert attrs["langfuse.experiment.dataset.id"] == kwargs["dataset_id"]
    assert attrs["langfuse.experiment.item.id"] == kwargs["dataset_item_id"]
    assert attrs["langfuse.experiment.item.root_observation_id"] == kwargs["span_id"]
    assert attrs["langfuse.experiment.item.version"] == kwargs["dataset_version"]
    assert "langfuse.observation.input" not in attrs and "langfuse.observation.output" not in attrs
    assert attrs.get("langfuse.observation.type") != "tool"
