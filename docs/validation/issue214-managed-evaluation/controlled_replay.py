#!/usr/bin/env python3
"""Explicit OTLP fault injection for the synthetic Issue214 calculator smoke test.

Default is dry-run. --emit may incur hosted-evaluator/model costs if rules are
active. This does NOT execute an Agent, create scores, or attach Experiment IO.
Use only your own isolated rules/environment, never production observations.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.resource.v1 import resource_pb2
from opentelemetry.proto.trace.v1 import trace_pb2


def parsed(value):
    return json.loads(value) if isinstance(value, str) else value


def build_case(case: str, run: dict, business: dict) -> tuple[dict, bytes]:
    if case not in {"duplicate", "rule-miss", "invalid-io", "wrong-answer"}:
        raise ValueError("Unknown controlled replay case")
    inp, out = parsed(business["input"]), parsed(business["output"])
    if not isinstance(inp, dict) or not isinstance(out, dict):
        raise ValueError("This fixture requires the synthetic HTTP JSON calculator task")
    trace_id, observation_id = run["trace_id"], run["observation_id"]
    name = "remote-agent-http"
    start = time.time_ns()
    if case == "duplicate":
        start = int(datetime.fromisoformat(business["startTime"].replace("Z", "+00:00")).timestamp() * 1e9)
    else:
        trace_id, observation_id = uuid.uuid4().hex, uuid.uuid4().hex[:16]
    if case == "rule-miss":
        name = "issue214-rule-miss"
    if case == "invalid-io":
        out = {"not_text": "invalid evaluator input"}
    if case == "wrong-answer":
        out = {**out, "text": "0"}
    attributes = {"langfuse.observation.type": "tool", "langfuse.environment": run["environment"],
                  "langfuse.observation.input": json.dumps(inp, ensure_ascii=False),
                  "langfuse.observation.output": json.dumps(out, ensure_ascii=False)}
    attrs = [common_pb2.KeyValue(key=k, value=common_pb2.AnyValue(string_value=v)) for k, v in attributes.items()]
    span = trace_pb2.Span(trace_id=bytes.fromhex(trace_id), span_id=bytes.fromhex(observation_id), name=name,
                         start_time_unix_nano=start, end_time_unix_nano=start + 1, attributes=attrs)
    resource = resource_pb2.Resource(attributes=[common_pb2.KeyValue(
        key="service.name", value=common_pb2.AnyValue(string_value="argus-issue214-controlled-replay"))])
    scope = trace_pb2.ScopeSpans(scope=common_pb2.InstrumentationScope(name="argus-issue214-controlled-replay"), spans=[span])
    payload = trace_service_pb2.ExportTraceServiceRequest(resource_spans=[
        trace_pb2.ResourceSpans(resource=resource, scope_spans=[scope])]).SerializeToString()
    plan = {"case": case, "trace_id": trace_id, "observation_id": observation_id, "name": name,
            "environment": run["environment"], "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "controlled_replay_not_agent_execution": True}
    return plan, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=["duplicate", "rule-miss", "invalid-io", "wrong-answer"])
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True, help="Saved API response with response.data entries")
    parser.add_argument("--run-index", type=int, default=0)
    parser.add_argument("--emit", action="store_true", help="Really POST this controlled OTLP span; may incur evaluator cost")
    parser.add_argument("--output", type=Path, help="Save the submission receipt (no credentials or business content)")
    args = parser.parse_args()
    run = json.loads(args.receipt.read_text())["runs"][args.run_index]
    rows = json.loads(args.observations.read_text())["response"]["data"]
    matches = [r for r in rows if r.get("traceId") == run["trace_id"] and r.get("id") == run["observation_id"]
               and r.get("type") == "TOOL" and r.get("name") == "remote-agent-http"]
    if len(matches) != 1:
        raise ValueError("Require one actual HTTP TOOL observation, not an empty replay SPAN")
    plan, payload = build_case(args.case, run, matches[0])
    plan["emitted"] = args.emit
    if args.emit:
        base = os.environ["LANGFUSE_BASE_URL"].rstrip("/")
        origin = urllib.parse.urlparse(base)
        if origin.scheme != "https" or origin.username or origin.password or origin.path.strip("/") or origin.query or origin.fragment:
            raise ValueError("Use a credential-free HTTPS Langfuse origin")
        auth = "Basic " + base64.b64encode((os.environ["LANGFUSE_PUBLIC_KEY"] + ":" + os.environ["LANGFUSE_SECRET_KEY"]).encode()).decode()
        request = urllib.request.Request(base + "/api/public/otel/v1/traces", data=payload, method="POST", headers={
            "Authorization": auth, "Content-Type": "application/x-protobuf", "x-langfuse-ingestion-version": "4"})
        with urllib.request.urlopen(request, timeout=45) as response:
            plan.update({"submitted_at": datetime.now(UTC).isoformat(), "status": response.status,
                         "response_hex": response.read().hex()})
    if args.output:
        args.output.write_text(json.dumps(plan, indent=2) + "\n")
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
