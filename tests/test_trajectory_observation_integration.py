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


def frozen_manifest(endpoint, scenario="serial", *, db, profile, trajectory_source_profile_id=None,
                    trajectory_contract=None, rule_selections=None, quality_policy_rules=None):
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
        is_idempotent=True, timeout_seconds=30, max_retries=2, max_concurrency=1, rate_limit_per_minute=60,
        trajectory_source_profile_id=trajectory_source_profile_id)
    manifest = {
        "schema_version": "1.2", "runner": identity,
        "agent": {"agent_id": "test-agent", "version": version, "agent_version_id": registered.id,
                  "spec_digest": registered.spec_digest,
                  "endpoint": endpoint, "method": "POST",
                  "request_mapping": {"operation": "input.operation", "customer_id": "input.customer_id"},
                  "is_idempotent": True,
                  **({"trajectory_source_profile_id": trajectory_source_profile_id} if trajectory_source_profile_id else {})},
        "execution_policy": {"timeout_seconds": 30, "max_retries": 2, "max_concurrency": 1,
                             "rate_limit_per_minute": 60},
        "dataset": {"version": "fixture-v1", "items": [{"id": "synthetic-case", "input": {
            "operation": scenario, "customer_id": "synthetic-001"},
            "expected_output": {"expected_intent": "balance"}}]},
        "evaluators": [binding],
    }
    if trajectory_contract is not None:
        from app.evaluator_binding import manifest_measurement_digest
        from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract
        manifest["schema_version"] = "1.3"
        manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(trajectory_contract), registered)
        if rule_selections:
            from app.evaluator_binding import EvaluatorBinding
            from app.quality_policy import QualityRule, default_quality_policy, freeze_quality_policy
            for selected in rule_selections:
                manifest["evaluators"].append(freeze_binding(registry.definition(selected["id"]),
                    registry.version(selected["id"], "1.0.0"), runner_identity=identity, params=selected["params"]).to_payload())
            manifest["quality_policy"] = default_quality_policy([EvaluatorBinding.from_payload(b) for b in manifest["evaluators"]]).to_payload()
            if quality_policy_rules is not None:
                manifest["quality_policy"] = freeze_quality_policy(policy_id="custom", version="1.0",
                    rules=[QualityRule.from_payload(r) for r in quality_policy_rules],
                    bindings={b["id"]: EvaluatorBinding.from_payload(b) for b in manifest["evaluators"]}).to_payload()
        manifest["measurement_digest"] = manifest_measurement_digest(manifest)
        manifest["contract_digests"] = {"measurement": {"version": "1.3", "digest": manifest["measurement_digest"]}}
        if rule_selections:
            from app.comparison_contracts import aggregation_comparison_digest
            from app.required_evidence import COMPARISON_POLICY_VERSION, EVIDENCE_CONTRACT_VERSION
            manifest["evidence_contract_version"] = EVIDENCE_CONTRACT_VERSION
            manifest["comparison"] = {"environment": "production", "baseline_snapshot_id": None, "comparison_policy_version": COMPARISON_POLICY_VERSION}
            manifest["dataset"].update(source="seed", dataset_id="s40-local", dataset_version="fixture-v1")
            manifest["contract_digests"].update(quality_policy={"digest": manifest["quality_policy"]["policy_digest"], "version": "test-policy"},
                aggregation_comparison={"digest": aggregation_comparison_digest(evidence_contract_version=EVIDENCE_CONTRACT_VERSION), "version": COMPARISON_POLICY_VERSION})
    return manifest


def invoke_worker(s40_runtime, monkeypatch, runtime, endpoint, scenario="serial", *, trajectory_source_profile_id=None,
                  trajectory_contract=None, recovery_stage=None, rule_selections=None, quality_policy_rules=None, expected_quality="pass", expected_eval_status="succeeded"):
    db, queue, _limiter, orchestrator, worker, _reconciler = s40_runtime
    manifest = frozen_manifest(endpoint, scenario, db=db, profile=runtime.agent_profiles[endpoint],
                               trajectory_source_profile_id=trajectory_source_profile_id,
                               trajectory_contract=trajectory_contract, rule_selections=rule_selections, quality_policy_rules=quality_policy_rules)
    launch = orchestrator.create_launch("test-agent", manifest["agent"]["version"], "s40-local", "fixture-v1", "S40", manifest)
    orchestrator.start_launch(launch.id)
    monkeypatch.setattr("app.worker.get_langfuse_client_safe", lambda: runtime.sdk)
    message = queue.read_group("s40", count=1)[0]
    if recovery_stage:
        from datetime import UTC, datetime, timedelta

        from app.db_models import ExecutionCheckpointRecord
        original_collector = worker.trajectory_collector
        class Interrupted:
            async def collect(self, *args, **kwargs):
                if recovery_stage == "ready":
                    await original_collector.collect(*args, **kwargs)
                raise asyncio.CancelledError
        monkeypatch.setattr(worker, "trajectory_collector", Interrupted())
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(worker.execute_item_message(*message))
        with db.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, message[1])
            checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(
                ExecutionCheckpointRecord.item_execution_id == item.id))
            original_response = (checkpoint.created_at, checkpoint.output_digest, checkpoint.final_attempt_id)
            item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        assert _reconciler.reconcile_expired_leases() == 1
        recovered_message = queue.read_group("s40", count=1)[0]
        assert recovered_message[1:3] == message[1:3]
        message = recovered_message
        monkeypatch.setattr(worker, "trajectory_collector", original_collector)
        if recovery_stage == "ready":
            monkeypatch.setattr(original_collector, "_get_factory", lambda: pytest.fail("terminal resume opened Reader"))
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
        assert version.trajectory_source_profile_id == trajectory_source_profile_id
        assert len(attempts) == 1 and item.final_attempt_id == attempts[0].id
        if recovery_stage:
            checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(
                ExecutionCheckpointRecord.item_execution_id == item.id))
            assert (checkpoint.created_at, checkpoint.output_digest, checkpoint.final_attempt_id) == original_response
        assert item.execution_status.lower() == "succeeded", item.execution_error
        if item.eval_status.lower() != expected_eval_status:
            from app.db_models import EvaluationResultRecord
            diagnostics = session.execute(select(EvaluationResultRecord.evaluator_id, EvaluationResultRecord.status,
                EvaluationResultRecord.error_code).where(EvaluationResultRecord.item_execution_id == item.id)).all()
            pytest.fail(f"unexpected evaluation status {item.eval_status}: {diagnostics}")
        assert item.scores == {"intent_match": 1.0}
        assert item.quality_conclusion.lower() == expected_quality
        assert item.trace_id and item.observation_id
        return {"launch_id": launch.id, "item_id": item.id, "dataset_item_id": item.dataset_item_id,
                "generation": item.dispatch_generation, "attempt_id": attempts[0].id,
                "agent_version_id": version.id, "agent_version": version.version, "agent_spec_digest": version.spec_digest,
                "trace_id": item.trace_id, "root_id": item.observation_id}


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
@pytest.mark.parametrize("repeat", range(3))
def test_real_worker_serial(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile, repeat):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        source_profile_id = "pydantic-ai-v1" if profile == "pydantic_ai" else "native-otel-v1"
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint,
                                 trajectory_source_profile_id=source_profile_id)
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
        normalization = assert_real_normalized_structure(local_langfuse, s40_runtime, identity, source_profile_id)
        local_langfuse.record(profile, "serial", identity, observations, normalization=normalization)


@pytest.mark.parametrize("repeat", range(10))
def test_http_only_preserves_answer_evaluation(local_langfuse, s40_runtime, monkeypatch, tmp_path, repeat):
    with AgentProcess(local_langfuse, tmp_path, "http_only") as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint,
                                 trajectory_source_profile_id="http-only-v1")
        observations = local_langfuse.wait_observations(identity["trace_id"], minimum=2)
        assert {row["name"] for row in observations} == {"remote-agent-http", "eval_item_execution:synthetic-case"}
        assert_lineage(observations, identity)
        normalization = assert_real_normalized_structure(local_langfuse, s40_runtime, identity, "http-only-v1")
        local_langfuse.record("http_only", "serial", identity, observations, normalization=normalization)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai", "http_only"])
