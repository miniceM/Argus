"""The optional controlled OTLP replayer must never become a Score writer."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("managed_replay", ROOT / "docs/validation/issue214-managed-evaluation/controlled_replay.py")
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


@pytest.mark.parametrize("case", ["duplicate", "rule-miss", "invalid-io", "wrong-answer"])
def test_controlled_cases_are_otlp_tool_inputs_not_score_requests(case):
    run = {"trace_id": "a" * 32, "observation_id": "b" * 16, "environment": "isolated"}
    business = {"startTime": "2026-10-19T00:00:00Z", "input": json.dumps({"request": {"question": "compute"}}),
                "output": json.dumps({"text": "1266"})}
    plan, payload = replay.build_case(case, run, business)
    request = trace_service_pb2.ExportTraceServiceRequest.FromString(payload)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    attrs = {a.key: a.value.string_value for a in span.attributes}
    assert attrs["langfuse.observation.type"] == "tool" and attrs["langfuse.environment"] == "isolated"
    assert json.loads(attrs["langfuse.observation.input"]) == {"request": {"question": "compute"}}
    assert not any("score" in k or "experiment" in k for k in attrs)
    assert plan["controlled_replay_not_agent_execution"] is True
    if case == "duplicate":
        assert span.trace_id.hex() == run["trace_id"] and span.span_id.hex() == run["observation_id"]
    else:
        assert span.trace_id.hex() != run["trace_id"] and span.span_id.hex() != run["observation_id"]
    assert span.name == ("issue214-rule-miss" if case == "rule-miss" else "remote-agent-http")
    output = json.loads(attrs["langfuse.observation.output"])
    if case == "invalid-io":
        assert "text" not in output
    elif case == "wrong-answer":
        assert output["text"] == "0"
    else:
        assert output == {"text": "1266"}
