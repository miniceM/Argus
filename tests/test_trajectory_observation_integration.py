"""S4.0: genuine local ingestion, production invocation and API lineage.

Run explicitly with the prepared isolated Agent environment and Docker images.
Connection/setup failures fail this gate; they never fall back to Cloud or skip.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest
from app.db_models import AgentVersionRecord, ExecutionAttemptRecord, ExperimentItemExecutionRecord
from app.evaluator_binding import freeze_binding
from app.evaluators import default_evaluator_registry
from app.runner_identity import current_runner_identity
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from trajectory_agents.local_runtime import AgentProcess, LocalLangfuse, assert_lineage  # noqa: E402

pytestmark = pytest.mark.trajectory_local


@pytest.fixture(scope="module")
def local_langfuse(tmp_path_factory):
    with LocalLangfuse(tmp_path_factory.mktemp("s40")) as runtime:
        yield runtime


@pytest.fixture(scope="module")
def s40_runtime(tmp_path_factory):
    from app.db import DatabaseManager, MigrationRunner
    from app.limiter import MemoryAgentLimiter
    from app.orchestrator import LaunchOrchestrator
    from app.queue import MemoryQueueAdapter
    from app.reconciler import ExecutionReconciler
    from app.registry import AgentRegistry
    from app.worker import ExecutionWorker

    directory = tmp_path_factory.mktemp("s40-db")
    db = DatabaseManager(f"sqlite:///{directory / 'runner.db'}")
    MigrationRunner(db.engine, Path(__file__).resolve().parents[1] / "migrations").apply_all()
    AgentRegistry(db).create_agent("test-agent", "S4 cross-framework fixture")
    queue, limiter = MemoryQueueAdapter(), MemoryAgentLimiter()
    yield (db, queue, limiter, LaunchOrchestrator(db, queue, limiter),
           ExecutionWorker(db, queue, limiter, worker_id="s40-worker"), ExecutionReconciler(db, queue, limiter))
    db.engine.dispose()


def frozen_manifest(endpoint, scenario="serial", *, db, profile):
    from app.registry import AgentRegistry
    identity = current_runner_identity().model_dump()
    registry = default_evaluator_registry
    binding = freeze_binding(
        registry.definition("intent_match"), registry.version("intent_match", "1.0.0"),
        runner_identity=identity,
    ).to_payload()
    version = profile + "-" + uuid.uuid4().hex[:12]
    registered = AgentRegistry(db).create_version("test-agent", version, endpoint,
        request_mapping={"operation": "input.operation", "customer_id": "input.customer_id"},
        is_idempotent=True, timeout_seconds=30, max_retries=2, max_concurrency=1, rate_limit_per_minute=60)
    return {
        "schema_version": "1.2", "runner": identity,
        "agent": {"agent_id": "test-agent", "version": version, "agent_version_id": registered.id,
                  "spec_digest": registered.spec_digest,
                  "endpoint": endpoint, "method": "POST",
                  "request_mapping": {"operation": "input.operation", "customer_id": "input.customer_id"},
                  "is_idempotent": True},
        "execution_policy": {"timeout_seconds": 30, "max_retries": 2, "max_concurrency": 1,
                             "rate_limit_per_minute": 60},
        "dataset": {"version": "fixture-v1", "items": [{"id": "synthetic-case", "input": {
            "operation": scenario, "customer_id": "synthetic-001"},
            "expected_output": {"expected_intent": "balance"}}]},
        "evaluators": [binding],
    }


def invoke_worker(s40_runtime, monkeypatch, runtime, endpoint, scenario="serial"):
    db, queue, _limiter, orchestrator, worker, _reconciler = s40_runtime
    manifest = frozen_manifest(endpoint, scenario, db=db, profile=runtime.agent_profiles[endpoint])
    launch = orchestrator.create_launch("test-agent", manifest["agent"]["version"], "s40-local", "fixture-v1", "S40", manifest)
    orchestrator.start_launch(launch.id)
    monkeypatch.setattr("app.worker.get_langfuse_client_safe", lambda: runtime.sdk)
    message = queue.read_group("s40", count=1)[0]
    assert asyncio.run(worker.execute_item_message(*message)) is True
    runtime.sdk.flush()
    with db.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, message[1])
        attempts = session.scalars(select(ExecutionAttemptRecord).where(
            ExecutionAttemptRecord.item_execution_id == item.id)).all()
        assert item.launch_id == launch.id and item.dataset_item_id == "synthetic-case"
        assert item.dispatch_generation == message[2]
        version = session.get(AgentVersionRecord, launch.agent_version_id)
        assert version.agent_id == launch.agent_id == "test-agent"
        assert version.endpoint == endpoint and version.spec_digest == manifest["agent"]["spec_digest"]
        assert len(attempts) == 1 and item.final_attempt_id == attempts[0].id
        assert item.execution_status.lower() == "succeeded", item.execution_error
        assert item.eval_status.lower() == "succeeded", item.eval_error
        assert item.scores == {"intent_match": 1.0}
        assert item.quality_conclusion.lower() == "pass"
        assert item.trace_id and item.observation_id
        return {"launch_id": launch.id, "item_id": item.id, "dataset_item_id": item.dataset_item_id,
                "generation": item.dispatch_generation, "attempt_id": attempts[0].id,
                "agent_version_id": version.id, "agent_version": version.version, "agent_spec_digest": version.spec_digest,
                "trace_id": item.trace_id, "root_id": item.observation_id}


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
@pytest.mark.parametrize("repeat", range(3))
def test_real_worker_serial(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile, repeat):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint)
        observations = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        assert_lineage(observations, identity)
        agent_roots = [row for row in observations if row["name"] == f"{profile}.business"]
        assert len(agent_roots) == 1
        assert agent_roots[0]["parentObservationId"] == identity["root_id"]
        phases = [row["metadata"].get("attributes.workflow.phase") for row in observations]
        assert phases.count("REQUESTED") == 2 and phases.count("EXECUTED") == 2
        prefix = "account_" if profile == "pydantic_ai" else ""
        completed = next(row for row in observations if row.get("metadata", {}).get("attributes.gen_ai.tool.name") ==
                         prefix + "crm_lookup" and row["metadata"].get("attributes.workflow.phase") == "EXECUTED")
        request = next(row for row in observations if row.get("metadata", {}).get("attributes.gen_ai.tool.name") ==
                       prefix + "ledger_read" and row["metadata"].get("attributes.workflow.phase") == "REQUESTED")
        assert completed["metadata"]["attributes.workflow.status"] == "SUCCEEDED"
        assert request["metadata"]["attributes.workflow.predecessor_event_ids"] == [
            completed["metadata"]["attributes.workflow.event_id"]]
        assert completed["endTime"] <= request["startTime"]
        for row in observations:
            if row.get("metadata", {}).get("attributes.workflow.phase") == "REQUESTED":
                assert row["metadata"]["attributes.workflow.arguments_visibility"] == "AVAILABLE"
                assert json.loads(row["input"]) == {"customer_id": "synthetic-001"}
        local_langfuse.record(profile, "serial", identity, observations)


def test_http_only_preserves_answer_evaluation(local_langfuse, s40_runtime, monkeypatch, tmp_path):
    with AgentProcess(local_langfuse, tmp_path, "http_only") as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint)
        observations = local_langfuse.wait_observations(identity["trace_id"], minimum=2)
        assert {row["name"] for row in observations} == {"remote-agent-http", "eval_item_execution:synthetic-case"}
        assert_lineage(observations, identity)
        local_langfuse.record("http_only", "serial", identity, observations)


def test_sync_entry_injects_remote_observation_parent(local_langfuse, s40_runtime, tmp_path):
    from app.execution import _execute_single_item
    from app.executor import RemoteAgentExecutor
    from app.registry import AgentVersionSpec

    db, _queue, _limiter, orchestrator, _worker, _reconciler = s40_runtime
    with AgentProcess(local_langfuse, tmp_path, "native_otel") as agent:
        manifest = frozen_manifest(agent.endpoint, db=db, profile="native_otel")
        launch = orchestrator.create_launch("test-agent", manifest["agent"]["version"], "s40-sync", "fixture-v1", "S40 sync", manifest)
        agent_spec = {key: value for key, value in manifest["agent"].items() if key != "agent_version_id"}
        spec = AgentVersionSpec(**agent_spec, **manifest["execution_policy"], id=manifest["agent"]["agent_version_id"])
        executor = RemoteAgentExecutor(spec, credential_db_manager=db)
        async def run():
            try:
                with local_langfuse.sdk.start_as_current_observation(name="s40-sync-envelope"):
                    return await _execute_single_item(db, executor, launch.id, manifest["dataset"]["items"][0],
                                                      spec, manifest, local_langfuse.sdk)
            finally:
                await executor._client.aclose()
        asyncio.run(run())
        local_langfuse.sdk.flush()
        with db.get_session() as session:
            item = session.scalar(select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == launch.id))
            assert item.execution_status == "succeeded"
            version = session.get(AgentVersionRecord, launch.agent_version_id)
            identity = {"launch_id": launch.id, "item_id": item.id, "dataset_item_id": item.dataset_item_id,
                        "generation": item.dispatch_generation, "attempt_id": item.final_attempt_id,
                        "agent_version_id": version.id, "agent_version": version.version,
                        "agent_spec_digest": version.spec_digest, "trace_id": item.trace_id, "root_id": item.observation_id}
        observations = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        agent_root = next(row for row in observations if row["name"] == "native_otel.business")
        assert agent_root["parentObservationId"] == identity["root_id"]
        local_langfuse.record("native_otel", "sync", identity, observations)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
@pytest.mark.parametrize("scenario,expected_status", [("blocked", "CANCELLED"), ("failed", "FAILED"),
                                                        ("cancelled", "CANCELLED")])
def test_business_event_statuses(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile,
                                 scenario, expected_status):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario)
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        assert_lineage(rows, identity)
        phase = "BLOCKED" if scenario == "blocked" else "EXECUTED"
        selected = [row for row in rows if row.get("metadata", {}).get("attributes.workflow.phase") == phase
                    and row["metadata"].get("attributes.workflow.status") == expected_status]
        assert len(selected) == 1
        execution = selected[0]
        request = next(row for row in rows if row.get("metadata", {}).get("attributes.workflow.phase") == "REQUESTED"
                       and row["metadata"]["attributes.workflow.call_id"] == execution["metadata"]["attributes.workflow.call_id"])
        assert execution["metadata"]["attributes.workflow.predecessor_event_ids"] == [
            request["metadata"]["attributes.workflow.event_id"]]
        if scenario == "blocked":
            assert execution["metadata"]["attributes.business.tool_executed"] is False
        if profile == "pydantic_ai":
            assert any(row["type"] == "GENERATION" for row in rows), "must execute the real framework"
        local_langfuse.record(profile, scenario, identity, rows)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
@pytest.mark.parametrize("scenario,visibility", [("missing", "MISSING"), ("redacted", "REDACTED"),
                                                 ("uncollected", "UNCOLLECTED")])
def test_arguments_visibility(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile, scenario, visibility):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario)
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        request = next(row for row in rows if row.get("metadata", {}).get("attributes.workflow.arguments_visibility") == visibility)
        if visibility == "MISSING":
            assert isinstance(request["input"], str) and "customer_id" not in json.loads(request["input"])
        else:
            assert request["input"] is None
            hidden = request["metadata"].get("attributes.workflow.hidden_paths")
            assert (hidden is not None) == (visibility == "REDACTED")
        execution = next(row for row in rows if row.get("metadata", {}).get("attributes.workflow.call_id") ==
                         request["metadata"]["attributes.workflow.call_id"] and row["id"] != request["id"])
        assert isinstance(execution["metadata"]["attributes.workflow.duration_ms"], (int, float))
        assert execution["metadata"]["attributes.workflow.duration_ms"] >= 0
        assert_lineage(rows, identity)
        local_langfuse.record(profile, scenario, identity, rows)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
def test_parallel_has_no_sibling_dependency(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, "parallel")
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        requests = [row for row in rows if row.get("metadata", {}).get("attributes.workflow.phase") == "REQUESTED"]
        assert len(requests) == 2
        assert len({row["metadata"]["attributes.workflow.scope_id"] for row in requests}) == 1
        assert all(row["metadata"]["attributes.workflow.predecessor_event_ids"] == [] for row in requests)
        if profile == "pydantic_ai":
            assert any(row["type"] == "GENERATION" for row in rows)
        else:
            assert len({row["parentObservationId"] for row in requests}) == 1
        assert_lineage(rows, identity)
        local_langfuse.record(profile, "parallel", identity, rows)


@pytest.mark.parametrize("mode", ["late_all", "terminal_first"])
def test_late_visibility_and_terminal_inventory(local_langfuse, s40_runtime, monkeypatch, tmp_path, mode):
    with AgentProcess(local_langfuse, tmp_path, "native_otel", export_mode=mode) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint)
        initial = local_langfuse.wait_observations(identity["trace_id"], minimum=2 if mode == "late_all" else 4,
                                                 require_inventory=False)
        if mode == "late_all":
            assert {row["name"] for row in initial} == {"remote-agent-http", "eval_item_execution:synthetic-case"}
        else:
            terminal = next(row for row in initial if row["name"] == "workflow.terminal")
            expected = set(terminal["metadata"]["attributes.workflow.event_ids"])
            observed = {row["metadata"].get("attributes.workflow.event_id") for row in initial}
            assert expected - observed, "terminal alone must not imply all earlier events arrived"
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        terminal = next(row for row in rows if row["name"] == "workflow.terminal")
        expected = set(terminal["metadata"]["attributes.workflow.event_ids"])
        observed = {row["metadata"].get("attributes.workflow.event_id") for row in rows}
        assert expected <= observed
        assert terminal["metadata"]["attributes.workflow.final_sequence"] == len(expected)
        assert_lineage(rows, identity)
        local_langfuse.record("native_otel", mode, identity, rows)


def test_retry_separates_real_attempt_traces(local_langfuse, s40_runtime, monkeypatch, tmp_path):
    import time
    db, queue, _limiter, orchestrator, worker, reconciler = s40_runtime
    with AgentProcess(local_langfuse, tmp_path, "native_otel") as agent:
        manifest = frozen_manifest(agent.endpoint, "retry", db=db, profile="native_otel")
        launch = orchestrator.create_launch("test-agent", manifest["agent"]["version"], "s40-retry", "fixture-v1", "Retry", manifest)
        orchestrator.start_launch(launch.id)
        monkeypatch.setattr("app.worker.get_langfuse_client_safe", lambda: local_langfuse.sdk)
        message = queue.read_group("s40-retry", count=1)[0]
        assert asyncio.run(worker.execute_item_message(*message))
        local_langfuse.sdk.flush()
        with db.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, message[1])
            assert item.execution_status == "retry_wait"
            first = {"launch_id": launch.id, "item_id": item.id, "dataset_item_id": item.dataset_item_id,
                     "generation": item.dispatch_generation, "attempt_id": item.final_attempt_id,
                     "trace_id": item.trace_id, "root_id": item.observation_id}
        first_rows = local_langfuse.wait_observations(first["trace_id"], minimum=8)
        assert_lineage(first_rows, first)
        deadline = time.monotonic() + 10
        while reconciler.reconcile_retry_waits() == 0 and time.monotonic() < deadline:
            time.sleep(0.2)
        next_message = queue.read_group("s40-retry", count=1)[0]
        assert asyncio.run(worker.execute_item_message(*next_message))
        local_langfuse.sdk.flush()
        with db.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, message[1])
            attempts = session.scalars(select(ExecutionAttemptRecord).where(
                ExecutionAttemptRecord.item_execution_id == item.id).order_by(ExecutionAttemptRecord.attempt_no)).all()
            assert [row.http_status for row in attempts] == [503, 200]
            assert item.final_attempt_id == attempts[1].id != first["attempt_id"]
            assert all(row.dispatch_generation == item.dispatch_generation for row in attempts)
            assert item.execution_status == "succeeded" and item.eval_status == "succeeded"
            second = {**first, "attempt_id": item.final_attempt_id, "trace_id": item.trace_id,
                      "root_id": item.observation_id}
        second_rows = local_langfuse.wait_observations(second["trace_id"], minimum=8)
        assert_lineage(second_rows, second)
        assert first["trace_id"] != second["trace_id"] and first["root_id"] != second["root_id"]
        assert not ({row["id"] for row in first_rows} & {row["id"] for row in second_rows})
        local_langfuse.record("native_otel", "retry-failed", first, first_rows)
        local_langfuse.record("native_otel", "retry-success", second, second_rows)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
def test_trusted_human_source(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile):
    with AgentProcess(local_langfuse, tmp_path, profile, approval=True) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, "approved")
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=10)
        human = [row for row in rows if row.get("metadata", {}).get("attributes.workflow.phase") == "HUMAN"]
        assert len(human) == 1
        metadata = human[0]["metadata"]
        assert metadata["attributes.workflow.source"] == "isolated-approval-system-v1"
        assert metadata["resourceAttributes.service.name"] == "s4-approval-system"
        assert metadata["attributes.workflow.event_id"]
        terminal = next(row for row in rows if row["name"] == "workflow.terminal")
        assert metadata["attributes.workflow.scope_id"] == terminal["metadata"]["attributes.workflow.scope_id"]
        assert metadata["attributes.workflow.event_id"] in terminal["metadata"]["attributes.workflow.event_ids"]
        assert_lineage(rows, identity)
        local_langfuse.record(profile, "approved", identity, rows)


@pytest.mark.parametrize("scenario", ["serial", "escalate"])
def test_closed_approval_source_without_human_event(local_langfuse, s40_runtime, monkeypatch, tmp_path, scenario):
    with AgentProcess(local_langfuse, tmp_path, "native_otel", approval=True) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario)
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=10 if scenario == "serial" else 12)
        inventories = [row for row in rows if row["name"] == "approval.inventory"]
        assert len(inventories) == 1
        metadata = inventories[0]["metadata"]
        assert metadata["attributes.approval.event_count"] == 0
        assert metadata["attributes.approval.scope_closed"] is True
        assert metadata["resourceAttributes.service.name"] == "s4-approval-system"
        assert not any(row.get("metadata", {}).get("attributes.workflow.phase") == "HUMAN" for row in rows)
        if scenario == "escalate":
            assert any(row.get("metadata", {}).get("attributes.gen_ai.tool.name") == "escalate" for row in rows)
        assert_lineage(rows, identity)
        local_langfuse.record("native_otel", "approval-zero-" + scenario, identity, rows)