def test_checkpoint_acquisition_under_the_original_worker_lease(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile):
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal
    from threading import Event

    from app.db_models import ExecutionCheckpointRecord
    from app.registry import AgentRegistry
    from app.trajectory.checkpoint import load_trajectory_checkpoint
    from app.trajectory.codec import canonical_dumps
    from app.trajectory.contract import TrajectoryContract
    from app.trajectory.coordinator import TrajectoryCoordinator
    from app.trajectory.reader import LangfuseObservationReader
    from app.trajectory.schema import TrajectoryScope
    from trajectory_contract_fixtures import observed_banking_contract_payload

    db, _, _, _, worker, _ = s40_runtime
    persisted, resume_evaluation = Event(), Event()
    saved = worker._persist_execution_checkpoint
    checkpoint_arguments = {}
    def pause_after_response(**arguments):
        saved(**arguments)
        checkpoint_arguments.update(arguments)
        persisted.set()
        assert resume_evaluation.wait(timeout=20), "test did not release the saved-response seam"
    monkeypatch.setattr(worker, "_persist_execution_checkpoint", pause_after_response)
    profile_id = {"native_otel": "native-otel-v1", "pydantic_ai": "pydantic-ai-v1", "http_only": "http-only-v1"}[profile]
    with AgentProcess(local_langfuse, tmp_path, profile) as agent, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(invoke_worker, s40_runtime, monkeypatch, local_langfuse, agent.endpoint,
                             trajectory_source_profile_id=profile_id)
        try:
            assert persisted.wait(timeout=20)
            local_langfuse.sdk.flush()
            with db.get_session() as session:
                cp = session.scalar(select(ExecutionCheckpointRecord).where(
                    ExecutionCheckpointRecord.item_execution_id == checkpoint_arguments["item_id"]))
                item = session.get(ExperimentItemExecutionRecord, cp.item_execution_id)
                assert item.execution_status == "running" and item.active_attempt_id == cp.final_attempt_id
                scope = TrajectoryScope(launch_id=cp.launch_id, item_execution_id=cp.item_execution_id,
                    dataset_item_id=cp.dataset_item_id, attempt_id=cp.final_attempt_id, dispatch_generation=cp.dispatch_generation,
                    trace_id=cp.trace_id, root_observation_id=cp.observation_id)
                lease_token = item.lease_token
            version = AgentRegistry(db).get_version("test-agent", checkpoint_arguments["manifest"]["agent"]["version"])
            payload = observed_banking_contract_payload()
            payload["limits"] = {"page_size": 2, "deadline_seconds": Decimal(3), "quiet_window_seconds": Decimal("0.05")}
            contract = TrajectoryContract.model_validate(payload).bind_source_version(version)
            async def consume():
                deadline_race = False

                class CrossingCoordinator(TrajectoryCoordinator):
                    async def _publish(self, claim, contract, lease_token, result, source_bytes_used, candidate):
                        nonlocal deadline_race
                        if profile == "http_only" and result.state == "PENDING" and candidate is not None and not deadline_race:
                            # 在可信候选之后注入同一原截止；正式协调器负责收尾。
                            deadline_race = True
                            remaining = (result.deadline_at - self._clock.utcnow()).total_seconds()
                            await asyncio.sleep(max(0, remaining) + 0.01)
                        return await super()._publish(claim, contract, lease_token, result, source_bytes_used, candidate)

                async with LangfuseObservationReader(server_url=local_langfuse.base_url,
                        public_key=local_langfuse.env["S4_PUBLIC_KEY"], secret_key=local_langfuse.env["S4_SECRET_KEY"]) as reader:
                    result = await CrossingCoordinator(db.get_session).run(scope, contract, client=reader, lease_token=lease_token)
                    if profile == "http_only":
                        assert deadline_race
                    return result
            acquired = asyncio.run(consume())
            assert acquired.state == "READY", acquired.reason_code
            assert acquired.completeness.status == ("ROOT_ONLY" if profile == "http_only" else "OBSERVED_COMPLETE")
        finally:
            resume_evaluation.set()
        identity = future.result(timeout=20)  # 原 Worker 继续评测；既有成功与单 Attempt 断言仍执行。
    with db.get_session() as session:
        recovered = load_trajectory_checkpoint(session, scope, contract)
        assert canonical_dumps(recovered.acquisition) == canonical_dumps(acquired)
        assert recovered.candidate is not None
        assert recovered.source_bytes_used <= contract.limits.max_source_bytes
    rows = local_langfuse.wait_observations(identity["trace_id"], minimum=2 if profile == "http_only" else 8)
    assert_lineage(rows, identity)
    local_langfuse.record(profile, "checkpoint-acquisition", identity, rows, normalization={
        "profile_id": profile_id, "acquisition_state": acquired.state, "completeness": acquired.completeness.status,
        "artifact_roundtrip": True, "checkpoint_restored": True, "original_worker_lease": True,
        "production_coordinator": True})


