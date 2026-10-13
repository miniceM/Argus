"""隔离的合成审批系统：生成自己的审批事件 ID，通过普通 OTel 导出。

用于证明审批来源映射，不代表真实人员已作审批。
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from business import create_tracing, deny_external_network
from opentelemetry.propagate import extract


def main():
    deny_external_network()
    provider, tracer = create_tracing("s4-approval-system")
    records, closed, lock = {}, set(), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(length))
            if body.get("case_reference") != "synthetic-approval":
                self.send_error(403)
                return
            event_id = uuid.uuid4().hex
            with lock:
                if body["workflow_id"] in closed:
                    self.send_error(409)
                    return
                records.setdefault(body["workflow_id"], []).append(event_id)
            with tracer.start_as_current_span("approval.decision", context=extract(
                {key.lower(): value for key, value in self.headers.items()}), attributes={
                    "workflow.scope_id": body["workflow_id"], "workflow.sequence": body["sequence"],
                    "workflow.event_id": event_id, "workflow.phase": "HUMAN", "workflow.status": "SUCCEEDED",
                    "workflow.source": "isolated-approval-system-v1", "approval.decision": "approved"}):
                payload = json.dumps({"event_id": event_id, "decision": "approved"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            scope = parse_qs(urlparse(self.path).query)["workflow_id"][0]
            with lock:
                closed.add(scope)
                events = list(records.get(scope, []))
            with tracer.start_as_current_span("approval.inventory", context=extract(
                {key.lower(): value for key, value in self.headers.items()}), attributes={
                    "workflow.scope_id": scope, "workflow.source": "isolated-approval-system-v1",
                    "approval.event_count": len(events), "approval.scope_closed": True}):
                payload = json.dumps({"event_ids": events, "scope_closed": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Path(os.environ["S4_READY_FILE"]).write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}/approve"}))
    print("isolated approval system ready", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        provider.shutdown()


if __name__ == "__main__":
    main()
