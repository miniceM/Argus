"""独立业务 HTTP 工具：不代理 Prediction、不模拟模型，记录真实工具请求。"""

import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOCK = threading.Lock()
LOG = Path(os.environ.get("TOOL_RECEIPTS_PATH", "/state/tool-receipts.jsonl"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            data = {"status": "ok"}
        elif path == "/receipts":
            with LOCK:
                data = [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []
        else:
            case_id = path.removeprefix("/")
            if not case_id or len(case_id) > 100 or not all(c.isalnum() or c in "-_" for c in case_id):
                self.send_error(400)
                return
            data = {"case_id": case_id, "receipt": secrets.token_hex(12), "value": 1266, "timestamp": time.time()}
            with LOCK:
                LOG.parent.mkdir(parents=True, exist_ok=True)
                with LOG.open("a") as handle:
                    handle.write(json.dumps(data) + "\n")
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