def assert_real_normalized_structure(runtime, s40_runtime, identity, profile_id, *, scenario="serial",
                                     calibration_payload=None, started_at=None, capacity_boundaries=False):
    from decimal import Decimal

    from app.registry import AgentRegistry
    from app.trajectory.assembler import assemble_trajectory, trajectory_digest
    from app.trajectory.causal import validate_causal_links
    from app.trajectory.codec import canonical_dumps
    from app.trajectory.contract import TrajectoryContract
    from app.trajectory.evidence import inventory_complete, load_trajectory_evidence
    from app.trajectory.normalizer import normalize_observations
    from app.trajectory.reader import LangfuseObservationReader
    from app.trajectory.schema import TrajectoryScope
    from trajectory_contract_fixtures import observed_banking_contract_payload

    version = AgentRegistry(s40_runtime[0]).get_version("test-agent", identity["agent_version"])
    assert version.id == identity["agent_version_id"] and version.trajectory_source_profile_id == profile_id
    payload = calibration_payload or observed_banking_contract_payload()
    # 此处验证真实采集消费链，短观察窗不冒充生产延迟 SLA；校准另行归档。
    if calibration_payload is None:
        payload["limits"] = {"page_size": 2, "deadline_seconds": Decimal(3), "quiet_window_seconds": Decimal("0.05")}
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version)
    scope = TrajectoryScope(launch_id=identity["launch_id"], item_execution_id=identity["item_id"],
        dataset_item_id=identity["dataset_item_id"], attempt_id=identity["attempt_id"],
        dispatch_generation=identity["generation"], trace_id=identity["trace_id"], root_observation_id=identity["root_id"])
    async def read():
        rows, cursors, source_bytes = [], set(), 0
        cursor = None
        async with LangfuseObservationReader(server_url=runtime.base_url, public_key=runtime.env["S4_PUBLIC_KEY"],
                secret_key=runtime.env["S4_SECRET_KEY"]) as reader:
            for _ in range(contract.limits.max_pages):
                page = await reader.read_page(scope, cursor=cursor, page_size=2, timeout_seconds=5,
                                              max_bytes=contract.limits.max_source_bytes - source_bytes)
                rows.extend(page.rows)
                source_bytes += page.source_bytes
                cursor = page.cursor
                if cursor is None:
                    return rows, len(cursors), source_bytes
                assert cursor not in cursors
                cursors.add(cursor)
        pytest.fail("Production Reader did not exhaust real normalization pages")
    rows, cursor_hops, source_bytes = asyncio.run(read())
    assert len(rows) == len({row["id"] for row in rows})
    expected = {scope.root_observation_id}
    for _ in rows:
        expected.update(row["id"] for row in rows if row["parentObservationId"] in expected)
    batch = normalize_observations(rows, contract, scope=scope)
    assert batch.scope == scope and batch.trajectory_contract_digest == contract.digest
    assert batch.source_profile_id == profile_id and batch.source_bytes_complete and not batch.truncated
    normalized = [record.observation for record in batch.records]
    causal = validate_causal_links(scope, batch, contract)
    trajectory = assemble_trajectory(scope, batch, contract)
    assert trajectory.provenance.trajectory_digest == trajectory_digest(trajectory)
    assert canonical_dumps(trajectory) == canonical_dumps(
        assemble_trajectory(scope, normalize_observations(list(reversed(rows)), contract, scope=scope), contract))
    root_graph = causal.root_graph
    assert root_graph.depth_by_id[scope.root_observation_id] == 0 and not root_graph.truncated
    assert {record.observation_id for record in root_graph.records} == expected
    assert canonical_dumps(causal) == canonical_dumps(
        validate_causal_links(scope, normalize_observations(list(reversed(rows)), contract, scope=scope), contract)
    ), "real parent closure must be independent of API arrival order"
    scoped = [result for result in normalized if result.step and result.step.step_id in expected]
    assert {result.step.step_id for result in scoped} == expected, "a real framework parent bridge was discarded"
    structural = [result for result in scoped if result.structural]
    assert all(result.step.parent_step_id in expected for result in scoped if result.step.step_id != scope.root_observation_id)
    assert all(not result.warnings for result in structural)
    assert all(all(capability == "NONE" for capability in result.capabilities.values()) for result in structural)
    assert all(result.step.tool_identity is None and result.step.arguments_redacted is None for result in structural)
    tools = [result for result in scoped if result.step.type == "TOOL"]
    if profile_id == "http-only-v1":
        assert len(scoped) == 1 and not tools and not structural
        assert all(value == "NONE" for value in scoped[0].capabilities.values())
        assert causal.request_by_execution_id == {} and not any(causal.predecessors_by_id.values())
        assert trajectory.completeness.status == "ROOT_ONLY"
        assert all(value == "NONE" for value in trajectory.completeness.capabilities.values())
    else:
        assert cursor_hops and structural
        assert len(tools) == 4 and {result.step.phase for result in tools} == (
            {"REQUESTED", "EXECUTED", "BLOCKED"} if scenario == "blocked" else {"REQUESTED", "EXECUTED"})
        assert all(result.step.tool_identity is not None for result in tools)
        assert all(result.step.duration_ms is not None for result in tools if result.step.phase == "EXECUTED")
        assert len(causal.request_by_execution_id) == 2 and causal.warnings_by_id == {}
        assert sum(len(refs) for refs in causal.predecessors_by_id.values()) == (2 if scenario == "parallel" else 3)
        assert trajectory.completeness.status == "PARTIAL"  # 这条消费链尚未提供生产 Acquisition quiet 证明。
        assert "ACQUISITION_CONVERGENCE_UNPROVEN" in trajectory.completeness.warnings
        assert not any(reason.startswith("TERMINAL_INVENTORY_") for reason in trajectory.completeness.warnings)
        assembled_tools = [step for step in trajectory.steps if step.type == "TOOL"]
        assert sum(len(step.causal_predecessor_ids) for step in assembled_tools) == (2 if scenario == "parallel" else 3)
        if scenario in {"missing", "redacted", "uncollected"}:
            request = next(result for result in tools if result.step.phase == "REQUESTED"
                           and result.step.tool_identity == "banking.balance.read")
            expected_visibility = {"missing": {"$.customer_id": "MISSING"},
                                   "redacted": {"$.customer_id": "REDACTED"}, "uncollected": {}}[scenario]
            assert request.step.argument_visibility == expected_visibility
            action = next(step for step in assembled_tools if step.phase in {"EXECUTED", "BLOCKED"}
                          and step.tool_identity == "banking.balance.read")
            assert action.argument_visibility == expected_visibility
            assert "synthetic-secret" not in canonical_dumps(causal)
        assert "synthetic-secret" not in canonical_dumps(trajectory)
        assert inventory_complete(trajectory, contract)
    acquired, acquisition_bytes, _first = asyncio.run(
        acquire_real_trajectory(runtime, s40_runtime[0], scope, contract, started_at=started_at))
    assert acquired.state == "READY" and acquired.evidence_ref is not None, acquired.reason_code
    assert acquired.completeness.status == ("ROOT_ONLY" if profile_id == "http-only-v1" else "OBSERVED_COMPLETE")
    assert 0 < acquisition_bytes <= contract.limits.max_source_bytes
    if started_at is not None:
        from datetime import UTC, datetime, timedelta
        assert acquired.deadline_at == started_at + timedelta(seconds=float(contract.limits.deadline_seconds))
        response_to_ready_ms = (datetime.now(UTC) - started_at).total_seconds() * 1000
        # 截止内完成或在截止收尾；250ms 只允许测试主线程/DB 发布调度开销。
        assert response_to_ready_ms <= float(contract.limits.deadline_seconds) * 1000 + 250
    else:
        response_to_ready_ms = None
    with s40_runtime[0].get_session() as session:
        restored = load_trajectory_evidence(session, acquired.evidence_ref, scope=scope, contract=contract)
    assert canonical_dumps(restored.steps) == canonical_dumps(trajectory.steps)
    assert restored.completeness == acquired.completeness
    assert len(canonical_dumps(restored).encode()) <= contract.limits.max_artifact_bytes
    if capacity_boundaries:
        for limits, warning in (({"max_steps": 3}, "GRAPH_STEP_LIMIT"),
                               ({"max_artifact_bytes": len(canonical_dumps(restored).encode()) // 2},
                                "TRAJECTORY_ARTIFACT_LIMIT")):
            limited_payload = contract.model_dump()
            limited_payload["limits"].update(limits)
            limited = TrajectoryContract.model_validate(limited_payload).bind_source_version(version)
            # 使用生产 Reader 的 Decimal wire 解码结果，而非测试 HTTP 客户端的 float DTO。
            truncated = assemble_trajectory(scope, normalize_observations(rows, limited, scope=scope), limited)
            assert truncated.completeness.status == "PARTIAL" and warning in truncated.completeness.warnings
            assert scope.root_observation_id in {step.step_id for step in truncated.steps}
            assert len(truncated.steps) <= limited.limits.max_steps
            assert len(canonical_dumps(truncated).encode()) <= limited.limits.max_artifact_bytes
    with s40_runtime[0].get_session() as session:
        assert canonical_dumps(load_trajectory_evidence(session, acquired.evidence_ref, scope=scope, contract=contract)) == canonical_dumps(restored)
    single_read_completeness = trajectory.completeness.status
    trajectory = restored
    return {"profile_id": profile_id, "source_profile_version": contract.source_profile.version,
            "trajectory_contract_digest": contract.digest, "scoped_steps": len(scoped),
            "structural_steps": len(structural), "business_tool_steps": len(tools),
            "cursor_hops": cursor_hops, "source_bytes": source_bytes,
            "normalized_source_bytes": batch.source_bytes,
            "graph_nodes": len(root_graph.records), "graph_max_depth": max(root_graph.depth_by_id.values()),
            "causal_edges": sum(len(refs) for refs in causal.predecessors_by_id.values()),
            "call_pairs": len(causal.request_by_execution_id),
            "assembled_steps": len(trajectory.steps), "completeness": trajectory.completeness.status,
            "trajectory_digest": trajectory.provenance.trajectory_digest,
            "artifact_roundtrip": True,
            "artifact_bytes": len(canonical_dumps(restored).encode()),
            "response_to_ready_ms": response_to_ready_ms,
            "original_deadline_preserved": started_at is not None,
            "real_capacity_boundaries": capacity_boundaries,
            "acquisition_state": acquired.state, "acquisition_source_bytes": acquisition_bytes,
            "single_read_completeness": single_read_completeness,
            "parent_closed": True, "structural_tool_capabilities": "NONE"}


async def acquire_real_trajectory(runtime, db, scope, contract, *, started_at=None):
    from datetime import UTC, datetime

    from app.trajectory.acquisition import AcquisitionRuntime, acquire_trajectory
    from app.trajectory.reader import LangfuseObservationReader

    acquisition_runtime = AcquisitionRuntime(scope, contract, session_factory=db.get_session,
                                            started_at=started_at or datetime.now(UTC))
    first = None
    async with LangfuseObservationReader(server_url=runtime.base_url, public_key=runtime.env["S4_PUBLIC_KEY"],
            secret_key=runtime.env["S4_SECRET_KEY"]) as reader:
        with acquisition_runtime.activate():
            while True:
                result = await acquire_trajectory(scope, contract, client=reader)
                if first is None:
                    first = result
                assert result.deadline_at == first.deadline_at
                if result.state != "PENDING":
                    return result, acquisition_runtime._wire_bytes, first
                await asyncio.sleep(min(0.05, acquisition_runtime.remaining()))


def calibrated_banking_payload():
    """受控夹具的独立校准身份；不修改通用默认值或失败样本的预算。"""
    from decimal import Decimal

    from trajectory_contract_fixtures import observed_banking_contract_payload
    payload = observed_banking_contract_payload()
    payload["limits"] = {"deadline_seconds": Decimal(12), "quiet_window_seconds": Decimal("0.3"),
        "max_steps": 64, "page_size": 2, "max_pages": 20,
        "max_source_bytes": 1024 * 1024, "max_artifact_bytes": 64 * 1024}
    return payload


@pytest.mark.parametrize("profile,scenario", [
    (profile, scenario) for profile in ("native_otel", "pydantic_ai")
    for scenario in ("serial", "blocked", "parallel", "late_all")
] + [("http_only", "serial")])
def test_reader_qualification_repeated_real_samples(local_langfuse, s40_runtime, monkeypatch, tmp_path,
                                                    profile, scenario):
    import hashlib
    from datetime import UTC

    from app.db_models import ExecutionCheckpointRecord
    from app.registry import AgentRegistry
    from app.trajectory.contract import TrajectoryContract
    from app.trajectory.factory import QualifiedReaderFactory, ReaderApproval, reader_artifact_digest
    from app.trajectory.schema import TrajectoryScope

    profile_id = {"native_otel": "native-otel-v1", "pydantic_ai": "pydantic-ai-v1", "http_only": "http-only-v1"}[profile]
    api_digest = "sha256:" + hashlib.sha256((local_langfuse.directory / "langfuse-openapi.yaml").read_bytes()).hexdigest()
    artifact_digest = reader_artifact_digest()
    samples = []
    with AgentProcess(local_langfuse, tmp_path, profile,
                      export_mode="late_all" if scenario == "late_all" else "immediate") as agent:
        for _ in range(10):
            identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint,
                "serial" if scenario == "late_all" else scenario, trajectory_source_profile_id=profile_id)
            with s40_runtime[0].get_session() as session:
                cp = session.scalar(select(ExecutionCheckpointRecord).where(
                    ExecutionCheckpointRecord.item_execution_id == identity["item_id"]))
                assert cp.final_attempt_id == identity["attempt_id"] and cp.trace_id == identity["trace_id"]
                started_at = cp.created_at.replace(tzinfo=UTC) if cp.created_at.tzinfo is None else cp.created_at
            rows = local_langfuse.wait_observations(identity["trace_id"], minimum=2 if profile == "http_only" else 8)
            assert_lineage(rows, identity)
            normalization = assert_real_normalized_structure(local_langfuse, s40_runtime, identity, profile_id,
                scenario="serial" if scenario == "late_all" else scenario,
                calibration_payload=calibrated_banking_payload(), started_at=started_at,
                capacity_boundaries=profile != "http_only" and scenario == "serial")
            normalization.update(calibration_sample=True, reader_artifact_digest=artifact_digest,
                                 api_document_digest=api_digest, qualification_group=profile + ":" + scenario)
            if scenario == "late_all":
                assert normalization["response_to_ready_ms"] >= 7500, "8s delayed export was bypassed"
            local_langfuse.record(profile, "calibration-" + scenario, identity, rows, normalization=normalization)
            assert all(field["absent"] == 0 for field in local_langfuse.reports[-1]["fields"].values())
            samples.append(normalization)
        assert len(samples) == 10 and len({sample["trajectory_contract_digest"] for sample in samples}) == 1
        # 只有全部实测通过后，测试控制面才批准这个受控合同；生产部署须独立校准。
        version = AgentRegistry(s40_runtime[0]).get_version("test-agent", identity["agent_version"])
        contract = TrajectoryContract.model_validate(calibrated_banking_payload()).bind_source_version(version)
        scope = TrajectoryScope(launch_id=identity["launch_id"], item_execution_id=identity["item_id"],
            dataset_item_id=identity["dataset_item_id"], attempt_id=identity["attempt_id"],
            dispatch_generation=identity["generation"], trace_id=identity["trace_id"], root_observation_id=identity["root_id"])
        factory = QualifiedReaderFactory(server_url=local_langfuse.base_url,
            public_key=local_langfuse.env["S4_PUBLIC_KEY"], secret_key=local_langfuse.env["S4_SECRET_KEY"],
            approvals=(ReaderApproval(contract.digest, artifact_digest, api_digest),))
        async def read_qualified():
            async with factory.open(contract) as reader:
                page = await reader.read_page(scope, cursor=None, page_size=2, timeout_seconds=5,
                                              max_bytes=contract.limits.max_source_bytes)
                assert page.rows and all(row["traceId"] == scope.trace_id for row in page.rows)
            assert reader.is_closed
        asyncio.run(read_qualified())
        # metadata-only 记录追加后重新归档最后一个样本，避免保存原始业务数据。
        normalization["qualified_factory_read"] = True
        local_langfuse.record(profile, "calibration-" + scenario + "-admission", identity, rows,
                              normalization=normalization)


