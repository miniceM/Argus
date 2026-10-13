"""普通业务 HTTP/OTel 示例，共享业务语义，不依赖任何评测 SDK。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


def deny_external_network():
    """依赖已预装；业务与 exporter 仅允许连接本地服务，未知地址失败。"""
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_resolve = socket.getaddrinfo
    def require_local(host):
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise OSError("fixture offline network policy")
    def guarded_connect(original):
        def connect(sock, address):
            if isinstance(address, tuple):
                require_local(address[0])
            return original(sock, address)
        return connect
    def resolve(host, *args, **kwargs):
        if host is not None:
            require_local(host)
        return original_resolve(host, *args, **kwargs)
    socket.socket.connect = guarded_connect(original_connect)
    socket.socket.connect_ex = guarded_connect(original_connect_ex)
    socket.getaddrinfo = resolve


class Workflow:
    def __init__(self, tracer, profile, operation):
        self.tracer, self.profile, self.operation = tracer, profile, operation
        self.scope = uuid.uuid4().hex
        self.sequence = 0
        self.lock = threading.Lock()
        self.inventory = []
        self.last_completed = None

    def attributes(self, event_id, phase, status, call_id=None):
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            self.inventory.append(event_id)
        return {"workflow.scope_id": self.scope, "workflow.event_id": event_id,
                "workflow.sequence": sequence, "workflow.phase": phase, "workflow.status": status,
                "workflow.call_id": call_id or event_id, "workflow.source": "business-instrumentation-v1"}

    async def tool(self, semantic_name, customer_id, *, predecessor=None, status="SUCCEEDED", visibility="AVAILABLE"):
        from opentelemetry.trace import Link, Status, StatusCode

        physical = {"customer_lookup": "crm_lookup", "balance_read": "ledger_read", "escalate": "escalate"}[semantic_name]
        if self.profile == "pydantic_ai":
            physical = "account_" + physical
        call_id = uuid.uuid4().hex
        if semantic_name == "balance_read":
            status = {"blocked": "BLOCKED", "failed": "FAILED", "cancelled": "CANCELLED"}.get(self.operation, status)
            visibility = {"missing": "MISSING", "redacted": "REDACTED", "uncollected": "UNCOLLECTED"}.get(self.operation, visibility)
        event_id = uuid.uuid4().hex
        arguments = {"customer_id": customer_id}
        if visibility == "MISSING":
            arguments = {}
        attrs = {**self.attributes(event_id, "REQUESTED", "SUCCEEDED", call_id),
                 "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": physical,
                 "workflow.arguments_visibility": visibility,
                 "workflow.predecessor_event_ids": json.dumps([predecessor[0]] if predecessor else [])}
        if visibility in {"AVAILABLE", "MISSING"}:
            attrs["gen_ai.tool.call.arguments"] = json.dumps(arguments)
        if visibility == "REDACTED":
            attrs["workflow.hidden_paths"] = json.dumps(["$.customer_id"])
        with self.tracer.start_as_current_span(physical + ".request", attributes=attrs,
                links=[Link(predecessor[1])] if predecessor else []) as request:
            request_context = request.get_span_context()
        execution_event = uuid.uuid4().hex
        start = time.monotonic()
        attrs = {**self.attributes(execution_event, "BLOCKED" if status == "BLOCKED" else "EXECUTED",
                                  "CANCELLED" if status == "BLOCKED" else status, call_id),
                 "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": physical,
                 "workflow.predecessor_event_ids": json.dumps([event_id])}
        with self.tracer.start_as_current_span(physical + ".execution", attributes=attrs,
                                              links=[Link(request_context)]) as execution:
            execution.set_attribute("business.tool_executed", status != "BLOCKED")
            async def perform():
                await asyncio.sleep(0.02)
                if status == "FAILED":
                    raise RuntimeError("synthetic business failure")
                if semantic_name == "customer_lookup":
                    return {"customer_id": arguments.get("customer_id")}
                return {"escalation_recorded": True} if semantic_name == "escalate" else {"balance": "12.50"}
            try:
                if status == "BLOCKED":
                    result = {"error": "policy_denied"}
                elif status == "CANCELLED":
                    task = asyncio.create_task(perform())
                    await asyncio.sleep(0)
                    task.cancel()
                    await task
                else:
                    result = await perform()
            except asyncio.CancelledError:
                result = {"error": "cancelled"}
            except RuntimeError:
                execution.set_status(Status(StatusCode.ERROR, "synthetic business failure"))
                result = {"error": "operation_failed"}
            execution.set_attribute("gen_ai.tool.call.result", json.dumps(result))
            execution.set_attribute("workflow.duration_ms", (time.monotonic() - start) * 1000)
            completed = (execution_event, execution.get_span_context())
        self.last_completed = completed
        return result, completed

    def terminal(self):
        if os.environ.get("S4_APPROVAL_URL"):
            from opentelemetry.propagate import inject
            endpoint = os.environ["S4_APPROVAL_URL"].rsplit("/", 1)[0] + "/inventory?workflow_id=" + self.scope
            with self.tracer.start_as_current_span("approval.inventory.http"):
                headers = {}
                inject(headers)
                with build_opener(ProxyHandler({})).open(Request(endpoint, headers=headers), timeout=5) as response:
                    inventory = json.load(response)
            assert inventory["scope_closed"] is True
            assert set(inventory["event_ids"]) <= set(self.inventory)
        event_id = uuid.uuid4().hex
        attrs = self.attributes(event_id, "TERMINAL", "SUCCEEDED")
        attrs.update({"workflow.final_sequence": self.sequence, "workflow.event_count": len(self.inventory),
                      "workflow.event_ids": json.dumps(self.inventory)})
        with self.tracer.start_as_current_span("workflow.terminal", attributes=attrs):
            pass

    def approve(self):
        from opentelemetry.propagate import inject

        endpoint = os.environ["S4_APPROVAL_URL"]
        assert ipaddress.ip_address(urlparse(endpoint).hostname).is_loopback
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
        with self.tracer.start_as_current_span("approval.http"):
            headers = {"Content-Type": "application/json"}
            inject(headers)
            request = Request(endpoint, json.dumps({"workflow_id": self.scope, "sequence": sequence,
                              "case_reference": "synthetic-approval"}).encode(), headers=headers, method="POST")
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                approval = json.load(response)
        assert approval["decision"] == "approved"
        with self.lock:
            self.inventory.append(approval["event_id"])


async def pydantic_business(workflow, customer_id):
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Instrumentation
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.instrumented import InstrumentationSettings

    returns_seen = []
    def plan(messages, _info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        returns_seen.append(len(returned))
        if not returned:
            if workflow.operation == "parallel":
                return ModelResponse(parts=[ToolCallPart("account_crm_lookup", {"customer_id": customer_id}),
                                            ToolCallPart("account_ledger_read", {"customer_id": customer_id})])
            return ModelResponse(parts=[ToolCallPart("account_crm_lookup", {"customer_id": customer_id})])
        assert returned[0].tool_name == "account_crm_lookup"
        if len(returned) == 1:
            arguments = {} if workflow.operation == "missing" else {"customer_id": customer_id}
            return ModelResponse(parts=[ToolCallPart("account_ledger_read", arguments)])
        assert returned[1].tool_name == "account_ledger_read"
        content = returned[1].content
        return ModelResponse(parts=[TextPart("unavailable" if isinstance(content, dict) and "error" in content else "12.50")])

    agent = Agent(FunctionModel(plan), capabilities=[Instrumentation(InstrumentationSettings(
        tracer_provider=workflow.tracer_provider, include_content=True, version=5))])

    @agent.tool_plain
    async def account_crm_lookup(customer_id: str) -> dict:
        """查找合成客户记录。"""
        result, _completed = await workflow.tool("customer_lookup", customer_id)
        return result

    @agent.tool_plain
    async def account_ledger_read(customer_id: str | None = None) -> dict:
        """读取已查得客户的账户余额。"""
        predecessor = None if workflow.operation == "parallel" else workflow.last_completed
        if workflow.operation != "parallel":
            assert predecessor is not None
        if workflow.operation == "approved":
            workflow.approve()
        result, _completed = await workflow.tool("balance_read", customer_id, predecessor=predecessor)
        return result

    result = await agent.run("Read the synthetic customer's balance")
    assert result.output in {"12.50", "unavailable"}
    assert returns_seen == ([0, 2] if workflow.operation == "parallel" else [0, 1, 2])
    return {"balance": result.output}


def create_tracing(profile):
    if profile == "http_only":
        return None, None
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
        from requests import Session

        url = os.environ["S4_OTLP_URL"]
        assert ipaddress.ip_address(urlparse(url).hostname).is_loopback
        session = Session()
        session.trust_env = False
        exporter = OTLPSpanExporter(endpoint=url, headers={"Authorization": os.environ["S4_OTLP_AUTH"],
                                     "x-langfuse-ingestion-version": "4"}, session=session, timeout=10)
        mode = os.environ.get("S4_EXPORT_MODE", "immediate")
        if mode != "immediate":
            downstream = exporter
            class DelayedExporter(SpanExporter):
                def export(self, spans):
                    for span in spans:
                        if mode == "terminal_first" and span.name in {"workflow.terminal", profile + ".business"}:
                            downstream.export([span])
                        else:
                            timer = threading.Timer(8, downstream.export, args=([span],))
                            timer.daemon = True
                            timer.start()
                    return SpanExportResult.SUCCESS
                def shutdown(self):
                    downstream.shutdown()
            exporter = DelayedExporter()
        provider = TracerProvider(resource=Resource.create({"service.name": profile, "service.version": "fixture-v1"}))
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("synthetic-banking-business", "1.0.0")
        return provider, tracer


def serve(profile):
    deny_external_network()
    provider, tracer = create_tracing(profile)

    class Handler(BaseHTTPRequestHandler):
        retry_count = 0
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 < length <= 4096
                body = json.loads(self.rfile.read(length))
                customer_id = body["customer_id"]
                operation = body["operation"]
                output = {"intent": "balance", "balance": "12.50"}
                if tracer:
                    from opentelemetry.propagate import extract
                    carrier = {key.lower(): value for key, value in self.headers.items()}
                    with tracer.start_as_current_span(profile + ".business", context=extract(carrier),
                                                      attributes={"business.operation": operation}):
                        workflow = Workflow(tracer, profile, operation)
                        workflow.tracer_provider = provider
                        async def run():
                            if profile == "pydantic_ai":
                                return await pydantic_business(workflow, customer_id)
                            if operation == "parallel":
                                await asyncio.gather(workflow.tool("customer_lookup", customer_id),
                                                     workflow.tool("balance_read", customer_id))
                            else:
                                _result, completed = await workflow.tool("customer_lookup", customer_id)
                                if operation == "approved":
                                    workflow.approve()
                                await workflow.tool("balance_read", customer_id, predecessor=completed)
                                if operation == "escalate":
                                    await workflow.tool("escalate", customer_id, predecessor=workflow.last_completed)
                            return {"balance": "12.50"}
                        output.update(asyncio.run(run()))
                        workflow.terminal()
                payload = json.dumps(output).encode()
                if operation == "retry":
                    Handler.retry_count += 1
                self.send_response(503 if operation == "retry" and Handler.retry_count == 1 else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("X-Demo-Traceparent-Received", str(bool(self.headers.get("traceparent"))).lower())
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                # 错误输出不回显 Header/业务 input；测试保留失败类别。
                print("business failure:", type(exc).__name__,
                      [(Path(frame.filename).name, frame.name, frame.lineno)
                       for frame in traceback.extract_tb(exc.__traceback__)], flush=True)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error":"BUSINESS_FIXTURE_FAILED"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ready = Path(os.environ["S4_READY_FILE"])
    ready.write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}/invoke"}))
    print(f"business Agent ready: {profile}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if provider:
            provider.shutdown()


if __name__ == "__main__":
    serve(sys.argv[1])
