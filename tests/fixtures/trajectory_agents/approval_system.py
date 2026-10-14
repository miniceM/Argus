"""隔离的合成审批系统：生成自己的审批事件 ID，通过普通 OTel 导出。

用于证明审批来源映射，不代表真实人员已作审批。
"""

import base64
import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from business import create_tracing, deny_external_network
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from opentelemetry.propagate import extract


def _identity(value, size):
    return type(value) is str and re.fullmatch(f"[0-9a-f]{{{size}}}", value) and set(value) != {"0"}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("APPROVAL_REQUEST_INVALID")
        value[key] = item
    return value


class ApprovalAuthority:
    """独立进程持有临时私钥；只发布公钥与有限签名事实，不导入 Argus。"""

    def __init__(self):
        self._key = Ed25519PrivateKey.generate()

    def public_policy(self):
        return {"authority_id": "isolated-approval-system-v1", "algorithm": "Ed25519",
            "public_key": base64.b64encode(self._key.public_key().public_bytes_raw()).decode()}

    def receipt(self, kind, *, trace_id, workflow_scope_id, observation_id, root_observation_id, event_id=None, event_ids=None):
        values = ((trace_id, 32), (workflow_scope_id, 32), (observation_id, 16), (root_observation_id, 16))
        if any(type(value) is not str or not re.fullmatch(f"[0-9a-f]{{{size}}}", value)
            or set(value) == {"0"} for value, size in values):
            raise ValueError("AUTHORITY_RECEIPT_INVALID")
        body = {"schema_version": "human-authority-1.0", "authority_id": self.public_policy()["authority_id"], "kind": kind,
            "trace_id": trace_id, "workflow_scope_id": workflow_scope_id, "observation_id": observation_id,
            "root_observation_id": root_observation_id}
        if (kind == "ESCALATION" and event_ids is None and type(event_id) is str
            and re.fullmatch("[0-9a-f]{32}", event_id) and set(event_id) != {"0"}):
            body.update(event_id=event_id, outcome="APPROVED")
        elif kind == "INVENTORY" and event_id is None and type(event_ids) is list and len(event_ids) <= 1000 and (
            all(type(value) is str and re.fullmatch("[0-9a-f]{32}", value) and set(value) != {"0"} for value in event_ids)
            and len(event_ids) == len(set(event_ids))):
            body.update(event_ids=sorted(event_ids), scope_closed=True)
        else:
            raise ValueError("AUTHORITY_RECEIPT_INVALID")
        message = b"Argus/HumanAuthority/1.0\x00" + json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return {"body": body, "signature": base64.b64encode(self._key.sign(message)).decode()}


def main():
    deny_external_network()
    provider, tracer = create_tracing("s4-approval-system")
    authority = ApprovalAuthority()
    records, sequences, closed, lock = {}, {}, set(), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def trace_context(self):
            from opentelemetry.trace import get_current_span
            context = extract({key.lower(): value for key, value in self.headers.items()})
            span_context = get_current_span(context).get_span_context()
            if not span_context.is_valid:
                self.send_error(400)
                return None
            return context, f"{span_context.trace_id:032x}"

        def do_POST(self):
            if self.path != "/approve":
                self.send_error(404)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not re.fullmatch("[0-9]{1,4}", lengths[0])
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json"):
                self.send_error(400)
                return
            length = int(lengths[0])
            if not 0 < length <= 4096:
                self.send_error(400)
                return
            try:
                body = json.loads(self.rfile.read(length), object_pairs_hook=_object)
            except (ValueError, UnicodeError, RecursionError, OSError):
                self.send_error(400)
                return
            if type(body) is not dict:
                self.send_error(400)
                return
            if (set(body) != {"case_reference", "workflow_id", "root_id", "sequence"}
                or body.get("case_reference") != "synthetic-approval"
                or not _identity(body.get("workflow_id"), 32) or not _identity(body.get("root_id"), 16)
                or type(body.get("sequence")) is not int
                or not 1 <= body["sequence"] <= 1000):
                self.send_error(403)
                return
            current = self.trace_context()
            if current is None:
                return
            context, trace_id = current
            event_id = uuid.uuid4().hex
            identity = trace_id, body["root_id"], body["workflow_id"]
            with lock:
                known = set(records) | closed
                if (identity in closed or identity not in known and len(known) >= 1000
                    or len(records.get(identity, [])) >= 1000 or body["sequence"] in sequences.get(identity, set())):
                    self.send_error(409)
                    return
                records.setdefault(identity, []).append(event_id)
                sequences.setdefault(identity, set()).add(body["sequence"])
            with tracer.start_as_current_span("approval.decision", context=context, attributes={
                    "workflow.scope_id": body["workflow_id"], "workflow.sequence": body["sequence"],
                    "workflow.event_id": event_id, "workflow.phase": "HUMAN", "workflow.status": "SUCCEEDED",
                    "workflow.source": "isolated-approval-system-v1", "approval.decision": "approved"}) as span:
                span.set_attribute("approval.receipt", json.dumps(authority.receipt("ESCALATION", trace_id=trace_id,
                    workflow_scope_id=body["workflow_id"], root_observation_id=body["root_id"],
                    observation_id=f"{span.get_span_context().span_id:016x}", event_id=event_id), sort_keys=True, separators=(",", ":")))
                payload = json.dumps({"event_id": event_id, "decision": "approved"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/inventory":
                self.send_error(404)
                return
            try:
                if len(parsed.query) > 128:
                    raise ValueError
                query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True, max_num_fields=2)
                if (set(query) != {"workflow_id", "root_id"} or any(len(values) != 1 for values in query.values())
                    or not _identity(query["workflow_id"][0], 32) or not _identity(query["root_id"][0], 16)):
                    raise ValueError
            except ValueError:
                self.send_error(400)
                return
            scope, root = query["workflow_id"][0], query["root_id"][0]
            current = self.trace_context()
            if current is None:
                return
            context, trace_id = current
            identity = trace_id, root, scope
            with lock:
                known = set(records) | closed
                if identity not in known and len(known) >= 1000:
                    self.send_error(409)
                    return
                closed.add(identity)
                events = list(records.get(identity, []))
            with tracer.start_as_current_span("approval.inventory", context=context, attributes={
                    "workflow.scope_id": scope, "workflow.source": "isolated-approval-system-v1",
                    "approval.event_count": len(events), "approval.scope_closed": True}) as span:
                span.set_attribute("approval.receipt", json.dumps(authority.receipt("INVENTORY", trace_id=trace_id,
                    workflow_scope_id=scope, root_observation_id=root, observation_id=f"{span.get_span_context().span_id:016x}",
                    event_ids=events), sort_keys=True, separators=(",", ":")))
                payload = json.dumps({"event_ids": events, "scope_closed": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Path(os.environ["S4_READY_FILE"]).write_text(json.dumps({"endpoint": f"http://127.0.0.1:{server.server_port}/approve",
        "human_authority": authority.public_policy()}))
    print("isolated approval system ready", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        provider.shutdown()


if __name__ == "__main__":
    main()