@pytest.mark.parametrize("profile,mode,approved", [("native_otel", "immediate", True),
    ("pydantic_ai", "immediate", True), ("http_only", "immediate", True),
    ("native_otel", "late_all", True), ("native_otel", "immediate", False)])
@pytest.mark.parametrize("recovery_stage", [None, "before_acquisition", "ready"])
def test_formal_worker_automatically_freezes_trajectory(local_langfuse, s40_runtime, monkeypatch, tmp_path,
                                                       profile, mode, approved, recovery_stage):
    from datetime import UTC, timedelta

    from app.db_models import ExecutionCheckpointRecord
    from app.trajectory.checkpoint import load_trajectory_checkpoint
    from app.trajectory.contract import TrajectoryContract
    from app.trajectory.factory import QualifiedReaderFactory, ReaderApproval, reader_artifact_digest
    from app.trajectory.manifest import load_trajectory_contract
    from app.trajectory.schema import TrajectoryScope
    from app.trajectory.worker_acquisition import WorkerTrajectoryCollector

    # 复用175已经实测的受控批准，不在本消费测试根据Trace或返回值自动批准。
    report = json.loads((Path(__file__).resolve().parents[1] / "docs/validation/s4-1/reader-calibration.json").read_text())
    approval = report["groups"][profile + ":serial"]["approved_identity"]
    contract = TrajectoryContract.model_validate(calibrated_banking_payload())
    assert contract.digest == approval["trajectory_contract_digest"]
    assert reader_artifact_digest() == approval["reader_artifact_digest"]
    factory = QualifiedReaderFactory(server_url=local_langfuse.base_url,
        public_key=local_langfuse.env["S4_PUBLIC_KEY"], secret_key=local_langfuse.env["S4_SECRET_KEY"],
        approvals=(ReaderApproval(**approval),) if approved else ())
    worker = s40_runtime[4]
    monkeypatch.setattr(worker, "trajectory_collector", WorkerTrajectoryCollector(s40_runtime[0].get_session,
        factory_provider=lambda: factory))
    profile_id = {"native_otel": "native-otel-v1", "pydantic_ai": "pydantic-ai-v1", "http_only": "http-only-v1"}[profile]
    from app.evaluator_binding import evaluate_frozen_item
    captured = []
    def evaluate(*args, **kwargs):
        captured.append(kwargs.get("context"))
        return evaluate_frozen_item(*args, **kwargs)
    for module in ("app.worker", "app.trajectory.execution_recovery", "app.evaluation_recovery"):
        monkeypatch.setattr(module + ".evaluate_frozen_item", evaluate)
    with AgentProcess(local_langfuse, tmp_path, profile, export_mode=mode) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint,
            trajectory_source_profile_id=profile_id, trajectory_contract=contract, recovery_stage=recovery_stage)
    with s40_runtime[0].get_session() as session:
        cp = session.scalar(select(ExecutionCheckpointRecord).where(
            ExecutionCheckpointRecord.item_execution_id == identity["item_id"]))
        from app.db_models import ExperimentLaunchRecord
        manifest = session.get(ExperimentLaunchRecord, identity["launch_id"]).manifest
        scope = TrajectoryScope(launch_id=identity["launch_id"], item_execution_id=identity["item_id"],
            dataset_item_id=identity["dataset_item_id"], attempt_id=identity["attempt_id"],
            dispatch_generation=identity["generation"], trace_id=identity["trace_id"], root_observation_id=identity["root_id"])
        saved = load_trajectory_checkpoint(session, scope, load_trajectory_contract(manifest))
        created = cp.created_at.replace(tzinfo=UTC) if cp.created_at.tzinfo is None else cp.created_at
        assert saved.acquisition.deadline_at == created + timedelta(seconds=12)
        assert saved.source_bytes_used <= contract.limits.max_source_bytes and saved.round_token is None
    acquired = saved.acquisition
    if approved:
        assert acquired.state == "READY"
        assert acquired.completeness.status == ("ROOT_ONLY" if profile == "http_only" else "OBSERVED_COMPLETE")
    else:
        assert acquired.state == "UNAVAILABLE" and acquired.reason_code == "TRAJECTORY_SOURCE_UNQUALIFIED"
        assert saved.source_bytes_used == 0 and acquired.evidence_ref is None
    assert len(captured) == 1 and captured[0].output == {"intent": "balance", "balance": "12.50"}
    assert captured[0].trajectory_evidence_ref == acquired.evidence_ref
    assert (captured[0].trajectory.scope == scope) if approved else captured[0].trajectory is None
    # 消费与 eval-only 都只读本地冻结输入，禁止再次打开来源或调用业务 Agent。
    from copy import deepcopy

    from app.db_models import EvaluationResultRecord, RunResultSnapshotRecord
    from app.evaluation_recovery import recover_evaluation
    from app.result_snapshots import create_result_snapshot
    from app.trajectory.inputs import load_snapshot_input
    monkeypatch.setattr(worker.trajectory_collector, "_get_factory", lambda: pytest.fail("evaluation reopened Reader"))
    async def forbid_invoke(*args, **kwargs):
        pytest.fail("evaluation retry invoked business Agent")
    monkeypatch.setattr("app.worker.RemoteAgentExecutor.invoke_once", forbid_invoke)
    with s40_runtime[0].get_session() as session:
        launch = session.get(ExperimentLaunchRecord, identity["launch_id"])
        launch.status = "COMPLETED"
        snapshot = create_result_snapshot(session, launch)
        original_items = deepcopy(snapshot.items)
        assert original_items[0]["trajectory_input"]["acquisition"] == acquired.model_dump(mode="json")
        item = session.get(ExperimentItemExecutionRecord, identity["item_id"])
        item.evaluation_status, item.evaluation_generation = "evaluating", 1
        item.eval_status, item.quality_conclusion = "failed", "unknown"
        for result in session.scalars(select(EvaluationResultRecord).where(
            EvaluationResultRecord.item_execution_id == item.id)):
            result.status = "failed"
    outcome = recover_evaluation(s40_runtime[0], item_id=identity["item_id"], evaluation_generation=1,
        worker_id="real-frozen-input")
    assert outcome["finalized"] and outcome["quality_conclusion"] == "pass"
    assert len(captured) == 2 and captured[1].trajectory_evidence_ref == acquired.evidence_ref
    with s40_runtime[0].get_session() as session:
        cp = session.scalar(select(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.item_execution_id == identity["item_id"]))
        session.delete(cp)
    with s40_runtime[0].get_session() as session:
        loaded = load_snapshot_input(session, snapshot_id=snapshot.id, dataset_item_id=scope.dataset_item_id)
        assert loaded.trajectory_evidence_ref == acquired.evidence_ref
        assert (loaded.trajectory.scope == scope) if approved else loaded.trajectory is None
        assert session.get(RunResultSnapshotRecord, snapshot.id).items == original_items
    rows = local_langfuse.wait_observations(identity["trace_id"], minimum=2 if profile == "http_only" else 8)
    assert_lineage(rows, identity)
    local_langfuse.record(profile, "automatic-worker-" + mode + ("" if approved else "-unqualified")
        + ("-resume-" + recovery_stage if recovery_stage else ""), identity, rows,
        normalization={"automatic_worker": True, "profile_id": profile_id, "acquisition_state": acquired.state,
            "completeness": acquired.completeness.status if acquired.completeness else None,
            "reason_code": acquired.reason_code, "original_deadline_preserved": True,
            "recovery_stage": recovery_stage,
            "single_attempt_preserved": True, "agent_output_evaluation_preserved": True})


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
        profile_id = "pydantic-ai-v1" if profile == "pydantic_ai" else "native-otel-v1"
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario,
                                 trajectory_source_profile_id=profile_id)
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
        normalization = assert_real_normalized_structure(local_langfuse, s40_runtime, identity, profile_id, scenario=scenario)
        local_langfuse.record(profile, scenario, identity, rows, normalization=normalization)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
