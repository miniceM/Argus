"""#255：两个真实自托管 Langfuse project；不使用 Cloud 或业务环境凭据。

本测试验证真实 Dataset/OTLP/Score 项目路由。Builtin Score 不替代
Langfuse 原生 Evaluation Rule / EVAL 来源评分的独立验收。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from app.db_models import ExecutionAttemptRecord, ExperimentItemExecutionRecord
from app.langfuse_connection import BoundLangfuseClient, resolve_manifest_connection, scores_client_for_manifest
from app.langfuse_sync import LangfuseOutboxSyncer
from app.manifest import LaunchService
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from trajectory_agents.local_runtime import LocalLangfuse  # noqa: E402

pytest_plugins = ["test_credential_resources"]
pytestmark = pytest.mark.trajectory_local


def test_two_real_projects_isolate_dataset_observation_and_scores(credentials, setup_runtime, tmp_path, monkeypatch):
    _, service, registry, _ = credentials
    db, queue, _, orchestrator, worker, reconciler = setup_runtime
    received = []

    class Agent(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            received.append(self.headers.get("traceparent"))
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"answer": "ok", "intent": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        directory = tmp_path / "langfuse"
        directory.mkdir()
        with LocalLangfuse(directory) as runtime:
            project_b = "project-b-" + uuid.uuid4().hex
            # 只在一次性测试数据库建立第二个项目；密钥使用镜像原生生成函数，
            # 不复制哈希或把密钥放入 SQL、进程参数和持久化证据。
            runtime.compose("exec", "-T", "postgres", "psql", "-U", "langfuse", "-d", "langfuse", "-v", "ON_ERROR_STOP=1", "-c", f"""
                INSERT INTO projects SELECT (jsonb_populate_record(NULL::projects,
                    to_jsonb(p) || jsonb_build_object('id', '{project_b}', 'name', 'Issue 255 B'))).*
                    FROM projects p WHERE id = '{runtime.env['S4_PROJECT_ID']}';
            """)
            generated = json.loads(runtime.compose("exec", "-T", "-w", "/app/worker", "worker", "node", "-e", f"""
                const {{prisma}} = require('@langfuse/shared/src/db');
                const {{createAndAddApiKeysToDb}} = require('@langfuse/shared/src/server/auth/apiKeys');
                (async () => {{
                    const key = await createAndAddApiKeysToDb({{prisma, scope: 'PROJECT', entityId: '{project_b}'}});
                    await prisma.$disconnect();
                    console.log(JSON.stringify({{publicKey: key.publicKey, secretKey: key.secretKey}}));
                    process.exit(0);
                }})().catch(() => {{ console.error('fixture key generation failed'); process.exit(1); }});
            """).splitlines()[-1])
            runtime.env["S4_SECRET_KEY_B"] = generated["secretKey"]
            monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "legacy")
            monkeypatch.setenv("LANGFUSE_PROJECT_ID", runtime.env["S4_PROJECT_ID"])
            launches = []
            for label, project, public, secret in (("a", runtime.env["S4_PROJECT_ID"], runtime.env["S4_PUBLIC_KEY"], runtime.env["S4_SECRET_KEY"]),
                                                   ("b", project_b, generated["publicKey"], generated["secretKey"])):
                credential = service.create("Project " + label, "production",
                    {"username": public, "password": secret}, "integration",
                    type="basic_auth", allowed_origins=[runtime.base_url])
                binding = {"base_url": runtime.base_url, "project_id": project, "credential_id": credential["id"]}
                registry.create_agent("team-" + label, "Team " + label, langfuse_connection=binding)
                connection = resolve_manifest_connection(db, {"langfuse_connection": {**binding, "source": "BOUND"}})
                with BoundLangfuseClient(connection) as client:
                    client.verify_project()
                    client.api.datasets.create(name="same-dataset-name")
                    client.api.dataset_items.create(dataset_name="same-dataset-name", input={"team": label},
                                                    expected_output={"intent": "ok"}, id="case-" + label)
                for version in ("v1", "v2"):
                    registry.create_version("team-" + label, version, f"http://127.0.0.1:{server.server_port}/invoke", max_retries=0)
                    launch = LaunchService(db, registry).create_launch("team-" + label, version, "same-dataset-name",
                                                                      evaluator_ids=["intent_match"])
                    assert launch.manifest["dataset"]["items"][0]["id"] == "case-" + label
                    assert launch.manifest["langfuse_connection"] == {**binding, "source": "BOUND", "credential_version": 1}
                    orchestrator.start_launch(launch.id)
                    launches.append(launch)
            for message in queue.read_group("two-projects", count=10):
                assert asyncio.run(worker.execute_item_message(*message))
            reconciler.reconcile_launch_states()
            syncer = LangfuseOutboxSyncer(db, langfuse_client=lambda: pytest.fail("global client used"))
            assert syncer.process_batch(batch_size=10) == 4
            assert len(received) == 4 and len(set(received)) == 4
            with db.get_session() as session:
                items = [session.scalar(select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.launch_id == launch.id)) for launch in launches]
            assert all(item.execution_status == "succeeded" and item.quality_conclusion == "pass" for item in items)
            deadline = time.monotonic() + 90
            receipt = {"observed_at": datetime.now(UTC).isoformat(), "test": "two_real_projects",
                       "score_source": "API", "native_rule_eval_verified": False, "binding_scope": "agent", "projects": []}
            for index, launch in enumerate(launches):
                own, other = items[index], items[2 if index < 2 else 0]
                with scores_client_for_manifest(db, launch.manifest) as client:
                    while True:
                        observations = client.observations(own.trace_id)
                        # Builtin 的来源是 API；Managed/EVAL 来源保留给另一项专项验收。
                        scores = client._pages("/api/public/v3/scores", {"traceId": own.trace_id})
                        if observations and scores:
                            break
                        assert time.monotonic() < deadline, "真实 Observation/Score 未在期限内可见"
                        time.sleep(1)
                    assert any(row["id"] == own.observation_id and row["name"] == "remote-agent-http" for row in observations)
                    assert all(row["source"] == "API" for row in scores)
                    assert client.observations(other.trace_id) == []
                    assert client._pages("/api/public/v3/scores", {"traceId": other.trace_id}) == []
                    with db.get_session() as session:
                        attempt = session.get(ExecutionAttemptRecord, own.final_attempt_id)
                    evidence = attempt.credential_usage[-1]
                    binding = launch.manifest["langfuse_connection"]
                    assert evidence["credential_id"] == binding["credential_id"] and evidence["version"] == 1
                    receipt["projects"].append({"agent_id": launch.agent_id, "agent_version": launch.agent_version,
                        "project_id": binding["project_id"], "launch_id": launch.id,
                        "credential_id": evidence["credential_id"], "credential_version": evidence["version"],
                        "dataset_item_id": own.dataset_item_id, "trace_id": own.trace_id, "observation_id": own.observation_id,
                        "score_ids": [row["id"] for row in scores], "execution": own.execution_status,
                        "quality": own.quality_conclusion, "cross_project_observations": 0, "cross_project_scores": 0})
            (tmp_path / "two-project-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()
