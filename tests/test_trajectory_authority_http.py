"""隔离审批进程的实际 HTTP 边界；内存 OTel exporter，不授予来源准入。"""

import json
import threading
from http.client import HTTPConnection, RemoteDisconnected
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

TRACE = "00-" + "1" * 32 + "-" + "2" * 16 + "-01"
BODY = {"workflow_id": "3" * 32, "root_id": "4" * 16, "sequence": 1, "case_reference": "synthetic-approval"}
QUERY = "/inventory?workflow_id=" + BODY["workflow_id"] + "&root_id=" + BODY["root_id"]


@pytest.fixture
def approval_http(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "fixtures" / "trajectory_agents"))
    import approval_system
    provider, exporter = TracerProvider(), InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(approval_system, "create_tracing", lambda _: (provider, provider.get_tracer("http-boundary")))
    monkeypatch.setattr(approval_system, "deny_external_network", lambda: None)
    monkeypatch.setenv("S4_READY_FILE", str(tmp_path / "ready.json"))
    ready, servers = threading.Event(), []
    def create_server(*args):
        server = ThreadingHTTPServer(*args)
        servers.append(server)
        ready.set()
        return server
    monkeypatch.setattr(approval_system, "ThreadingHTTPServer", create_server)
    thread = threading.Thread(target=approval_system.main, daemon=True)
    thread.start()
    assert ready.wait(5), "approval HTTP server did not bind"
    server = servers[0]
    def request(method, path, body=None, *, trace=TRACE):
        headers = {"Content-Type": "application/json"}
        if trace is not None:
            headers["traceparent"] = trace
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            try:
                response = connection.getresponse()
            except RemoteDisconnected:
                return 0, b""
            return response.status, response.read()
        finally:
            connection.close()
    yield request, exporter
    server.shutdown()
    thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b'"text"'])
def test_malformed_body_is_rejected_without_signing(approval_http, body):
    request, exporter = approval_http
    assert request("POST", "/approve", body)[0] == 400
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("trace", [None, "invalid", "00-" + "0" * 32 + "-" + "2" * 16 + "-01"])
def test_invalid_trace_does_not_mutate_the_closed_inventory(approval_http, trace):
    request, exporter = approval_http
    assert request("POST", "/approve", json.dumps(BODY), trace=trace)[0] == 400
    assert request("GET", QUERY, trace=trace)[0] == 400
    assert exporter.get_finished_spans() == ()
    status, payload = request("GET", QUERY)
    assert status == 200 and json.loads(payload)["event_ids"] == []


@pytest.mark.parametrize("path, status", [("/unknown", 404), ("/inventory", 400),
    (QUERY + "&root_id=" + "5" * 16, 400), (QUERY.replace("4" * 16, "0" * 16), 400),
    (QUERY + "&note=private", 400)])
def test_inventory_query_is_bounded_and_exact(approval_http, path, status):
    request, exporter = approval_http
    assert request("GET", path)[0] == status
    assert exporter.get_finished_spans() == ()


def test_unknown_post_route_cannot_issue_approval(approval_http):
    request, exporter = approval_http
    assert request("POST", "/unknown", json.dumps(BODY))[0] == 404
    assert exporter.get_finished_spans() == ()


def test_closed_scope_rejects_new_events_and_preserves_inventory(approval_http):
    request, _ = approval_http
    status, payload = request("POST", "/approve", json.dumps(BODY))
    assert status == 200
    identity = json.loads(payload)["event_id"]
    status, payload = request("GET", QUERY)
    assert status == 200 and json.loads(payload)["event_ids"] == [identity]
    assert request("POST", "/approve", json.dumps(BODY | {"sequence": 2}))[0] == 409
    status, payload = request("GET", QUERY)
    assert status == 200 and json.loads(payload)["event_ids"] == [identity]


def test_replayed_sequence_cannot_create_two_signed_events(approval_http):
    request, exporter = approval_http
    status, payload = request("POST", "/approve", json.dumps(BODY))
    assert status == 200
    identity = json.loads(payload)["event_id"]
    assert request("POST", "/approve", json.dumps(BODY))[0] == 409
    status, payload = request("GET", QUERY)
    assert status == 200 and json.loads(payload)["event_ids"] == [identity]
    assert [span.name for span in exporter.get_finished_spans()] == ["approval.decision", "approval.inventory"]


def test_empty_closed_scopes_share_the_global_capacity_bound(approval_http):
    request, _ = approval_http
    for index in range(1, 1001):
        path = "/inventory?workflow_id=" + f"{index:032x}" + "&root_id=" + BODY["root_id"]
        assert request("GET", path)[0] == 200
    overflow = "/inventory?workflow_id=" + f"{1001:032x}" + "&root_id=" + BODY["root_id"]
    assert request("GET", overflow)[0] == 409
    assert request("POST", "/approve", json.dumps(BODY))[0] == 409