@pytest.mark.parametrize("scenario,visibility", [("missing", "MISSING"), ("redacted", "REDACTED"),
                                                 ("uncollected", "UNCOLLECTED")])
def test_arguments_visibility(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile, scenario, visibility):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        profile_id = "pydantic-ai-v1" if profile == "pydantic_ai" else "native-otel-v1"
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario,
                                 trajectory_source_profile_id=profile_id)
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
        normalization = assert_real_normalized_structure(local_langfuse, s40_runtime, identity, profile_id, scenario=scenario)
        local_langfuse.record(profile, scenario, identity, rows, normalization=normalization)


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
def test_parallel_has_no_sibling_dependency(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile):
    with AgentProcess(local_langfuse, tmp_path, profile) as agent:
        profile_id = "pydantic-ai-v1" if profile == "pydantic_ai" else "native-otel-v1"
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, "parallel",
                                 trajectory_source_profile_id=profile_id)
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
        normalization = assert_real_normalized_structure(local_langfuse, s40_runtime, identity, profile_id, scenario="parallel")
        local_langfuse.record(profile, "parallel", identity, rows, normalization=normalization)


@pytest.mark.parametrize("mode", ["late_all", "terminal_first"])
def test_late_visibility_and_terminal_inventory(local_langfuse, s40_runtime, monkeypatch, tmp_path, mode):
    with AgentProcess(local_langfuse, tmp_path, "native_otel", export_mode=mode) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint,
                                 trajectory_source_profile_id="native-otel-v1")
        initial = local_langfuse.wait_observations(identity["trace_id"], minimum=2 if mode == "late_all" else 4,
                                                 require_inventory=False)
        if mode == "late_all":
            assert {row["name"] for row in initial} == {"remote-agent-http", "eval_item_execution:synthetic-case"}
        else:
            terminal = next(row for row in initial if row["name"] == "workflow.terminal")
            expected = set(terminal["metadata"]["attributes.workflow.event_ids"])
            observed = {row["metadata"].get("attributes.workflow.event_id") for row in initial}
            assert expected - observed, "terminal alone must not imply all earlier events arrived"
        from decimal import Decimal

        from app.registry import AgentRegistry
        from app.trajectory.contract import TrajectoryContract
        from app.trajectory.evidence import inventory_complete, load_trajectory_evidence
        from app.trajectory.schema import TrajectoryScope
        from trajectory_contract_fixtures import observed_banking_contract_payload
        version = AgentRegistry(s40_runtime[0]).get_version("test-agent", identity["agent_version"])
        payload = observed_banking_contract_payload()
        payload["limits"] = {"page_size": 2, "deadline_seconds": Decimal(10), "quiet_window_seconds": Decimal("0.05")}
        contract = TrajectoryContract.model_validate(payload).bind_source_version(version)
        scope = TrajectoryScope(launch_id=identity["launch_id"], item_execution_id=identity["item_id"],
            dataset_item_id=identity["dataset_item_id"], attempt_id=identity["attempt_id"],
            dispatch_generation=identity["generation"], trace_id=identity["trace_id"], root_observation_id=identity["root_id"])
        acquired, wire_bytes, first = asyncio.run(acquire_real_trajectory(local_langfuse, s40_runtime[0], scope, contract))
        assert first.state == "PENDING" and first.evidence_ref is None
        assert acquired.state == "READY" and acquired.completeness.status == "OBSERVED_COMPLETE"
        with s40_runtime[0].get_session() as session:
            frozen = load_trajectory_evidence(session, acquired.evidence_ref, scope=scope, contract=contract)
        assert inventory_complete(frozen, contract) and frozen.completeness == acquired.completeness
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=8)
        terminal = next(row for row in rows if row["name"] == "workflow.terminal")
        expected = set(terminal["metadata"]["attributes.workflow.event_ids"])
        observed = {row["metadata"].get("attributes.workflow.event_id") for row in rows}
        assert expected <= observed
        assert terminal["metadata"]["attributes.workflow.final_sequence"] == len(expected)
        assert_lineage(rows, identity)
        local_langfuse.record("native_otel", mode, identity, rows, normalization={
            "profile_id": "native-otel-v1", "acquisition_state": acquired.state,
            "completeness": acquired.completeness.status, "artifact_roundtrip": True,
            "acquisition_source_bytes": wire_bytes, "original_deadline_preserved": True,
            "initial_acquisition_state": first.state})


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


