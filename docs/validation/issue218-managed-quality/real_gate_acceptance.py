#!/usr/bin/env python3
"""#218 隔离真实验收：复用 #217 原生双评分，验证现有 Frozen Snapshot/Gate。

默认 dry-run。--execute 使用已运行隔离测试容器提供的凭据（仅内存传递），
启动当前 checkout 的独立 SQLite Runner。PASS 使用真实 Flowise 模型/工具；
FAIL/UNKNOWN 使用合成错误 HTTP 答案，但 Judge/Code Score 均由真实 Langfuse 产生。
不手写 Score，不修改共享 Rule；结束时暂停本次自有 Rule，停止本进程的服务。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--credential-container", default="argus-flowise-e2e-eval-runner-1")
    parser.add_argument("--flowise-origin", default="http://127.0.0.1:18091")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if not args.execute:
        print("DRY RUN：隔离真实双评分 PASS / 错误 HTTP 输出 FAIL / 暂停自有 Code Rule UNKNOWN；会产生模型费用。")
        return
    name = "issue218-" + datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz")
    out = args.evidence_dir or HERE / "evidence" / name
    out.mkdir(parents=True, exist_ok=False)
    work = Path("/tmp") / name
    work.mkdir(exist_ok=False)
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_files = [
        "managed_quality.py",
        "managed_score_recovery.py",
        "result_snapshots.py",
        "aggregation.py",
        "release_gates.py",
        "langfuse_managed_scores.py",
        "langfuse_sync.py",
    ]
    source_hashes = {
        f: hashlib.sha256((ROOT / "services/eval-runner/app" / f).read_bytes()).hexdigest() for f in source_files
    }
    raw = json.loads(subprocess.check_output(["docker", "inspect", args.credential_container]))[0]
    provided = dict(x.split("=", 1) for x in raw["Config"]["Env"] if "=" in x)
    env = os.environ.copy()
    for key, value in provided.items():
        if key.startswith(("LANGFUSE_", "FLOWISE_")) or key in ("ARGUS_SECRET_MODE", "ARGUS_ALLOWED_CREDENTIAL_ENVS"):
            env[key] = value
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(
        DATABASE_URL="sqlite:///" + str(work / "runtime.db"),
        ARGUS_DB_MODE="standalone",
        ARGUS_AUTO_IMPORT_YAML="false",
        ARGUS_BUILD_ID=source_commit,
        RUNNER_VERSION="0.2.0",
        ARGUS_WORKER_ENABLED="true",
        ARGUS_RECONCILER_ENABLED="true",
        ARGUS_WORKER_CONCURRENCY="1",
        ARGUS_DATASET_SOURCE="langfuse",
        ARGUS_LANGFUSE_RUN_ITEM_STRATEGY="otel",
        LANGFUSE_TRACING_ENVIRONMENT=name,
        ARGUS_MANAGED_SCORE_MAX_ATTEMPTS="15",
        ARGUS_MANAGED_SCORE_BASE_DELAY_SECONDS="3",
        ARGUS_MANAGED_SCORE_MAX_DELAY_SECONDS="10",
        ARGUS_MANAGED_SCORE_DEADLINE_SECONDS="240",
        PYTHONPATH=str(ROOT / "services/eval-runner"),
    )
    env.pop("ARGUS_REDIS_URL", None)
    audit, rules, frozen = [], [], []

    def save(filename, data):
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        for key, value in env.items():
            if value and ("KEY" in key or "TOKEN" in key or "SECRET" in key):
                text = text.replace(value, "[redacted]")
        (out / (filename + ".json")).write_text(text + "\n")

    lf = httpx.Client(
        base_url=env["LANGFUSE_BASE_URL"].rstrip("/"),
        auth=(env["LANGFUSE_PUBLIC_KEY"], env["LANGFUSE_SECRET_KEY"]),
        timeout=90,
    )
    argus = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180)

    def request(client, method, path, body=None):
        if client is lf:
            assert method == "GET" or (method == "PATCH" and any(path.endswith(r) for r in rules))
            time.sleep(2.2)
        response = client.request(method, path, json=body) if body is not None else client.request(method, path)
        audit.append(
            dict(
                service="langfuse" if client is lf else "argus",
                method=method,
                path=path,
                status=response.status_code,
                at=datetime.now(UTC).isoformat(),
            )
        )
        save("request-audit", audit)
        if response.status_code not in (200, 201, 202):
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}; response body omitted")
        return response.json()

    class WrongAnswer(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b'{"text":"0"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    wrong = ThreadingHTTPServer(("127.0.0.1", 0), WrongAnswer)
    threading.Thread(target=wrong.serve_forever, daemon=True).start()
    log = (work / "runner.log").open("wb")
    server = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    gate_policy = None

    def gate(launch_id, expected, label):
        summary = request(argus, "GET", f"/api/v1/experiment-launches/{launch_id}/summary")
        snapshot_id = summary["snapshot_id"]
        path = f"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}"
        snapshot = request(argus, "GET", path)
        result = request(
            argus,
            "POST",
            "/api/v1/release-gates/evaluate",
            dict(
                policy_name=gate_policy["name"],
                policy_version=gate_policy["version"],
                candidate_launch_id=launch_id,
                candidate_snapshot_id=snapshot_id,
            ),
        )
        save(label + "-summary", summary)
        save(label + "-snapshot", snapshot)
        save(label + "-gate", result)
        assert result["decision"] == expected and result["releasable"] == (expected == "PASS"), result
        frozen.append((path, snapshot, result))
        return result

    try:
        for _ in range(300):
            if server.poll() is not None:
                raise RuntimeError("隔离 Runner 启动失败；日志仅保留本地")
            try:
                if argus.get("/openapi.json", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Runner readiness timeout")
        endpoint = json.loads(
            (ROOT / "docs/validation/issue214-managed-evaluation/evidence/agent-version.json").read_text()
        )["request"]["endpoint"]
        endpoint = args.flowise_origin.rstrip("/") + "/api/" + endpoint.split("/api/", 1)[1]
        # 原有 harness 只创建自有 Rule；没有 Score 写接口或手动 collect。
        command = [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "docs/validation/issue217-managed-async/real_async_convergence.py"),
            "--execute",
            "--argus-url",
            str(argus.base_url).rstrip("/"),
            "--environment",
            name,
            "--agent-endpoint",
            endpoint,
            "--evidence-dir",
            str(out / "pass-native"),
            "--converge-timeout",
            "600",
        ]
        with (work / "native-harness.log").open("wb") as harness_log:
            completed = subprocess.run(
                command, cwd=ROOT, env=env, stdout=harness_log, stderr=subprocess.STDOUT, timeout=900
            )
        native = (
            json.loads((out / "pass-native/receipt.json").read_text())
            if (out / "pass-native/receipt.json").exists()
            else {}
        )
        rules = native.get("rule_ids", [])
        if completed.returncode:
            raise RuntimeError("真实双评分前置失败；保留本地日志和 pass-native 脱敏证据，禁止 synthetic fallback")
        launch_body = json.loads((out / "pass-native/launch.json").read_text())["request"]
        gate_policy = request(
            argus,
            "POST",
            "/api/v1/release-policies",
            dict(
                name=name,
                version="1.0.0",
                agent_id=launch_body["agent_id"],
                environment=name,
                rules=[
                    dict(id="quality", metric="pass_rate", operator=">=", threshold=1.0),
                    dict(id="coverage", metric="evaluation_coverage", operator=">=", threshold=1.0),
                ],
            ),
        )
        gate(native["launch_id"], "PASS", "pass")
        for rule in rules:
            request(lf, "PATCH", "/api/public/v2/evaluation-rules/" + rule, {"enabled": True})
        version = json.loads((out / "pass-native/agent-version.json").read_text())["request"]
        version.update(version="v2", endpoint=f"http://127.0.0.1:{wrong.server_port}/invoke")
        request(argus, "POST", "/api/v1/agent-versions", version)
        code_rule = next(
            s["rule_id"] for s in launch_body["managed_evaluation"]["scores"] if s["name"].endswith("-code")
        )
        outcomes = []
        for label, expected in [("fail", "FAIL"), ("missing", "UNKNOWN")]:
            if label == "missing":
                request(lf, "PATCH", "/api/public/v2/evaluation-rules/" + code_rule, {"enabled": False})
            body = copy.deepcopy(launch_body)
            body.update(agent_version="v2", name=name + "-" + label, idempotency_key=name + "-" + label)
            launch = request(argus, "POST", "/api/v1/experiment-launches", body)
            path = "/api/v1/experiment-launches/" + launch["id"]
            request(argus, "POST", path + "/run")
            deadline = time.monotonic() + 420
            timeline = []
            while time.monotonic() < deadline:
                rows = request(argus, "GET", path + "/items")
                timeline.append(rows)
                if rows and rows[0]["evaluation_status"] in ("recovered", "recovery_failed"):
                    break
                time.sleep(5)
            else:
                raise RuntimeError(label + ": managed terminal state timeout")
            save(label + "-timeline", timeline)
            save(label + "-adopted", request(argus, "GET", path + "/items/" + rows[0]["id"] + "/managed-scores"))
            assert rows[0]["attempt_count"] == 1
            result = gate(launch["id"], expected, label)
            outcomes.append(dict(launch_id=launch["id"], decision=result["decision"], gate_id=result["id"]))
        # Rule 配置真实变化后，原 Snapshot 的每个字段、旧 Gate ID/结论均不变。
        for rule in rules:
            request(lf, "PATCH", "/api/public/v2/evaluation-rules/" + rule, {"enabled": False})
        for path, snapshot, old_gate in frozen:
            assert request(argus, "GET", path) == snapshot
            assert request(argus, "GET", "/api/v1/release-gates/" + old_gate["id"]) == old_gate
        with sqlite3.connect(work / "runtime.db") as db:
            attempts = db.execute("select count(*) from execution_attempts").fetchone()[0]
            snapshots = db.execute("select count(*) from run_result_snapshots").fetchone()[0]
            score_rows = db.execute("select count(*) from evaluation_results").fetchone()[0]
        assert attempts == 3
        save(
            "receipt",
            dict(
                status="PASS",
                source_commit=source_commit,
                source_sha256=source_hashes,
                environment=name,
                pass_native=native,
                outcomes=outcomes,
                http_attempts=attempts,
                snapshots=snapshots,
                adopted_results=score_rows,
                manual_score_writes=0,
                manual_collect_calls=0,
                historical_snapshots_and_stored_gates_unchanged=True,
                rule_update="enabled false, owned Rules only",
                fail_agent="synthetic wrong HTTP text=0; real Langfuse Judge/Code",
                at=datetime.now(UTC).isoformat(),
            ),
        )
        print("PASS: native managed scores -> Frozen Snapshot -> existing Gate PASS / FAIL / UNKNOWN", flush=True)
    except Exception as exc:
        save(
            "failure-receipt",
            {
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "source_commit": source_commit,
                "source_sha256": source_hashes,
            },
        )
        raise
    finally:
        paused = []
        for rule in rules:
            try:
                result = request(lf, "PATCH", "/api/public/v2/evaluation-rules/" + rule, {"enabled": False})
                paused.append({"id": rule, "enabled": result.get("enabled")})
            except Exception:
                paused.append({"id": rule, "cleanup": "FAILED"})
        save("owned-rules-paused", paused)
        server.terminate()
        try:
            server.wait(timeout=12)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        wrong.shutdown()
        wrong.server_close()
        log.close()
        argus.close()
        lf.close()
        if not all(r.get("enabled") is False for r in paused):
            raise RuntimeError("Rule cleanup incomplete")


if __name__ == "__main__":
    main()
