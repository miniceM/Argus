"""#255：冻结项目路由、凭据权限与三条链路隔离。"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.db_models import ExecutionAttemptRecord, ExperimentItemExecutionRecord
from app.manifest import LaunchService
from sqlalchemy import select

pytest_plugins = ["test_credential_resources"]


def binding(credentials, project="project-b"):
    client, service, registry, _ = credentials
    record = service.create(project, "production", {"username": "pk-" + project, "password": "sk-example"},
                            "test", type="basic_auth", allowed_origins=["https://lf.example"])
    connection = {"base_url": "https://lf.example", "project_id": project, "credential_id": record["id"]}
    return client, service, registry, connection


def test_binding_requires_admin_and_is_retained_by_credential_usage(credentials):
    client, _, registry, connection = binding(credentials)
    payload = {"agent_id": "test-agent", "version": "bound", "endpoint": "https://agent.example/invoke",
               "langfuse_connection": connection}
    client.headers.pop("Authorization")
    assert client.post("/api/v1/agent-versions", json=payload).status_code == 401
    client.headers["Authorization"] = "Bearer example-admin-token"
    response = client.post("/api/v1/agent-versions", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["langfuse_connection"] == connection
    assert registry.get_version("test-agent", "bound").environment == "production"
    usage = client.get(f"/api/v1/credentials/{connection['credential_id']}/usage").json()
    assert usage["versions"][0]["version"] == "bound"
    assert client.delete(f"/api/v1/credentials/{connection['credential_id']}",
                         params={"confirm_name": "project-b"}).status_code == 409


def test_connection_target_and_environment_are_enforced(credentials):
    _, _, registry, connection = binding(credentials)
    for changes in ({"base_url": "https://other.example"}, {"base_url": "https://user:secret@lf.example"},
                    {"base_url": "https://lf.example/path"}, {"project_id": " "}):
        with pytest.raises(ValueError):
            registry.create_version("test-agent", "bad", "https://agent.example/invoke",
                                    langfuse_connection={**connection, **changes})
    with pytest.raises(ValueError):
        registry.create_version("test-agent", "bad-env", "https://agent.example/invoke",
                                environment="staging", langfuse_connection=connection)


def test_admin_yaml_import_retains_project_binding(credentials, tmp_path):
    _, _, registry, connection = binding(credentials)
    path = tmp_path / "agents.yaml"
    path.write_text(json.dumps({"agents": {"yaml-bound": {"versions": {"v1": {
        "endpoint": "https://agent.example/invoke", "langfuse_connection": connection,
    }}}}}))
    assert registry.import_yaml(path)["created"] == ["yaml-bound:v1"]
    assert registry.get_version("yaml-bound", "v1").langfuse_connection == connection


def test_bound_dataset_uses_same_frozen_connection_and_not_global_project(credentials, setup_runtime, monkeypatch):
    from app.langfuse_connection import BoundLangfuseClient
    monkeypatch.setattr(BoundLangfuseClient, "verify_project", lambda _: None)

    _, _, registry, connection = binding(credentials)
    registry.create_version("test-agent", "bound", "https://agent.example/invoke", langfuse_connection=connection)
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "project-a")
    calls = []

    def get_dataset(client, name, *, version):
        calls.append((client.connection.project_id, client.connection.credential_id, name, version))
        return SimpleNamespace(id="dataset-b", items=[SimpleNamespace(id="b1", input={}, expected_output={})])

    monkeypatch.setattr(BoundLangfuseClient, "get_dataset", get_dataset)
    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "langfuse")
    launch = LaunchService(setup_runtime[0], registry).create_launch("test-agent", "bound", "dataset-b")
    assert launch.manifest["langfuse_connection"] == {**connection, "source": "BOUND", "credential_version": 1}
    assert calls[0][:3] == ("project-b", connection["credential_id"], "dataset-b")
    assert "sk-example" not in json.dumps(launch.manifest)


def test_global_manifest_records_source_without_rewriting_old_rows(setup_runtime):
    from app.registry import AgentRegistry

    launch = LaunchService(setup_runtime[0], AgentRegistry(setup_runtime[0])).create_launch(
        "test-agent", "v1", "banking-agent-regression")
    assert launch.manifest["langfuse_connection"]["source"] == "GLOBAL_ENV"


def test_bound_worker_fails_before_dispatch_when_credential_is_disabled(credentials, setup_runtime):
    from app.db_models import CredentialRecord
    from test_issue_49_cross_issue_acceptance import _binding, _manifest, _policy

    _, _, _, connection = binding(credentials)
    manager, queue, _, orchestrator, worker, _ = setup_runtime
    evaluator = _binding("intent_match")
    manifest = _manifest(evaluator.to_payload(), policy_payload=_policy([evaluator]).to_payload())
    manifest["langfuse_connection"] = {**connection, "source": "BOUND", "credential_version": 1}
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", "bound", manifest)
    frozen = copy.deepcopy(launch.manifest)
    with manager.get_session() as session:
        session.get(CredentialRecord, connection["credential_id"]).enabled = False
    orchestrator.start_launch(launch.id)
    message = queue.read_group("bound", count=1)[0]
    with patch("app.worker.get_langfuse_client_safe") as global_client, patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock,
    ) as invoke:
        assert asyncio.run(worker.execute_item_message(*message))
        invoke.assert_not_awaited()
        global_client.assert_not_called()
    with manager.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        attempt = session.scalar(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id == item.id))
        assert item.execution_error == "CREDENTIAL_UNAVAILABLE"
        assert attempt.request_phase == "PREPARED"
    assert launch.manifest == frozen


def test_scores_client_is_bound_and_rotation_does_not_mutate_manifest(credentials, setup_runtime, monkeypatch):
    import httpx
    from app.langfuse_connection import scores_client_for_manifest
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)

    _, service, _, connection = binding(credentials)
    manifest = {"langfuse_connection": {**connection, "source": "BOUND", "credential_version": 1},
                "comparison": {"environment": "production"}}
    frozen = copy.deepcopy(manifest)
    service.rotate(connection["credential_id"], {"username": "pk-next", "password": "sk-next"}, "test")
    with scores_client_for_manifest(setup_runtime[0], manifest) as client:
        def receive(request):
            assert request.headers["authorization"] == "Basic cGstbmV4dDpzay1uZXh0"
            return httpx.Response(200, json={"data": [], "meta": {}})
        client.http._transport = httpx.MockTransport(receive)
        assert client.scores_for("a" * 32, "b" * 16, "score") == []
        assert client.base_url == connection["base_url"]
    assert manifest == frozen

    from app.db_models import CredentialAuditRecord
    with setup_runtime[0].get_session() as session:
        audit = session.scalars(select(CredentialAuditRecord).where(
            CredentialAuditRecord.credential_id == connection["credential_id"],
            CredentialAuditRecord.action == "RESOLVE_LANGFUSE_SCORES",
        )).one()
        assert audit.version == 2


@pytest.mark.parametrize("export_fails", [False, True])
def test_bound_worker_and_otlp_use_project_credentials_without_global_sdk(credentials, setup_runtime, monkeypatch, export_fails):
    from app.db_models import LangfuseSyncTaskRecord
    from app.executor import SingleInvocationResult
    from app.langfuse_sync import LangfuseOutboxSyncer
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    from test_issue_49_cross_issue_acceptance import _binding, _manifest, _policy

    _, _, _, connection = binding(credentials)
    db, queue, _, orchestrator, worker, reconciler = setup_runtime
    evaluator = _binding("intent_match")
    manifest = _manifest(evaluator.to_payload(), policy_payload=_policy([evaluator]).to_payload())
    manifest["langfuse_connection"] = {**connection, "source": "BOUND", "credential_version": 1}
    manifest["dataset"].update(source="langfuse", dataset_id="ds-b", dataset_version="2026-11-12T00:00:00+00:00")
    monkeypatch.setenv("ARGUS_LANGFUSE_RUN_ITEM_STRATEGY", "legacy")
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", "bound", manifest)
    orchestrator.start_launch(launch.id)
    message = queue.read_group("bound", count=1)[0]
    result = SingleInvocationResult(status_code=200, body={"intent": "ok"}, raw_response="{}", headers={},
        duration_ms=1, trace_context_received=True, error_category=None, error_message=None,
        is_retryable=False, may_have_side_effects=False)
    with patch("app.worker.get_langfuse_client_safe") as global_client, patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock, return_value=result,
    ) as invoke, patch("opentelemetry.trace.get_tracer") as global_tracer:
        assert asyncio.run(worker.execute_item_message(*message))
        global_client.assert_not_called()
        global_tracer.assert_not_called()
        traceparent = invoke.call_args.args[1]["traceparent"]
    reconciler.reconcile_launch_states()
    with db.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        attempt = session.scalar(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id == item.id))
        assert traceparent == f"00-{item.trace_id}-{item.observation_id}-01"
        assert attempt.credential_usage[-1]["credential_id"] == connection["credential_id"]
        assert attempt.credential_usage[-1]["version"] == 1
        quality = item.quality_conclusion
    syncer = LangfuseOutboxSyncer(db, langfuse_client=lambda: pytest.fail("global SDK must not be used"))
    with patch("app.langfuse_sync.link_dataset_run_item", return_value="bound-run",
               side_effect=RuntimeError("sensitive upstream detail") if export_fails else None) as send:
        with patch("app.langfuse_connection.BoundLangfuseClient.__init__", lambda self, conn: setattr(self, "connection", conn)), patch(
            "app.langfuse_connection.BoundLangfuseClient.__exit__", return_value=None,
        ):
            # 本测试只覆盖 OTLP，跳过 Builtin Score 的额外写入。
            with patch("app.langfuse_sync._project_scores", return_value=[]):
                assert syncer.process_batch() == (0 if export_fails else 1)
        assert send.call_args.kwargs["strategy"] == "otel"
        assert send.call_args.kwargs["langfuse_public_key"] == "pk-project-b"
        assert send.call_args.kwargs["langfuse_secret_key"] == "sk-example"
        assert send.call_args.kwargs["observation_projection"]["span_name"] == "remote-agent-http"
    with db.get_session() as session:
        assert session.get(ExperimentItemExecutionRecord, message[1]).quality_conclusion == quality
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch.id))
        assert task.status == ("PENDING" if export_fails else "SYNCED")
        if export_fails:
            assert task.last_error == "LANGFUSE_SYNC_FAILED"


def test_bound_managed_contract_ignores_global_project_but_rejects_binding_mismatch(credentials, setup_runtime, monkeypatch):
    from app.managed_evaluation import ManagedEvaluationError, freeze_managed_evaluation

    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "project-a")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "global-env")
    contract = {"project_id": "project-b", "environment": "production", "target": {"name": "remote-agent-http", "type": "TOOL"},
                "scores": [{"name": "correctness", "data_type": "numeric", "required": True, "rule_id": "rule-b"}]}
    rules = [{"evaluator_id": "correctness", "result_type": "numeric", "operator": ">=", "threshold": 0.5}]
    frozen, _ = freeze_managed_evaluation(contract, environment="production", policy_rules=rules, project_id="project-b", bound=True)
    assert frozen["project_id"] == "project-b"
    with pytest.raises(ManagedEvaluationError, match="project"):
        freeze_managed_evaluation(contract, environment="production", policy_rules=rules, project_id="project-c", bound=True)


def test_otlp_redirect_does_not_forward_project_keys():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from app.langfuse_otel_projection import LangfuseProjectionError, post_langfuse_projection

    calls = []

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "/credential-sink")
            self.end_headers()

        def do_GET(self):
            calls.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        credentials = SimpleNamespace(base_url=f"http://127.0.0.1:{server.server_port}", public_key="pk-example", secret_key="sk-example")
        with pytest.raises(LangfuseProjectionError):
            post_langfuse_projection(b"example", credentials=credentials)
        assert calls == []
    finally:
        server.shutdown()
        thread.join(2)
        server.server_close()


@pytest.mark.parametrize("managed", [False, True])
def test_bound_synchronous_execution_uses_outbox_and_never_fetches_global_dataset(credentials, setup_runtime, monkeypatch, managed):
    import httpx
    from app.db_models import LangfuseSyncTaskRecord
    from app.execution import LaunchExecutionService
    from app.executor import RemoteAgentExecutor

    _, _, registry, connection = binding(credentials)
    db = setup_runtime[0]
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    registry.create_version("test-agent", "bound-sync", "https://agent.example/invoke", langfuse_connection=connection)
    evaluation = {"evaluator_ids": ["intent_match"]}
    if managed:
        from test_issue_215_managed_evaluation import contract, rules
        evaluation = {"evaluation_source": "langfuse_managed",
                      "managed_evaluation": {**contract(), "project_id": "project-b"}, "quality_policy_rules": rules()}
    launch = LaunchService(db, registry).create_launch("test-agent", "bound-sync", "dataset-b",
        **evaluation, dataset_snapshot={"source": "langfuse", "dataset_id": "dataset-b",
        "dataset_version": "2026-11-12T00:00:00+00:00", "items_count": 1, "snapshot_digest": "fixture",
        "items": [{"id": "case-sync", "input": {}, "expected_output": {"intent": "ok"}}]})
    received = []

    def target(request):
        received.append(request.headers["traceparent"])
        return httpx.Response(200, json={"answer": "ok", "intent": "ok"})

    transport = httpx.MockTransport(target)
    original = RemoteAgentExecutor.invoke

    async def invoke(self, *args, **kwargs):
        return await original(self, *args, **kwargs, client_transport=transport)

    monkeypatch.setattr(RemoteAgentExecutor, "invoke", invoke)
    with patch("app.execution.get_langfuse_client_safe", side_effect=AssertionError("global SDK")), patch(
        "app.langfuse_connection.BoundLangfuseClient.get_dataset", side_effect=AssertionError("frozen dataset refetched"),
    ):
        outcome = asyncio.run(LaunchExecutionService(db, registry).execute_launch(launch.id))
    assert outcome.launch.quality_conclusion == ("unknown" if managed else "pass")
    assert outcome.launch.langfuse_sync_status == "PENDING"
    with db.get_session() as session:
        task = session.scalar(select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch.id))
        assert received == [f"00-{task.trace_id}-{task.observation_id}-01"]
    if managed:
        assert setup_runtime[5].reconcile_managed_submissions() == 1
        messages = setup_runtime[1].read_group("managed-sync", count=10)
        from app.managed_score_recovery import WORK_TYPE_MANAGED
        assert len(messages) == 1 and messages[0][-1] == WORK_TYPE_MANAGED


@pytest.mark.parametrize("http_status,code", [(401, "AUTHENTICATION_FAILED"), (403, "AUTHORIZATION_FAILED"),
                                              (200, "MANAGED_PROJECT_MISMATCH")])
def test_bound_project_verification_never_falls_back_to_global(credentials, setup_runtime, http_status, code):
    import httpx
    from app.langfuse_connection import scores_client_for_manifest
    from app.langfuse_scores_client import ScoresReadError

    _, _, _, connection = binding(credentials)
    original = httpx.Client

    def receive(request):
        assert request.headers["authorization"] == "Basic cGstcHJvamVjdC1iOnNrLWV4YW1wbGU="
        return httpx.Response(http_status, json={"data": [{"id": "project-a", "name": "other", "metadata": {},
                                                "organization": {"id": "org", "name": "test"}}],
                                                "meta": {"page": 1, "limit": 100, "totalItems": 1, "totalPages": 1}})

    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(receive))

    with patch("app.langfuse_connection.httpx.Client", side_effect=client), patch(
        "app.langfuse_connection.ScoresV3Client.from_env", side_effect=AssertionError("global fallback"),
    ):
        with pytest.raises(ScoresReadError) as error:
            scores_client_for_manifest(setup_runtime[0], {"langfuse_connection": {**connection, "source": "BOUND"}})
        assert error.value.code == code


def test_bound_sync_disabled_credential_records_prepared_attempt(credentials, setup_runtime, monkeypatch):
    from app.db_models import CredentialRecord
    from app.execution import LaunchExecutionService

    _, _, registry, connection = binding(credentials)
    db = setup_runtime[0]
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    registry.create_version("test-agent", "bound-sync-disabled", "https://agent.example/invoke", langfuse_connection=connection)
    launch = LaunchService(db, registry).create_launch("test-agent", "bound-sync-disabled", "dataset-b",
        evaluator_ids=["intent_match"], dataset_snapshot={"source": "langfuse", "dataset_id": "dataset-b",
        "dataset_version": "2026-11-12T00:00:00+00:00", "items_count": 1, "snapshot_digest": "fixture",
        "items": [{"id": "case-sync", "input": {}, "expected_output": {"intent": "ok"}}]})
    with db.get_session() as session:
        session.get(CredentialRecord, connection["credential_id"]).enabled = False
    with patch("app.execution.get_langfuse_client_safe", side_effect=AssertionError("global SDK")), patch(
        "app.executor.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock,
    ) as invoke:
        outcome = asyncio.run(LaunchExecutionService(db, registry).execute_launch(launch.id))
        invoke.assert_not_awaited()
    assert outcome.launch.quality_conclusion == "unknown"
    with db.get_session() as session:
        item = session.scalar(select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch.id))
        attempt = session.scalar(select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.item_execution_id == item.id))
        assert attempt.request_phase == "PREPARED"
        assert attempt.error_type == "CREDENTIAL_UNAVAILABLE"


def test_bound_run_score_error_is_sanitized_and_uses_snapshot_connection(credentials, setup_runtime, monkeypatch, caplog):
    from app.db_models import ExperimentLaunchRecord, LangfuseRunScoreTaskRecord
    from app.langfuse_run_scores import LangfuseRunScoreSyncer
    from app.result_snapshots import create_result_snapshot
    from test_run_result_s3 import _create_completed_launch

    _, _, _, connection = binding(credentials)
    db = setup_runtime[0]
    launch_id = _create_completed_launch(db)
    with db.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.manifest["dataset"]["source"] = "langfuse"
        launch.manifest["langfuse_connection"] = {**connection, "source": "BOUND", "credential_version": 1}
        launch.langfuse_sync_status = "SYNCED"
        launch.langfuse_experiment_id = "run-b"
        snapshot = create_result_snapshot(session, launch)
    seen = []

    def init(client, resolved):
        seen.append(resolved.project_id)
        client.api = SimpleNamespace(scores=SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("sk-example"))))

    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.__init__", init)
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.__exit__", lambda *_: None)
    monkeypatch.setattr("app.langfuse_connection.BoundLangfuseClient.verify_project", lambda _: None)
    syncer = LangfuseRunScoreSyncer(db, lambda: pytest.fail("global client used"))
    assert syncer.process_batch() == 1
    assert seen == ["project-b"]
    assert "sk-example" not in caplog.text
    with db.get_session() as session:
        task = session.scalar(select(LangfuseRunScoreTaskRecord).where(LangfuseRunScoreTaskRecord.snapshot_id == snapshot.id))
        assert task.last_error == "LANGFUSE_SCORE_PUBLISH_FAILED"