def signed_governed_payload(profile_id, authority):
    from decimal import Decimal

    from trajectory_contract_fixtures import observed_banking_contract_payload

    payload = observed_banking_contract_payload()
    payload["limits"] = {"page_size": 2, "deadline_seconds": Decimal(12), "quiet_window_seconds": Decimal("0.3"),
        "max_steps": 64, "max_pages": 20, "max_source_bytes": 1024 * 1024, "max_artifact_bytes": 64 * 1024}
    payload["redaction_policy"].update(content_policy_version="pii-v1", string_policies={"$.customer_id": "identifier"})
    for profile in payload["source_profile_set"]:
        if profile["profile_id"] != profile_id:
            continue
        profile["human_authority_id"] = authority["authority_id"]
        profile["human_authority"] = authority
        profile["capabilities"]["human_events"] = "COMPLETE"
        profile["fields"]["human_receipt"] = [["metadata", "attributes.approval.receipt"]]
        business_role = profile["structural_roles"][0]
        profile["structural_roles"].extend({"provider_type": "SPAN", "raw_name": name,
            "step_type": "CUSTOM", "safe_name": name, "metadata_equals": dict(business_role["metadata_equals"])}
            for name in ("approval.http", "approval.inventory.http"))
    return payload


def assert_signed_human_artifact(runtime, s40_runtime, identity, agent, *, expected):
    """真实诊断制品消费；不授予正式 Reader 准入或人员审批资格。"""
    from app.registry import AgentRegistry
    from app.trajectory.contract import TrajectoryContract
    from app.trajectory.evidence import load_trajectory_evidence
    from app.trajectory.inputs import EvaluationContext
    from app.trajectory.rules import evaluate_trajectory_rule
    from app.trajectory.schema import TrajectoryScope
    version = AgentRegistry(s40_runtime[0]).get_version("test-agent", identity["agent_version"])
    payload = signed_governed_payload(version.trajectory_source_profile_id, agent.human_authority)
    contract = TrajectoryContract.model_validate(payload).bind_source_version(version)
    scope = TrajectoryScope(launch_id=identity["launch_id"], item_execution_id=identity["item_id"],
        dataset_item_id=identity["dataset_item_id"], attempt_id=identity["attempt_id"],
        dispatch_generation=identity["generation"], trace_id=identity["trace_id"], root_observation_id=identity["root_id"])
    acquired, _, _ = asyncio.run(acquire_real_trajectory(runtime, s40_runtime[0], scope, contract))
    assert acquired.state == "READY" and acquired.evidence_ref is not None, acquired.reason_code
    with s40_runtime[0].get_session() as session:
        trajectory = load_trajectory_evidence(session, acquired.evidence_ref, scope=scope, contract=contract)
    assert trajectory.completeness.capabilities["human_events"] == "COMPLETE", trajectory.completeness.model_dump()
    registry = default_evaluator_registry
    selected = freeze_binding(registry.definition("trajectory.require_human_escalation"),
        registry.version("trajectory.require_human_escalation", "1.0.0"), runner_identity=current_runner_identity().model_dump(),
        params={"rule": "require_human_escalation"})
    result = evaluate_trajectory_rule(EvaluationContext({}, {}, {}, trajectory, acquired.evidence_ref),
        selected.params, binding=selected, manifest_schema_version="1.3",
        authority_policy=contract.source_profile.human_authority)
    assert result.value is expected
    assert result.evidence["reason_code"] == ("HUMAN_ESCALATION_OBSERVED" if expected else "HUMAN_ESCALATION_ABSENT")
    return contract, acquired.evidence_ref


@pytest.mark.parametrize("profile", ["native_otel", "pydantic_ai"])
def test_trusted_human_source(local_langfuse, s40_runtime, monkeypatch, tmp_path, profile):
    with AgentProcess(local_langfuse, tmp_path, profile, approval=True) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, "approved",
            trajectory_source_profile_id={"native_otel": "native-otel-v1", "pydantic_ai": "pydantic-ai-v1"}[profile])
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
        from app.trajectory.human import HumanReceiptError, verify_human_receipt
        receipt = verify_human_receipt(agent.human_authority, metadata["attributes.approval.receipt"],
            trace_id=identity["trace_id"], workflow_scope_id=metadata["attributes.workflow.scope_id"],
            observation_id=human[0]["id"], root_observation_id=identity["root_id"])
        assert receipt.body.event_id == metadata["attributes.workflow.event_id"]
        inventory = next(row for row in rows if row["name"] == "approval.inventory")
        closed = verify_human_receipt(agent.human_authority, inventory["metadata"]["attributes.approval.receipt"],
            trace_id=identity["trace_id"], workflow_scope_id=metadata["attributes.workflow.scope_id"],
            observation_id=inventory["id"], root_observation_id=identity["root_id"])
        assert closed.body.scope_closed is True
        assert closed.body.event_ids == [receipt.body.event_id]
        with pytest.raises(HumanReceiptError, match="HUMAN_RECEIPT_SCOPE_MISMATCH"):
            verify_human_receipt(agent.human_authority, receipt,
                trace_id=identity["trace_id"], workflow_scope_id=metadata["attributes.workflow.scope_id"],
                observation_id=human[0]["id"], root_observation_id="f" * 16)
        assert_lineage(rows, identity)
        assert_signed_human_artifact(local_langfuse, s40_runtime, identity, agent, expected=True)
        local_langfuse.record(profile, "approved", identity, rows)


@pytest.mark.parametrize("scenario", ["serial", "escalate"])
def test_closed_approval_source_without_human_event(local_langfuse, s40_runtime, monkeypatch, tmp_path, scenario):
    with AgentProcess(local_langfuse, tmp_path, "native_otel", approval=True) as agent:
        identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario,
            trajectory_source_profile_id="native-otel-v1")
        rows = local_langfuse.wait_observations(identity["trace_id"], minimum=10 if scenario == "serial" else 12)
        inventories = [row for row in rows if row["name"] == "approval.inventory"]
        assert len(inventories) == 1
        metadata = inventories[0]["metadata"]
        assert metadata["attributes.approval.event_count"] == 0
        assert metadata["attributes.approval.scope_closed"] is True
        assert metadata["resourceAttributes.service.name"] == "s4-approval-system"
        from app.trajectory.human import verify_human_receipt
        closed = verify_human_receipt(agent.human_authority, metadata["attributes.approval.receipt"],
            trace_id=identity["trace_id"], workflow_scope_id=metadata["attributes.workflow.scope_id"],
            observation_id=inventories[0]["id"], root_observation_id=identity["root_id"])
        assert closed.body.event_ids == []
        assert closed.body.scope_closed is True
        assert not any(row.get("metadata", {}).get("attributes.workflow.phase") == "HUMAN" for row in rows)
        if scenario == "escalate":
            assert any(row.get("metadata", {}).get("attributes.gen_ai.tool.name") == "escalate" for row in rows)
        assert_lineage(rows, identity)
        assert_signed_human_artifact(local_langfuse, s40_runtime, identity, agent, expected=False)
        local_langfuse.record("native_otel", "approval-zero-" + scenario, identity, rows)


