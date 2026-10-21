"""自有 Runner 的真实 SIGKILL 验收；默认 dry run，不修改已有服务。

复用 real_async_convergence.py 的隔离 Rule/真实 Agent 流程。独立 HTTP 计数代理
跨 Runner 重启存活，避免仅用数据库 attempt 数冒充 Agent 实际调用数证据。
凭据只经环境变量继承；不生成凭据文件，不人工写 Score/调用 collect。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SOURCES = [
    "managed_score_recovery.py", "worker.py", "main.py", "evaluation_recovery.py",
    "langfuse_managed_scores.py", "langfuse_run_items.py", "langfuse_sync.py",
    "langfuse_otel_projection.py", "reconciler.py",
]


def verify_evidence(out, save):
    """只读重验当前源码身份和已运行证据；绝不重复 Agent/Rule/Score 写入。"""
    identity = json.loads((out / "source-identity.json").read_text())
    proof = json.loads((out / "restart-proof.json").read_text())
    receipt = json.loads((out / "receipt.json").read_text())
    assert proof["source_commit"] == identity["source_commit"]
    launch = json.loads((out / "launch.json").read_text())["response"]
    assert launch["manifest"]["runner"]["build_id"] == proof["source_commit"]
    assert receipt["recovery_sha256"] == identity["source_hashes"][
        "services/eval-runner/app/managed_score_recovery.py"]
    for path, digest in identity["source_hashes"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    assert proof["before"]["eval_status"] == "pending" and proof["after"]["eval_status"] == "succeeded"
    assert proof["before"]["checkpoint_digest"] == proof["after"]["checkpoint_digest"]
    assert proof["before"]["http_attempts"] == proof["after"]["http_attempts"] == 1
    assert proof["independent_agent_requests"] == len(proof["agent_request_audit"]) == 1
    assert receipt["manual_collect_calls"] == receipt["score_write_requests"] == 0
    sys.path.insert(0, str(ROOT / "services/eval-runner"))
    from app.langfuse_scores_client import ScoresV3Client
    after = proof["after"]
    diagnostics = json.loads((out / "diagnostics-final.json").read_text())
    with ScoresV3Client.from_env() as client:
        observations = client.observations(after["trace_id"])
        # Langfuse 可返回同 ID 的 SDK transport SPAN；它不属于可评分证据。
        targets = [o for o in observations if o.get("id") == after["observation_id"]
                   and o.get("type") == "TOOL" and o.get("name") == "remote-agent-http"]
        assert len(targets) == 1
        assert targets[0].get("input") is not None and targets[0].get("output") is not None
        save("langfuse-observations", targets)
        scores = []
        for result in diagnostics["results"]:
            score = result["evidence"]["score"]
            scores.extend(client.scores_for(after["trace_id"], after["observation_id"], score["name"]))
        assert {s["id"] for s in scores} == set(receipt["score_ids"])
        assert len(scores) == 2 and all(s["source"] == "EVAL" for s in scores)
        save("langfuse-scores", scores)
        save("readback-request-audit", client.requests)
    rules = json.loads((out / "owned-rules-readback.json").read_text())
    assert len(rules) == 2 and all(r["enabled"] is False for r in rules)
    report = {"status": "PASS", "source_commit": proof["source_commit"],
              "verified_at": datetime.now(UTC).isoformat(), "independent_agent_requests": 1,
              "native_eval_scores": len(scores), "complete_tool_observations": len(targets),
              "owned_rules_paused": len(rules), "no_additional_agent_or_score_writes": True}
    save("verification-report", report)
    print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--verify-evidence", action="store_true")
    parser.add_argument("--agent-endpoint", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute and not args.verify_evidence:
        print("DRY RUN：一次真实 Agent 请求、隔离 Rule、仅强杀自有 Runner，以相同库重启。")
        return
    for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        assert os.environ.get(key), f"Missing {key}; no value logged"
    assert not subprocess.check_output(["git", "diff", "HEAD", "--", "services/eval-runner/app"], cwd=ROOT), (
        "必须先提交生产源码，不能把未提交代码绑定到旧 SHA"
    )
    out = args.evidence_dir.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()

    def save(name, data):
        rendered = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            rendered = rendered.replace(os.environ[key], "[redacted]")
        (out / f"{name}.json").write_text(rendered + "\n")

    if args.verify_evidence:
        assert not args.execute, "Choose execution or read-only verification, not both"
        verify_evidence(out, save)
        return
    out.mkdir(parents=True, exist_ok=False)
    save("source-identity", {
        "source_commit": commit, "environment": args.environment,
        "source_hashes": {f"services/eval-runner/app/{p}": hashlib.sha256(
            (ROOT / "services/eval-runner/app" / p).read_bytes()).hexdigest() for p in SOURCES},
        "configuration": {"database": "isolated SQLite", "worker_concurrency": 2,
                          "managed_concurrency": 2, "run_item_strategy": "otel",
                          "max_attempts": 10, "deadline_seconds": 1800, "agent_retries": 0},
    })
    calls, call_lock = [], threading.Lock()

    class Counter(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass  # 绝不记录请求内容、Header 或凭据。

        def do_POST(self):
            with call_lock:
                record = {"at": datetime.now(UTC).isoformat(), "status": None}
                calls.append(record)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {k: self.headers[k] for k in ("Content-Type", "traceparent", "tracestate") if k in self.headers}
            with httpx.Client(timeout=150, follow_redirects=False) as client:
                response = client.post(args.agent_endpoint, content=body, headers=headers)
            record["status"] = response.status_code
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    counter = ThreadingHTTPServer(("127.0.0.1", 0), Counter)
    threading.Thread(target=counter.serve_forever, daemon=True).start()
    runner = verifier = None
    with tempfile.TemporaryDirectory(prefix="argus217-live-") as scratch:
        db_file = Path(scratch) / "runner.db"
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "services/eval-runner"),
               "DATABASE_URL": f"sqlite:///{db_file}", "ARGUS_DB_MODE": "standalone", "ARGUS_ENV": "test",
               "ARGUS_REDIS_URL": "", "ARGUS_AUTO_IMPORT_YAML": "false", "ARGUS_DATASET_SOURCE": "langfuse",
               "ARGUS_BUILD_ID": commit, "LANGFUSE_TRACING_ENVIRONMENT": args.environment,
               "ARGUS_LANGFUSE_RUN_ITEM_STRATEGY": "otel", "ARGUS_WORKER_ENABLED": "true",
               "ARGUS_RECONCILER_ENABLED": "true", "ARGUS_WORKER_CONCURRENCY": "2",
               "ARGUS_MANAGED_SCORE_CONCURRENCY": "2"}

        def start_runner(log):
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            for _ in range(150):
                try:
                    if httpx.get(base + "/health", timeout=1).is_success:
                        return process
                except httpx.HTTPError:
                    pass
                assert process.poll() is None, "Owned Runner startup failed; private log not exported"
                time.sleep(0.1)
            process.kill()
            process.wait()
            raise TimeoutError("Owned Runner startup timeout")

        def state():
            with sqlite3.connect(db_file) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT id,execution_status,eval_status,evaluation_status,evaluation_generation,"
                                 "evaluation_lease_expires_at,trace_id,observation_id FROM experiment_item_executions").fetchone()
                result = dict(row)
                result["http_attempts"] = db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0]
                result["checkpoint_digest"] = db.execute("SELECT output_digest FROM execution_checkpoints").fetchone()[0]
                result["outbox"] = [dict(r) for r in db.execute(
                    "SELECT status,attempts,scores_payload FROM langfuse_sync_tasks")]
            return result

        try:
            with open(Path(scratch) / "runner.log", "w") as runner_log, open(Path(scratch) / "verifier.log", "w") as verifier_log:
                runner = start_runner(runner_log)
                verifier = subprocess.Popen([
                    sys.executable, str(HERE / "real_async_convergence.py"), "--execute", "--argus-url", base,
                    "--environment", args.environment, "--agent-endpoint", f"http://127.0.0.1:{counter.server_port}/invoke",
                    "--evidence-dir", str(out), "--converge-timeout", "1200",
                ], cwd=ROOT, env=env, stdout=verifier_log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 360
                while not (out / "items-before.json").exists():
                    assert verifier.poll() is None, "Live setup failed; inspect sanitized evidence, not private logs"
                    assert time.monotonic() < deadline, "No successful Agent checkpoint; do not retry Agent"
                    time.sleep(0.1)
                before = state()
                assert before["execution_status"] == "succeeded" and before["eval_status"] == "pending"
                assert before["http_attempts"] == len(calls) == 1
                crashed_at = datetime.now(UTC).isoformat()
                runner.kill()  # 只操作本函数创建且仍由本 Popen 持有的进程。
                runner.wait()
                runner = start_runner(runner_log)
                restored_at = datetime.now(UTC).isoformat()
                assert verifier.wait(timeout=1220) == 0, "Real scores did not converge; no mock fallback"
                after = state()
                assert before["checkpoint_digest"] == after["checkpoint_digest"]
                assert after["http_attempts"] == len(calls) == 1
                assert after["eval_status"] == "succeeded" and after["evaluation_status"] == "recovered"
                assert any(r["status"] == "SYNCED" and json.loads(r["scores_payload"]).get(
                    "_managed_observation_digest") == after["checkpoint_digest"] for r in after["outbox"])
                save("restart-proof", {"status": "PASS", "source_commit": commit, "database_reused": True,
                     "crashed_at": crashed_at, "restored_at": restored_at, "before": before, "after": after,
                     "independent_agent_requests": len(calls), "agent_request_audit": calls,
                     "existing_services_modified": False})
        finally:
            if verifier and verifier.poll() is None:
                verifier.send_signal(signal.SIGINT)  # 让子脚本 finally 暂停自有 Rule。
                try:
                    verifier.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    verifier.kill()
                    verifier.wait()
            if runner and runner.poll() is None:
                runner.terminate()
                try:
                    runner.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait()
            counter.shutdown()
            # 失败也回读/清理仅本目录本次创建的 Rule，不接触他人的 Rule。
            with httpx.Client(base_url=os.environ["LANGFUSE_BASE_URL"], timeout=60,
                              auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])) as client:
                readback = []
                for path in sorted(out.glob("rule-*.json")):
                    rule_id = json.loads(path.read_text())["response"]["id"]
                    endpoint = "/api/public/v2/evaluation-rules/" + rule_id
                    time.sleep(2.2)
                    response = client.get(endpoint)
                    response.raise_for_status()
                    if response.json()["enabled"]:
                        time.sleep(2.2)
                        client.patch(endpoint, json={"enabled": False}).raise_for_status()
                        time.sleep(2.2)
                        response = client.get(endpoint)
                        response.raise_for_status()
                    readback.append({"id": rule_id, "enabled": response.json()["enabled"]})
                save("owned-rules-readback", readback)
                assert all(r["enabled"] is False for r in readback), "Owned Rule cleanup failed"
    verify_evidence(out, save)


if __name__ == "__main__":
    main()
