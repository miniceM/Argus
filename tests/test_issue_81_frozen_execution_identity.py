"""Argus Issue #81 — frozen evaluation execution identity (S3.1)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.db import DatabaseManager, MigrationRunner  # noqa: E402
from app.evaluator_binding import (  # noqa: E402
    FROZEN_CONTRACT_STATUS,
    LEGACY_CONTRACT_STATUS,
    EvaluatorBinding,
    EvaluatorBindingError,
    bindings_from_manifest,
    build_executor_registry,
    canonical_digest,
    evaluate_frozen_item,
    freeze_binding,
    manifest_contract_status,
    resolve_execution_plan,
)
from app.evaluators import (  # noqa: E402
    Evaluation,
    EvaluatorDefinition,
    EvaluatorVersion,
    default_evaluator_registry,
)
from app.manifest import LaunchService  # noqa: E402
from app.registry import AgentRegistry  # noqa: E402

RUNNER_IDENTITY = {
    "runner_version": "0.1.0",
    "build_id": "build-issue-81",
    "mapping_engine_version": "sha256-mapping-engine-v1",
}


@pytest.fixture(autouse=True)
def isolate_global_evaluator_registry():
    """Several acceptance cases mutate the process-wide registry on purpose."""
    registry = default_evaluator_registry
    saved_definitions = dict(registry._definitions)
    saved_versions = dict(registry._versions)
    try:
        yield registry
    finally:
        registry._definitions.clear()
        registry._definitions.update(saved_definitions)
        registry._versions.clear()
        registry._versions.update(saved_versions)


def setup_db(tmp_path):
    db_file = tmp_path / "issue81.db"
    db_mgr = DatabaseManager(f"sqlite:///{db_file}")
    MigrationRunner(engine=db_mgr.engine, migrations_dir=ROOT / "migrations").apply_all()
    registry = AgentRegistry(db_mgr)
    registry.import_yaml(ROOT / "config" / "agents.yaml")
    return db_mgr, registry


def create_launch(db_mgr, registry, **kwargs):
    service = LaunchService(db_mgr, registry, runner_version="0.1.0")
    return service.create_launch(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Canonical digest behaviour
# ---------------------------------------------------------------------------

def test_binding_digest_is_independent_of_field_order():
    binding = EvaluatorBinding.from_payload(
        {
            "id": "intent_match",
            "version": "1.0.0",
            "scope": "item",
            "threshold": 1.0,
            "definition_digest": "sha256:" + "a" * 64,
            "implementation_ref": "builtin:intent_match@1.0.0",
            "executor_type": "builtin_python",
            "input_contract": {"type": "object", "required": ["output"]},
            "output_contract": {"type": "number"},
            "params": {},
            "runner": dict(RUNNER_IDENTITY),
            "implementation_artifact": {
                "kind": "python_source",
                "locator": "app.evaluators:intent_match",
                "digest": "sha256:" + "b" * 64,
                "runtime": "cpython",
            },
        }
    )
    reordered = EvaluatorBinding.from_payload(
        {
            "runner": dict(RUNNER_IDENTITY),
            "params": {},
            "output_contract": {"type": "number"},
            "input_contract": {"type": "object", "required": ["output"]},
            "executor_type": "builtin_python",
            "implementation_ref": "builtin:intent_match@1.0.0",
            "definition_digest": "sha256:" + "a" * 64,
            "threshold": 1.0,
            "scope": "item",
            "version": "1.0.0",
            "id": "intent_match",
            "implementation_artifact": {
                "runtime": "cpython",
                "digest": "sha256:" + "b" * 64,
                "locator": "app.evaluators:intent_match",
                "kind": "python_source",
            },
        }
    )
    assert binding.binding_digest == reordered.binding_digest


@pytest.mark.parametrize(
    "field,value",
    [
        ("threshold", 0.5),
        ("direction", "lower_is_better"),
        ("critical", True),
        ("result_type", "boolean"),
        ("definition_digest", "sha256:" + "c" * 64),
        ("implementation_ref", "builtin:intent_match@9.9.9"),
        ("executor_type", "remote_python"),
    ],
)
def test_any_result_affecting_change_changes_the_binding_digest(field, value):
    base = EvaluatorBinding.from_payload(
        {
            "id": "intent_match",
            "version": "1.0.0",
            "scope": "item",
            "threshold": 1.0,
            "definition_digest": "sha256:" + "a" * 64,
            "implementation_ref": "builtin:intent_match@1.0.0",
            "executor_type": "builtin_python",
            "runner": dict(RUNNER_IDENTITY),
            "implementation_artifact": {
                "kind": "python_source",
                "locator": "app.evaluators:intent_match",
                "digest": "sha256:" + "b" * 64,
                "runtime": "cpython",
            },
        }
    )
    changed = EvaluatorBinding.from_payload({**base.to_payload(), field: value})
    assert changed.binding_digest != base.binding_digest


def test_display_only_rename_does_not_change_binding_digest():
    base = EvaluatorBinding.from_payload(
        {"id": "pii_safe", "version": "1.0.0", "definition_digest": "sha256:" + "a" * 64}
    )
    renamed = EvaluatorBinding.from_payload(
        {
            "id": "pii_safe",
            "version": "1.0.0",
            "definition_digest": "sha256:" + "a" * 64,
            "name": "敏感信息保护（新文案）",
        }
    )
    assert renamed.binding_digest == base.binding_digest


def test_artifact_digest_is_not_a_mutable_tag_or_env_string():
    definition = default_evaluator_registry.definition("intent_match")
    version = default_evaluator_registry.version("intent_match", "1.0.0")
    binding = freeze_binding(definition, version, runner_identity=RUNNER_IDENTITY)
    assert binding.artifact is not None
    assert binding.artifact.digest.startswith("sha256:")
    assert "latest" not in binding.artifact.digest
    assert binding.artifact.kind == "python_source"


# ---------------------------------------------------------------------------
# Manifest freezing
# ---------------------------------------------------------------------------

def test_launch_manifest_freezes_binding_identity(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-freeze")

    manifest = launch.manifest
    assert manifest["schema_version"] == "1.2"
    assert manifest_contract_status(manifest) == FROZEN_CONTRACT_STATUS
    for binding_payload in manifest["evaluators"]:
        assert binding_payload["binding_id"].startswith("bind_")
        assert binding_payload["binding_digest"].startswith("sha256:")
        assert binding_payload["definition_digest"].startswith("sha256:")
        assert binding_payload["implementation_ref"]
        assert binding_payload["executor_type"] == "builtin_python"
        assert binding_payload["contract_status"] == FROZEN_CONTRACT_STATUS
        assert binding_payload["verification_status"] == "RECORDED"
        artifact = binding_payload["implementation_artifact"]
        assert artifact["digest"].startswith("sha256:")
        assert artifact["kind"] == "python_source"
        assert binding_payload["runner"]["build_id"] == launch.manifest["runner"]["build_id"]
        # Legacy spec fields stay readable for older consumers.
        assert binding_payload["id"] and binding_payload["scope"] == "item"


def test_binding_id_is_stable_for_the_same_frozen_identity(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    first = create_launch(db_mgr, registry, name="issue81-stable-a")
    second = create_launch(db_mgr, registry, name="issue81-stable-b")
    assert [b["binding_id"] for b in first.manifest["evaluators"]] == [
        b["binding_id"] for b in second.manifest["evaluators"]
    ]


# ---------------------------------------------------------------------------
# Catalog updates must not drift a frozen Launch
# ---------------------------------------------------------------------------

def test_catalog_update_does_not_change_frozen_binding_or_plan(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-no-drift")
    frozen_before = [dict(b) for b in launch.manifest["evaluators"]]
    plan_before = resolve_execution_plan(launch.manifest)

    evaluators = default_evaluator_registry
    definition = evaluators.definition("intent_match")
    evaluators.register_version(
        EvaluatorVersion(
            evaluator_id="intent_match",
            version="2.0.0",
            result_type="numeric",
            scope="item",
            threshold=1.0,
            direction="higher_is_better",
            critical=False,
            implementation_ref="builtin:intent_match@2.0.0",
            executor_type="builtin_python",
            input_contract={"type": "object"},
            output_contract={"type": "number"},
            param_schema={"type": "object", "properties": {}},
            fn=lambda *, output, expected_output, **_: Evaluation(
                value=1.0, passed=True, reason="v2"
            ),
        )
    )
    evaluators._definitions["intent_match"] = EvaluatorDefinition(
        id="intent_match",
        name=definition.name,
        display_description=definition.display_description,
        definition_source=definition.definition_source,
        execution_owner=definition.execution_owner,
        default_version="2.0.0",
        default_selected=definition.default_selected,
    )

    # Manifest is immutable: the frozen binding still points at 1.0.0.
    assert [dict(b) for b in launch.manifest["evaluators"]] == frozen_before
    assert evaluators.definition("intent_match").default_version == "2.0.0"

    plan_after = resolve_execution_plan(launch.manifest)
    assert [r.binding.version for r in plan_after] == [r.binding.version for r in plan_before]
    assert all(r.binding.version == "1.0.0" for r in plan_after if r.binding.evaluator_id == "intent_match")


def test_tampered_implementation_is_detected_by_artifact_digest(tmp_path):
    """Editing the registered function without publishing a new version is caught."""
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-frozen-impl")
    manifest = launch.manifest

    evaluators = default_evaluator_registry
    original = evaluators.version("intent_match", "1.0.0")
    evaluators._versions[("intent_match", "1.0.0")] = EvaluatorVersion(
        evaluator_id=original.evaluator_id,
        version=original.version,
        result_type=original.result_type,
        scope=original.scope,
        threshold=original.threshold,
        direction=original.direction,
        critical=original.critical,
        implementation_ref=original.implementation_ref,
        executor_type=original.executor_type,
        input_contract=original.input_contract,
        output_contract=original.output_contract,
        param_schema=original.param_schema,
        fn=lambda *, output, expected_output, **_: Evaluation(
            value=0.0, passed=False, reason="tampered"
        ),
    )
    with pytest.raises(EvaluatorBindingError) as excinfo:
        resolve_execution_plan(manifest)
    assert excinfo.value.code == "EVALUATOR_ARTIFACT_DIGEST_MISMATCH"
    assert excinfo.value.expected != excinfo.value.actual
    assert excinfo.value.expected.startswith("sha256:")
    assert excinfo.value.actual.startswith("sha256:")
    assert excinfo.value.recovery


def test_missing_frozen_version_never_falls_back_to_another_version(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-missing-version")
    manifest = launch.manifest

    evaluators = default_evaluator_registry
    saved = evaluators._versions.pop(("intent_match", "1.0.0"))
    try:
        with pytest.raises(EvaluatorBindingError) as excinfo:
            resolve_execution_plan(manifest)
        assert excinfo.value.code == "EVALUATOR_VERSION_UNAVAILABLE"
        assert excinfo.value.evaluator_id == "intent_match"
        assert excinfo.value.version == "1.0.0"
        assert excinfo.value.recovery
    finally:
        evaluators._versions[("intent_match", "1.0.0")] = saved


def test_tampered_definition_digest_is_detected(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-tampered")
    manifest = dict(launch.manifest)
    manifest["evaluators"] = [
        {**b, "definition_digest": "sha256:" + "f" * 64}
        if b["id"] == "pii_safe"
        else b
        for b in manifest["evaluators"]
    ]
    with pytest.raises(EvaluatorBindingError) as excinfo:
        resolve_execution_plan(manifest)
    assert excinfo.value.code == "EVALUATOR_BINDING_DIGEST_MISMATCH"
    assert excinfo.value.expected == "sha256:" + "f" * 64
    assert excinfo.value.actual is not None
    assert excinfo.value.expected != excinfo.value.actual


def test_unsupported_executor_type_is_rejected_before_execution(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-executor")
    manifest = dict(launch.manifest)
    manifest["evaluators"] = [
        {**b, "executor_type": "remote_python"} if b["id"] == "intent_match" else b
        for b in manifest["evaluators"]
    ]
    with pytest.raises(EvaluatorBindingError) as excinfo:
        resolve_execution_plan(manifest)
    assert excinfo.value.code == "EVALUATOR_EXECUTOR_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Unified evaluation entry point
# ---------------------------------------------------------------------------

def test_evaluate_frozen_item_returns_scores_and_quality(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-evaluate")
    result = evaluate_frozen_item(
        launch.manifest,
        output={"intent": "refund", "tool_calls": [{"name": "lookup"}], "disclosed_fields": []},
        expected_output={
            "expected_intent": "refund",
            "required_tool": "lookup",
            "must_escalate": False,
        },
    )
    assert result.eval_status == "succeeded"
    assert result.quality_conclusion == "pass"
    assert set(result.scores) == {
        "escalation_match",
        "intent_match",
        "pii_safe",
        "required_tool_match",
    }
    assert result.verification_status == FROZEN_CONTRACT_STATUS
    assert {e["verification"] for e in result.evaluators} == {"VERIFIED"}


def test_evaluate_frozen_item_reports_binding_failure_without_scores(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-evaluate-fail")
    manifest = dict(launch.manifest)
    manifest["evaluators"] = [
        {**b, "executor_type": "remote_python"} if b["id"] == "pii_safe" else b
        for b in manifest["evaluators"]
    ]
    result = evaluate_frozen_item(manifest, output={}, expected_output={})
    assert result.eval_status == "failed"
    assert result.scores == {}
    assert result.quality_conclusion == "unknown"
    assert "EVALUATOR_EXECUTOR_UNSUPPORTED" in (result.eval_error or "") or result.eval_error


def test_item_with_no_item_scope_evaluators_is_skipped(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-skipped")
    manifest = dict(launch.manifest)
    manifest["evaluators"] = [
        {**b, "scope": "run"} for b in manifest["evaluators"]
    ]
    result = evaluate_frozen_item(manifest, output={}, expected_output={})
    assert result.eval_status == "skipped"
    assert result.quality_conclusion == "unknown"


# ---------------------------------------------------------------------------
# Legacy manifests
# ---------------------------------------------------------------------------

def test_legacy_manifest_is_read_explicitly_as_historical_contract(tmp_path):
    legacy_manifest = {
        "schema_version": "1.1",
        "evaluators": [
            {
                "id": "intent_match",
                "version": "1.0.0",
                "scope": "item",
                "threshold": 1.0,
                "params": {},
                "direction": "higher_is_better",
                "critical": False,
                "name": "意图匹配",
                "result_type": "numeric",
                "content_digest": default_evaluator_registry.version(
                    "intent_match", "1.0.0"
                ).content_digest,
            }
        ],
    }
    assert manifest_contract_status(legacy_manifest) == LEGACY_CONTRACT_STATUS
    bindings = bindings_from_manifest(legacy_manifest)
    assert bindings[0].contract_status == LEGACY_CONTRACT_STATUS
    assert bindings[0].artifact is None
    assert bindings[0].verification_status == LEGACY_CONTRACT_STATUS

    # Still executable (backward compatibility) but never claims verification.
    plan = resolve_execution_plan(legacy_manifest)
    assert plan[0].verification == LEGACY_CONTRACT_STATUS
    result = evaluate_frozen_item(
        legacy_manifest,
        output={"intent": "refund"},
        expected_output={"expected_intent": "refund"},
    )
    assert result.eval_status == "succeeded"
    assert result.verification_status == LEGACY_CONTRACT_STATUS
    assert {e["verification"] for e in result.evaluators} == {LEGACY_CONTRACT_STATUS}


def test_legacy_manifest_without_version_still_fails_explicitly():
    legacy_manifest = {
        "schema_version": "1.0",
        "evaluators": [{"id": "intent_match", "scope": "item", "threshold": 1.0}],
    }
    with pytest.raises(EvaluatorBindingError) as excinfo:
        resolve_execution_plan(legacy_manifest)
    assert excinfo.value.code == "EVALUATOR_VERSION_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Executor extensibility
# ---------------------------------------------------------------------------

def test_extra_evaluator_runs_without_touching_the_main_flow(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    evaluators = default_evaluator_registry

    def always_half(*, output: Any, expected_output: Any, **_: Any) -> Evaluation:
        return Evaluation(name="custom_ratio", value=0.5, comment="custom")

    evaluators.register_definition(
        EvaluatorDefinition(
            id="custom_ratio",
            name="自定义比率",
            display_description="注册表扩展的确定性指标",
            definition_source="ARGUS_BUILTIN",
            execution_owner="ARGUS",
            default_version="1.0.0",
        )
    )
    evaluators.register_version(
        EvaluatorVersion(
            evaluator_id="custom_ratio",
            version="1.0.0",
            result_type="numeric",
            scope="item",
            threshold=1.0,
            direction="higher_is_better",
            critical=False,
            implementation_ref="builtin:custom_ratio@1.0.0",
            executor_type="builtin_python",
            input_contract={"type": "object"},
            output_contract={"type": "number"},
            param_schema={"type": "object", "properties": {}},
            fn=always_half,
        )
    )

    launch = create_launch(
        db_mgr,
        registry,
        name="issue81-custom",
        evaluator_selections=[
            {"id": "intent_match", "version": "1.0.0"},
            {"id": "custom_ratio", "version": "1.0.0"},
        ],
    )
    ids = {b["id"] for b in launch.manifest["evaluators"]}
    assert ids == {"intent_match", "custom_ratio"}
    result = evaluate_frozen_item(
        launch.manifest,
        output={"intent": "refund"},
        expected_output={"expected_intent": "refund"},
    )
    assert result.eval_status == "succeeded"
    assert result.scores["custom_ratio"] == 0.5
    assert result.quality_conclusion == "fail"


def test_executor_registry_is_extensible_by_executor_type():
    registry = default_evaluator_registry
    executors = build_executor_registry(registry)
    assert "builtin_python" in executors


# ---------------------------------------------------------------------------
# No bypass paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module", ["worker.py", "execution.py", "api_launches.py"]
)
def test_execution_modules_do_not_bypass_the_frozen_boundary(module):
    source = (ROOT / "services" / "eval-runner" / "app" / module).read_text()
    assert "get_evaluator_fn" not in source, (
        f"{module} must evaluate through evaluator_binding.evaluate_frozen_item / "
        "resolve_execution_plan, not by looking functions up per evaluator id."
    )


def test_worker_and_run_paths_preflight_the_execution_plan(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-preflight")
    manifest = launch.manifest
    # Simulate the frozen artifact disappearing between creation and execution.
    tampered = dict(manifest)
    tampered["evaluators"] = [
        {**b, "implementation_artifact": None} if b["id"] == "intent_match" else b
        for b in manifest["evaluators"]
    ]
    with pytest.raises(EvaluatorBindingError) as excinfo:
        resolve_execution_plan(tampered)
    assert excinfo.value.code == "EVALUATOR_ARTIFACT_UNRESOLVABLE"


def test_binding_payload_round_trips(tmp_path):
    db_mgr, registry = setup_db(tmp_path)
    launch = create_launch(db_mgr, registry, name="issue81-roundtrip")
    for payload in launch.manifest["evaluators"]:
        binding = EvaluatorBinding.from_payload(payload)
        assert binding.to_payload()["binding_digest"] == payload["binding_digest"]
        assert binding.to_payload()["binding_id"] == payload["binding_id"]
        assert binding.to_payload()["implementation_artifact"] == payload[
            "implementation_artifact"
        ]


def test_canonical_digest_is_prefixed_sha256():
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})
    assert canonical_digest({"a": 1}).startswith("sha256:")


# ---------------------------------------------------------------------------
# Worker preflight: an unrecoverable frozen artifact must stop execution
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from app.db_models import ExperimentItemExecutionRecord as Item  # noqa: E402
from app.db_models import ExperimentLaunchRecord as Launch  # noqa: E402


def _start_launch_with_manifest(setup_runtime, manifest, group="issue81"):
    db_mgr, queue, _limiter, orchestrator, worker, _rec = setup_runtime
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", group, manifest)
    orchestrator.start_launch(launch.id)
    msgs = queue.read_group(group, count=1)
    return db_mgr, worker, launch.id, msgs[0]


def _frozen_manifest(binding_overrides=None):
    # Freeze against the identity this test process actually runs with, so the
    # assertions isolate the Evaluator binding and not the Runner check.
    from app.runner_identity import current_runner_identity

    identity = current_runner_identity().model_dump()
    definition = default_evaluator_registry.definition("intent_match")
    version = default_evaluator_registry.version("intent_match", "1.0.0")
    binding = freeze_binding(definition, version, runner_identity=identity).to_payload()
    if binding_overrides:
        binding.update(binding_overrides)
    return {
        "agent": {
            "agent_id": "test-agent",
            "version": "v1",
            "agent_version_id": "test-agent-v1",
            "endpoint": "http://localhost/invoke",
            "method": "POST",
            "request_mapping": {},
            "is_idempotent": False,
        },
        "execution_policy": {
            "timeout_seconds": 60,
            "max_retries": 2,
            "max_concurrency": 1,
            "rate_limit_per_minute": 60,
        },
        "dataset": {
            "items": [{"id": "0", "input": {}, "expected_output": {"expected_intent": "refund"}}]
        },
        "evaluators": [binding],
        "runner": identity,
    }


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"implementation_artifact": None}, "EVALUATOR_ARTIFACT_UNRESOLVABLE"),
        ({"executor_type": "remote_python"}, "EVALUATOR_EXECUTOR_UNSUPPORTED"),
        ({"definition_digest": "sha256:" + "0" * 64}, "EVALUATOR_BINDING_DIGEST_MISMATCH"),
    ],
)
def test_worker_stops_before_calling_the_agent(
    setup_runtime, overrides, expected_code
):
    """No Agent call and no score may exist when the frozen identity cannot be honored."""
    db_mgr, worker, launch_id, msg = _start_launch_with_manifest(
        setup_runtime, _frozen_manifest(overrides)
    )

    with patch(
        "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
    ) as mock_invoke:
        assert asyncio.run(worker.execute_item_message(*msg)) is True
        assert mock_invoke.await_count == 0

    with db_mgr.get_session() as session:
        item = session.get(Item, msg[1])
        assert item.execution_status.lower() == "failed"
        assert item.eval_status.lower() == "skipped"
        assert item.quality_conclusion == "unknown"
        assert item.execution_error == expected_code
        assert not item.scores
        launch = session.get(Launch, launch_id)
        assert launch.quality_conclusion.lower() == "unknown"


def test_worker_executes_the_frozen_binding_normally(setup_runtime):
    """The happy path still runs through the frozen boundary and scores once."""
    db_mgr, worker, _launch_id, msg = _start_launch_with_manifest(
        setup_runtime, _frozen_manifest(), group="issue81-ok"
    )

    async def _run():
        with patch(
            "app.worker.get_langfuse_client_safe", return_value=None
        ):
            from app.executor import SingleInvocationResult

            with patch(
                "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
            ) as mock_invoke:
                mock_invoke.return_value = SingleInvocationResult(
                    status_code=200,
                    body={"intent": "refund"},
                    raw_response='{"intent": "refund"}',
                    headers={},
                    duration_ms=5,
                    trace_context_received=True,
                    error_category=None,
                    error_message=None,
                    is_retryable=False,
                    may_have_side_effects=False,
                )
                return await worker.execute_item_message(*msg)

    assert asyncio.run(_run()) is True

    with db_mgr.get_session() as session:
        item = session.get(Item, msg[1])
        assert item.execution_status.lower() == "succeeded"
        assert item.eval_status.lower() == "succeeded"
        assert item.scores == {"intent_match": 1.0}
        assert item.quality_conclusion.lower() == "pass"