@pytest.mark.parametrize("profile,profile_id", [("native_otel", "native-otel-v1"), ("pydantic_ai", "pydantic-ai-v1")])
def test_governed_reader_qualification_and_formal_consumers(local_langfuse, s40_runtime, monkeypatch, tmp_path,
                                                          profile, profile_id, caplog):
    """固定公钥下重复校准后才批准受控 fixture 合同，生产仍须独立准入。"""
    import hashlib
    import logging

    from app.db_models import EvaluationResultRecord, ExperimentLaunchRecord
    from app.evaluation_recovery import recover_evaluation
    from app.result_snapshots import create_result_snapshot
    from app.trajectory.factory import QualifiedReaderFactory, ReaderApproval, reader_artifact_digest
    from app.trajectory.worker_acquisition import WorkerTrajectoryCollector
    from test_trajectory_rule_integration import assert_frozen_diagnosis

    api_digest = "sha256:" + hashlib.sha256((local_langfuse.directory / "langfuse-openapi.yaml").read_bytes()).hexdigest()
    artifact_digest = reader_artifact_digest()
    caplog.set_level(logging.DEBUG, logger="app")
    with AgentProcess(local_langfuse, tmp_path, profile, approval=True) as agent:
        samples, refs, traces = [], set(), set()
        for scenario in ("approved", "serial", "escalate"):
            for _ in range(10):
                identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario,
                    trajectory_source_profile_id=profile_id)
                contract, ref = assert_signed_human_artifact(local_langfuse, s40_runtime, identity, agent,
                    expected=scenario == "approved")
                rows = local_langfuse.wait_observations(identity["trace_id"], minimum=10)
                assert_lineage(rows, identity)
                samples.append(contract.digest)
                refs.add(ref.artifact_id)
                traces.add(identity["trace_id"])
                local_langfuse.record(profile, "governed-calibration-" + scenario, identity, rows, normalization={
                    "calibration_sample": True, "qualification_group": profile + ":governed-" + scenario,
                    "trajectory_contract_digest": contract.digest, "reader_artifact_digest": artifact_digest,
                    "api_document_digest": api_digest, "signed_human_expected": scenario == "approved",
                    "human_events_complete": True, "frozen_artifact_roundtrip": True})
        assert len(samples) == len(refs) == len(traces) == 30 and len(set(samples)) == 1
        factory = QualifiedReaderFactory(server_url=local_langfuse.base_url,
            public_key=local_langfuse.env["S4_PUBLIC_KEY"], secret_key=local_langfuse.env["S4_SECRET_KEY"],
            approvals=(ReaderApproval(contract.digest, artifact_digest, api_digest),))
        worker = s40_runtime[4]
        monkeypatch.setattr(worker, "trajectory_collector", WorkerTrajectoryCollector(s40_runtime[0].get_session,
            factory_provider=lambda: factory))
        params = [
            {"rule": "tool_argument_match", "tool_identity": "banking.balance.read", "path": "$.customer_id",
                "operator": "eq", "expected_json": '"synthetic-001"'},
            {"rule": "max_same_tool_repeats", "tool_identity": "banking.balance.read", "identity_paths": ["$.customer_id"],
                "max_repeats": 1},
            {"rule": "required_step_sequence", "steps": [{"type": "TOOL", "phase": "EXECUTED", "tool_identity": name}
                for name in ("banking.customer.lookup", "banking.balance.read")]},
            {"rule": "require_human_escalation"},
        ]
        selections = [{"id": "trajectory." + selected["rule"], "params": selected} for selected in params]
        for scenario in ("approved", "serial"):
            expected_quality = "pass" if scenario == "approved" else "fail"
            identity = invoke_worker(s40_runtime, monkeypatch, local_langfuse, agent.endpoint, scenario,
                trajectory_source_profile_id=profile_id, trajectory_contract=contract, rule_selections=selections,
                expected_quality=expected_quality)
            with s40_runtime[0].get_session() as session:
                launch = session.get(ExperimentLaunchRecord, identity["launch_id"])
                launch.status = "COMPLETED"
                snapshot = create_result_snapshot(session, launch)
                assert snapshot.evidence_state == "COMPLETE"
                assert snapshot.items[0]["required_evidence_status"] == "COMPLETE"
                results = session.scalars(select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == identity["item_id"])).all()
                measured = {result.evaluator_id: (result.status, result.value, result.evidence)
                    for result in results if result.evaluator_id.startswith("trajectory.")}
                assert len(measured) == 4
                for evaluator, (status, value, evidence) in measured.items():
                    assert status == "succeeded"
                    assert value is (scenario == "approved" if evaluator.endswith("require_human_escalation") else True)
                    assert evidence["evidence_ref"] == snapshot.items[0]["trajectory_input"]["acquisition"]["evidence_ref"]
                item = session.get(ExperimentItemExecutionRecord, identity["item_id"])
                item.evaluation_status, item.evaluation_generation = "evaluating", 1
                item.eval_status, item.quality_conclusion = "failed", "unknown"
                for result in results:
                    result.status = "failed"
            original_factory = worker.trajectory_collector._get_factory
            with monkeypatch.context() as recovery_patch:
                recovery_patch.setattr(worker.trajectory_collector, "_get_factory", lambda: pytest.fail("eval-only reopened Reader"))
                async def forbid_invoke(*args, **kwargs):
                    pytest.fail("eval-only called business Agent")
                recovery_patch.setattr("app.worker.RemoteAgentExecutor.invoke_once", forbid_invoke)
                outcome = recover_evaluation(s40_runtime[0], item_id=identity["item_id"], evaluation_generation=1,
                    worker_id="governed-rules")
            assert worker.trajectory_collector._get_factory == original_factory
            assert outcome["finalized"] and outcome["quality_conclusion"] == expected_quality
            with s40_runtime[0].get_session() as session:
                after = session.scalars(select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == identity["item_id"])).all()
                assert {result.evaluator_id: (result.status, result.value, result.evidence) for result in after
                    if result.evaluator_id.startswith("trajectory.")} == measured
            diagnosis = assert_frozen_diagnosis(s40_runtime, monkeypatch, snapshot, identity["dataset_item_id"])
            assert len(diagnosis["rule_results"]) == 4
            assert {result["conclusion"] for result in diagnosis["rule_results"]} == (
                {"pass"} if scenario == "approved" else {"pass", "fail"})
            assert_governed_snapshot_projection_revisions(local_langfuse, s40_runtime, identity,
                tmp_path / f"{profile_id}-{scenario}-snapshot-projection.json")

        for privacy_operation in ("privacy", "privacy_ssn"):
            assert_governed_privacy_consumers(local_langfuse, s40_runtime, monkeypatch, agent, profile_id, contract, caplog,
                                             operation=privacy_operation)


def assert_governed_snapshot_projection_revisions(runtime, s40_runtime, identity, receipt_path):
    """S45-15：真实 SDK 验证 eval-only 两个 Snapshot 同 generation 且旧 Score 保留。"""
    import json
    import time

    from app.db_models import LangfuseSyncTaskRecord, RunResultSnapshotRecord
    from app.langfuse_sync import LangfuseOutboxSyncer
    from sqlalchemy import select

    db = s40_runtime[0]
    with db.get_session() as session:
        snapshots = session.scalars(select(RunResultSnapshotRecord).where(
            RunResultSnapshotRecord.launch_id == identity["launch_id"],
            RunResultSnapshotRecord.evidence_state == "COMPLETE").order_by(RunResultSnapshotRecord.revision)).all()
        assert len(snapshots) == 2
        snapshot_ids = {snapshot.id for snapshot in snapshots}
        assert len({snapshot.items[0]["dispatch_generation"] for snapshot in snapshots}) == 1
        before = {snapshot.id: json.dumps({"manifest": snapshot.manifest, "items": snapshot.items,
            "summary": snapshot.summary}, sort_keys=True) for snapshot in snapshots}
        tasks = session.scalars(select(LangfuseSyncTaskRecord).where(
            LangfuseSyncTaskRecord.result_snapshot_id.in_(snapshot_ids))).all()
        assert len(tasks) == 2 and {task.result_snapshot_id for task in tasks} == snapshot_ids
        task_ids = {task.id for task in tasks}
    syncer = LangfuseOutboxSyncer(db, runtime.sdk, lease_duration_seconds=180)
    claims = syncer.claim_tasks(batch_size=1000)
    assert task_ids <= {task_id for task_id, _, _ in claims}
    expected = {}
    for task_id, _, payload in claims:
        if task_id in task_ids:
            scores, reason = syncer._trajectory_projection(task_id, payload)
            assert reason is None
            assert len([score for score in scores if score.evaluator_id.startswith("trajectory.")]) == 4
            for score in scores:
                assert score.score_id not in expected
                expected[score.score_id] = score.evidence["snapshot_id"]
    for task_id, token, payload in claims:
        success = syncer.process_single_task(task_id, token, payload)
        if task_id in task_ids:
            assert success
    deadline = time.monotonic() + 15
    while True:
        response = runtime.http.get("/api/public/v3/scores", params={"traceId": identity["trace_id"], "fields": "details,subject"}, timeout=5)
        assert response.status_code == 200
        actual = {score["id"]: score["metadata"]["snapshot_id"] for score in response.json()["data"]}
        if actual == expected:
            break
        assert time.monotonic() < deadline, "two Snapshot projections did not remain visible within 15 seconds"
        time.sleep(0.1)
    with db.get_session() as session:
        assert all(session.get(LangfuseSyncTaskRecord, task_id).status == "SYNCED" for task_id in task_ids)
        for snapshot_id, original in before.items():
            stored = session.get(RunResultSnapshotRecord, snapshot_id)
            assert json.dumps({"manifest": stored.manifest, "items": stored.items, "summary": stored.summary}, sort_keys=True) == original
    receipt_path.write_text(json.dumps({"snapshot_ids": sorted(snapshot_ids), "score_snapshot_ids": actual,
        "same_dispatch_generation": True, "production_outbox_synced": True, "snapshot_unchanged": True}, indent=2))


