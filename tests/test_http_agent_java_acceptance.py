"""真实 Java/TCP + Launch/Worker 验收，不代表官方 DataAgent/模型/Cloud 验收。"""
from __future__ import annotations

import asyncio
import copy
import json
import select as io_select
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import test_credential_resources as credential_tests
from app.db_models import (
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from app.manifest import LaunchService
from app.result_snapshots import create_result_snapshot
from sqlalchemy import select

credentials = credential_tests.credentials
create = credential_tests.create


@pytest.fixture(scope="module")
def java_agent():
    java = shutil.which("java")
    if not java:
        pytest.skip("JDK 17+ absent; real Java acceptance NOT RUN")
    source = Path(__file__).parent / "fixtures/http_auth_java/AuthAgent.java"
    process = subprocess.Popen([java, "--source", "17", str(source)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = io_select.select([process.stdout], [], [], 30)
        assert ready, "Java HTTP fixture did not start in 30 seconds"
        line = process.stdout.readline().strip()
        assert line.isdigit(), "Java HTTP fixture failed to report its port"
        yield f"http://127.0.0.1:{line}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("scheme,route,outcome", [
    (scheme, route, outcome)
    for scheme, route in [("none", "none"), ("bearer", "bearer"), ("api_key", "key"), ("api_key", "raw"), ("basic", "basic")]
    for outcome in ["success", "bad-auth", "missing-header", "disabled"]
] + [("bearer", "bearer", outcome) for outcome in ["forbidden", "missing", "echo", "redirect", "invalid-output"]])
def test_real_java_worker_auth_headers_frozen_output_and_failures(credentials, setup_runtime, java_agent, scheme, route, outcome):
    client, _, registry, _ = credentials
    manager, queue, _, orchestrator, worker, reconciler = setup_runtime
    httpx.post(java_agent + "/reset")
    kind = {"bearer": "bearer_token", "api_key": "api_key", "basic": "basic_auth"}.get(scheme)
    secret = {"username": "fixture user", "password": "fixture password"} if scheme == "basic" else "fixture-first-token"
    cid = None
    auth = {"scheme": scheme}
    if kind:
        response = create(client, type=kind, secret=secret, allowed_origins=[java_agent])
        assert response.status_code == 201, response.text
        cid = response.json()["id"]
    if scheme == "api_key":
        auth.update(header_name="Authorization" if route == "raw" else "X-API-Key", prefix="Token " if route == "raw" else "")
    extra = create(client, type="header_value", secret="fixture-extra-secret", allowed_origins=[java_agent]).json()
    headers = [{"name": "X-Tenant", "value": "evaluation"}, {"name": "X-App-Id", "value": "argus-acceptance"},
               {"name": "X-App-Secret", "credential_id": extra["id"]}]
    if outcome == "missing-header":
        headers = headers[1:]
    special = outcome in ("forbidden", "missing", "echo", "redirect", "invalid-output")
    endpoint = java_agent + "/" + (outcome if special else route)
    registry.create_version("test-agent", "java", endpoint, credential_id=cid, http_auth=auth,
                            http_headers=headers, response_mapping={"answer_path": "reply"}, max_retries=0,
                            request_mapping={"message": "input.text"})
    launch = LaunchService(manager, registry).create_launch(agent_id="test-agent", agent_version="java", dataset_name="fixture",
        evaluator_ids=["answer_present"], quality_policy_rules=[{"evaluator_id": "answer_present", "operator": "==", "result_type": "boolean", "expected_value": True, "required": True}], dataset_snapshot={"dataset_id": "fixture", "dataset_version": "1", "items": [
            {"id": str(i), "input": {"text": "hello"}, "expected_output": {}} for i in range(2)]})
    frozen = copy.deepcopy(launch.manifest)
    assert frozen["agent"]["http_headers"] == headers
    if outcome == "success" and scheme in ("bearer", "api_key"):
        assert client.post(f"/api/v1/credentials/{cid}/rotate", json={"secret": "fixture-second-token"}).status_code == 200
        httpx.post(java_agent + "/rotate")
    if outcome == "bad-auth":
        # 无主认证也验证错误敏感 Header，不能伪装匿名模式有 Token。
        bad_id = cid or extra["id"]
        bad_secret = {"username": "wrong", "password": "wrong"} if scheme == "basic" else "fixture-wrong-token"
        assert client.post(f"/api/v1/credentials/{bad_id}/rotate", json={"secret": bad_secret}).status_code == 200
    if outcome == "disabled":
        assert client.post(f"/api/v1/credentials/{extra['id']}/disable", json={"confirm_name": extra["name"], "force": True}).status_code == 200
    orchestrator.start_launch(launch.id)
    with patch("app.worker.get_langfuse_client_safe", return_value=None):
        messages = queue.read_group("java-http", count=10)
        assert len(messages) == 2
        for message in messages:
            assert asyncio.run(worker.execute_item_message(*message))
    reconciler.reconcile_launch_states()
    assert httpx.get(java_agent + "/stats").json()["calls"] == (0 if outcome == "disabled" else 2)
    with manager.get_session() as session:
        persisted = session.get(ExperimentLaunchRecord, launch.id)
        assert persisted.manifest == frozen
        items = session.scalars(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id)).all()
        attempts = session.scalars(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id.in_([item.id for item in items]))).all()
        assert len(attempts) == 2
        if outcome == "success":
            assert all(item.execution_status == "succeeded" and item.quality_conclusion == "pass" for item in items)
            outputs = session.scalars(select(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.launch_id == launch.id)).all()
            assert len(outputs) == 2
            assert {o.agent_output["answer"] for o in outputs} == {"java business answer"}
            assert {o.agent_output["reply"] for o in outputs} == {"java business answer"}
            assert len({o.agent_output["sessionKey"] for o in outputs}) == 2
            for attempt in attempts:
                assert len(attempt.credential_usage) == (1 if scheme == "none" else 2)
                assert attempt.request_phase == "RESPONSE_RECEIVED"
                response = client.get("/api/v1/execution-attempts", params={"item_execution_id": attempt.item_execution_id})
                assert response.status_code == 200
                assert response.json()[0]["credential_usage"] == attempt.credential_usage
            if scheme in ("bearer", "api_key"):
                assert all(attempt.credential_version == 2 for attempt in attempts)
        else:
            assert all(item.execution_status == "failed" and item.quality_conclusion != "pass" for item in items)
            if outcome == "disabled":
                assert all(a.error_type == "CREDENTIAL_UNAVAILABLE" and a.request_phase == "PREPARED" for a in attempts)
            elif outcome in ("bad-auth", "missing-header", "forbidden", "missing"):
                assert all(a.http_status in (401, 403, 404) and a.error_type == "HTTP_4XX" for a in attempts)
            elif outcome == "echo":
                assert all(a.error_type == "SENSITIVE_RESPONSE" for a in attempts)
            else:
                assert all(a.error_type == "INVALID_RESPONSE" for a in attempts)
        create_result_snapshot(session, persisted)
        session.commit()
    # 凭据不能出现在数据库明文字段/冻结证据中（密文中随机巧合概率可忽略）。
    contents = Path(manager.engine.url.database).read_bytes()
    assert b"fixture-first-token" not in contents and b"fixture-second-token" not in contents
    assert b"fixture-extra-secret" not in contents and b"fixture password" not in contents
    assert "fixture-first-token" not in json.dumps(frozen)