def assert_governed_privacy_consumers(runtime, s40_runtime, monkeypatch, agent, profile_id, contract, caplog, *, operation="privacy"):
    """真实来源哨兵 → 正式测量 → Snapshot/API/Outbox/实际 Score 的最小数据链路。"""
    from app.db_models import (
        ExperimentLaunchRecord,
        LangfuseSyncTaskRecord,
        RunResultSnapshotRecord,
        TrajectoryEvidenceArtifactRecord,
    )
    from app.langfuse_sync import LangfuseOutboxSyncer
    from app.result_snapshots import create_result_snapshot
    from app.trajectory.codec import canonical_dumps
    from test_trajectory_rule_integration import assert_frozen_diagnosis

    sentinels = ("123-45-6789" if operation == "privacy_ssn" else "4111111111111111", "11010519491231002X", "synthetic-privacy-sentinel")
    selections = [{"id": "trajectory.require_tool", "params": {"rule": "require_tool", "tool_identity": "banking.customer.lookup"}},
        {"id": "trajectory.tool_argument_match", "params": {"rule": "tool_argument_match", "tool_identity": "banking.balance.read",
            "path": "$.customer_id", "operator": "eq", "expected_json": '"synthetic-001"'}}]
    identity = invoke_worker(s40_runtime, monkeypatch, runtime, agent.endpoint, operation,
        trajectory_source_profile_id=profile_id, trajectory_contract=contract, rule_selections=selections,
        expected_quality="unknown", expected_eval_status="failed")
    rows = runtime.wait_observations(identity["trace_id"], minimum=10)
    raw = json.dumps(rows, sort_keys=True)  # 仅检查真实 HTTP DTO 的哨兵存在性，不用 float DTO 作为冻结证据。
    assert all(sentinel in raw for sentinel in sentinels), "privacy scenario did not actually collect the raw sentinels"
    with s40_runtime[0].get_session() as session:
        launch = session.get(ExperimentLaunchRecord, identity["launch_id"])
        launch.status = "COMPLETED"
        snapshot = create_result_snapshot(session, launch)
        assert snapshot.evidence_state == "DIAGNOSTIC"
        assert snapshot.items[0]["required_evidence_status"] == "INCOMPLETE"
        # DatabaseManager 使用 autoflush=False；事务提交前的查询须显式刷入待写 Outbox。
        session.flush()
        snapshots = session.scalars(select(RunResultSnapshotRecord).where(
            RunResultSnapshotRecord.launch_id == identity["launch_id"])).all()
        artifacts = session.scalars(select(TrajectoryEvidenceArtifactRecord).where(
            TrajectoryEvidenceArtifactRecord.item_execution_id == identity["item_id"])).all()
        tasks = session.scalars(select(LangfuseSyncTaskRecord).where(
            LangfuseSyncTaskRecord.item_id == identity["item_id"])).all()
        assert artifacts and tasks
        serialized = [json.dumps({"manifest": row.manifest, "items": row.items, "summary": row.summary}) for row in snapshots]
        serialized += [row.payload_json for row in artifacts]
        serialized += [json.dumps(row.scores_payload) for row in tasks]
        assert all(sentinel not in value for sentinel in sentinels for value in serialized)
        task = next(row for row in tasks if row.task_type == "TRAJECTORY_ITEM_SCORES" and row.result_snapshot_id == snapshot.id)
        task_id = task.id
    syncer = LangfuseOutboxSyncer(s40_runtime[0], runtime.sdk, lease_duration_seconds=180)
    claimed = syncer.claim_tasks(batch_size=1000)
    target = next((entry for entry in claimed if entry[0] == task_id), None)
    assert target is not None, "fixed Snapshot outbox task was not claimable"
    projected, projection_reason = syncer._trajectory_projection(task_id, target[2])
    assert projection_reason is None
    assert {score.evaluator_id for score in projected} == {"intent_match", "trajectory.require_tool"}
    boolean = next(score for score in projected if score.evaluator_id == "trajectory.require_tool")
    assert boolean.evidence["original_value"] is True
    for claimed_id, claim_token, payload in claimed:
        succeeded = syncer.process_single_task(claimed_id, claim_token, payload)
        if claimed_id == task_id:
            assert succeeded
    with s40_runtime[0].get_session() as session:
        assert session.get(LangfuseSyncTaskRecord, task_id).status == "SYNCED"
    diagnosis = assert_frozen_diagnosis(s40_runtime, monkeypatch, snapshot, identity["dataset_item_id"])
    assert all(sentinel not in canonical_dumps(diagnosis) for sentinel in sentinels)
    balance = [step for step in diagnosis["trajectory"]["steps"] if step["tool_identity"] == "banking.balance.read"]
    assert balance and all(step["argument_visibility"]["$.customer_id"] == "REDACTED" for step in balance)
    argument = next(result for result in diagnosis["rule_results"] if result["evaluator_id"] == "trajectory.tool_argument_match")
    assert argument["conclusion"] == "unknown"
    # 生产 Outbox 已经上传；此处只核验固定 Snapshot 的真实 Score 出口，不再旁路写入。
    for score in projected:
        assert all(sentinel not in json.dumps(score.evidence) for sentinel in sentinels)
        assert score.score_id.startswith("score:trajectory-v1:")
        assert score.evidence["snapshot_id"] == snapshot.id
    import time
    expected_ids = {score.score_id for score in projected}
    deadline = time.monotonic() + 15
    while True:
        response = runtime.http.get("/api/public/v3/scores", params={"traceId": identity["trace_id"], "fields": "details,subject"}, timeout=5)
        assert response.status_code == 200
        assert all(sentinel not in response.text for sentinel in sentinels)
        scores = response.json()["data"]
        if {score["id"] for score in scores} == expected_ids:
            break
        assert time.monotonic() < deadline, "projected scores did not become visible within 15 seconds"
        time.sleep(0.1)
    measured_boolean = next(score for score in scores if score["id"] == boolean.score_id)
    assert measured_boolean["metadata"]["original_value"] is True
    assert all(sentinel not in repr(record.__dict__) for sentinel in sentinels for record in caplog.records
        if record.name.startswith("app."))
    runtime.record(agent.profile, "governed-" + operation.replace("_", "-"), identity, rows, normalization={
        "trajectory_contract_digest": contract.digest, "raw_sentinels_observed": True, "required_no_result_unknown": True,
        "artifact_snapshot_api_outbox_score_minimized": True, "argus_log_records_minimized": True,
        "snapshot_outbox_claim_lease_projection_synced": True, "snapshot_id": snapshot.id,
        "actual_score_ids": sorted(score["id"] for score in scores)})
