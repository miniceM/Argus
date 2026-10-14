"""Judge 同步执行器只消费受预算异步阶段签发的固定结果。"""

import asyncio
import hashlib
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from app.evaluator_binding import evaluate_frozen_item, freeze_binding, resolve_execution_plan
from app.evaluators import EvaluatorRegistry
from app.runner_identity import current_runner_identity
from app.trajectory.codec import canonical_dumps
from test_trajectory_judge_definition import definition


def registered(**changes):
    from app.trajectory.judge_execution import judge_implementation_artifact, register_judge_version
    frozen = definition(implementation_artifact_digest=judge_implementation_artifact().digest, **changes)
    registry = EvaluatorRegistry(include_builtins=False)
    version = register_judge_version(registry, evaluator_id="judge.completion", definition=frozen)
    binding = freeze_binding(registry.definition(version.evaluator_id), version,
        runner_identity=current_runner_identity().model_dump(),
        params={"judge_definition_json": canonical_dumps(frozen)})
    return registry, frozen, binding


@pytest.fixture
def runtime(tmp_path):
    from app.db_models import AgentVersionRecord, ExecutionCheckpointRecord, ExperimentLaunchRecord
    from app.evaluator_binding import canonical_digest, manifest_measurement_digest
    from app.execution_checkpoint import binding_provenance_from_manifest
    from app.trajectory.inputs import context_for_item
    from app.trajectory.judge_input import adjudicate_judge_criteria, prepare_judge_request
    from app.trajectory.manifest import bind_trajectory_manifest, freeze_trajectory_contract
    from test_trajectory_assembler import configured
    from test_trajectory_inputs import context, ready

    registry, frozen, binding = registered(mode="release")
    setup = context.__wrapped__(SimpleNamespace(param="sqlite"), tmp_path)
    db, scope, contract, started_at = next(setup)
    try:
        contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
            "content_policy_version": "pii-v1", "string_policies": {"$.customer_id": "identifier", "$.intent": "identifier"}})
        with db.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, scope.launch_id)
            cp = session.get(ExecutionCheckpointRecord, "checkpoint")
            manifest = deepcopy(launch.manifest)
            manifest["evaluators"] = [binding.to_payload()]
            cp.expected_output = {"intent": "balance"}
            manifest["dataset"]["items"][0]["expected_output"] = cp.expected_output
            manifest["trajectory"] = bind_trajectory_manifest(freeze_trajectory_contract(contract),
                session.get(AgentVersionRecord, launch.agent_version_id))
            manifest["measurement_digest"] = manifest_measurement_digest(manifest)
            manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
            launch.manifest = manifest
            cp.manifest_digest = canonical_digest(manifest)
            cp.binding_provenance = binding_provenance_from_manifest(manifest)
        ready.__wrapped__((db, scope, contract, started_at))
        approved = adjudicate_judge_criteria({"intent": "balance"}, policy=frozen.criteria_policy,
            scope=scope, limits=contract.limits)
        with db.get_session() as session:
            request = prepare_judge_request(session, item_id=scope.item_execution_id, generation=scope.dispatch_generation,
                manifest=manifest, definition=frozen, approved_criteria=approved)
            fixed = context_for_item(session, item_id=scope.item_execution_id, generation=scope.dispatch_generation, manifest=manifest)
        yield registry, frozen, binding, request, fixed, manifest
    finally:
        setup.close()


def prepare(runtime, *, status="succeeded", verdict="PASS", reason="TASK_COMPLETE", confidence=Decimal("0.95")):
    from app.trajectory.judge import ParsedJudgeResult
    from app.trajectory.judge_budget import BudgetedJudgeProvider, JudgeBudgetOwner
    from app.trajectory.judge_execution import prepare_judge_evaluation
    from app.trajectory.judge_provider import JudgeProviderOutcome, JudgeUsage

    registry, frozen, binding, request, fixed, manifest = runtime
    calls = []

    class Provider:
        async def evaluate(self, prepared, definition):
            calls.append(prepared.input_digest)
            return JudgeProviderOutcome(ParsedJudgeResult(status, verdict, reason,
                (sorted(request.local_step_ids)[0],) if verdict is not None else (), confidence),
                JudgeUsage(40, 20), request.input_digest, definition.definition_digest, 5)

    class Store:
        def reserve(self, call, owner):
            return object()

        def mark_sent(self, receipt):
            pass

        def settle(self, receipt, *, usage):
            pass

        def release_unsent(self, receipt):
            pass

    result = asyncio.run(prepare_judge_evaluation(binding, request=request,
        provider=BudgetedJudgeProvider(Provider(), Store()), evaluation_generation=0,
        owner=JudgeBudgetOwner(kind="execution", generation=request.scope.dispatch_generation, lease_token="lease-1"),
        manifest_schema_version="1.3"))
    assert calls == [request.input_digest]
    return result


def evaluate(runtime, prepared=None, **changes):
    registry, frozen, binding, request, fixed, manifest = runtime
    values = dict(manifest=manifest, output={}, expected_output={}, registry=registry, context=fixed,
        prepared_results={binding.binding_id: prepared} if prepared is not None else None, evaluation_generation=0)
    values.update(changes)
    return evaluate_frozen_item(**values)


def test_registered_judge_freezes_complete_definition_and_actual_implementation():
    registry, frozen, binding = registered()
    assert binding.executor_type == "trajectory_judge" and binding.result_type == "categorical"
    assert binding.category_values == ("PASS", "FAIL") and binding.normalization_rule is None
    assert binding.input_contract["judge_definition_digest"] == frozen.definition_digest
    assert binding.release_eligible is False and "JUDGE_ADMISSION_UNAVAILABLE" in binding.eligibility_reasons
    assert registry.resolve(binding.evaluator_id, binding.version)["release_eligible"] is False


def test_prompt_change_requires_new_version_and_changes_binding():
    from app.trajectory.judge_execution import register_judge_version
    registry, original, binding = registered()
    text = original.prompt_text + " Updated."
    changed = definition(**(original.model_dump() | {"prompt_text": text,
        "prompt_digest": "sha256:" + hashlib.sha256(text.encode()).hexdigest()}))
    with pytest.raises(ValueError, match="已存在且测量语义不同"):
        register_judge_version(registry, evaluator_id=binding.evaluator_id, definition=changed)
    changed = changed.model_copy(update={"version": "1.1"})
    register_judge_version(registry, evaluator_id=binding.evaluator_id, definition=changed)
    from app.trajectory.codec import canonical_dumps
    new = freeze_binding(registry.definition(binding.evaluator_id), registry.version(binding.evaluator_id, "1.1"),
        runner_identity=binding.runner, params={"judge_definition_json": canonical_dumps(changed)})
    assert new.binding_id != binding.binding_id and new.definition_digest != binding.definition_digest


@pytest.mark.parametrize("damage", ["artifact", "params", "kind", "critical", "output", "eligibility"])
def test_frozen_judge_cannot_resolve_changed_contract(runtime, damage):
    from app.trajectory.codec import canonical_dumps
    registry, frozen, binding, request, fixed, manifest = runtime
    if damage == "artifact":
        binding = replace(binding, artifact=replace(binding.artifact, digest="sha256:" + "f" * 64))
    elif damage == "params":
        binding = replace(binding, params={"judge_definition_json": canonical_dumps(frozen.model_copy(update={"sample_rate": Decimal(0)}))})
    elif damage == "kind":
        binding = replace(binding, input_contract=binding.input_contract | {"judge_kind": "complex_policy"})
    elif damage == "critical":
        binding = replace(binding, critical=True)
    elif damage == "eligibility":
        binding = replace(binding, release_eligible=True, eligibility_reasons=())
    else:
        binding = replace(binding, category_values=("PASS", "FAIL", "UNCERTAIN"))
    changed = deepcopy(manifest)
    changed["evaluators"] = [binding.to_payload()]
    from app.evaluator_binding import manifest_measurement_digest
    changed["measurement_digest"] = manifest_measurement_digest(changed)
    changed["contract_digests"]["measurement"]["digest"] = changed["measurement_digest"]
    with pytest.raises(ValueError):
        resolve_execution_plan(changed, registry=registry)


def test_missing_prepared_result_is_unknown_without_network(runtime):
    with patch("httpx.AsyncClient.send", side_effect=AssertionError("同步执行器不能请求网络")) as send:
        result = evaluate(runtime)
    send.assert_not_called()
    assert result.typed_results[0].error_code == "JUDGE_PREPARED_RESULT_UNAVAILABLE"
    assert result.quality_conclusion == "unknown" and result.scores == {}


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_verified_result_retains_typed_provenance_and_frozen_evidence(runtime, verdict):
    from app.quality_policy import QualityRule, freeze_quality_policy

    prepared = prepare(runtime, verdict=verdict, reason="TASK_COMPLETE" if verdict == "PASS" else "TASK_INCOMPLETE")
    binding = runtime[2]
    policy = freeze_quality_policy(policy_id="judge-test", version="1.0", bindings={binding.evaluator_id: binding},
        rules=[QualityRule(evaluator_id=binding.evaluator_id, result_type="categorical", operator="==",
            expected_value="PASS", required=True)])
    result = evaluate(runtime, prepared, quality_policy=policy.to_payload())
    row = result.typed_results[0]
    assert row.status == "succeeded" and row.value == verdict and row.result_type == "categorical"
    assert row.normalized_value is None and row.comment is None
    assert row.provenance.binding_id == runtime[2].binding_id
    assert row.evidence["evidence_ref"] == runtime[3].evidence_ref.model_dump(mode="json")
    assert row.evidence["input_digest"] == runtime[3].input_digest
    assert "rationale" not in row.evidence and row.duration_ms == 5
    assert result.scores == {}
    assert result.quality_conclusion == verdict.lower()


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_release_judge_default_policy_decides_prepared_result(runtime, verdict):
    from app.quality_policy import default_quality_policy

    prepared = prepare(runtime, verdict=verdict, reason="TASK_COMPLETE" if verdict == "PASS" else "TASK_INCOMPLETE")
    policy = default_quality_policy([runtime[2]])
    result = evaluate(runtime, prepared, quality_policy=policy.to_payload())
    assert result.quality_conclusion == verdict.lower()
    assert result.scores == {}


@pytest.mark.parametrize("damage", ["seal", "result", "input", "criteria", "scope", "context_scope", "context_ref", "expected", "raw_typed"])
def test_foreign_or_mutated_prepared_result_never_becomes_quality(runtime, damage):
    prepared = prepare(runtime)
    fixed = runtime[4]
    if damage == "seal":
        prepared = replace(prepared, _seal="")
    elif damage == "result":
        prepared = replace(prepared, result=replace(prepared.result, value="FAIL"))
    elif damage in {"input", "criteria"}:
        prepared = replace(prepared, **{damage + "_digest": "sha256:" + "f" * 64})
    elif damage == "scope":
        prepared = replace(prepared, scope=prepared.scope.model_copy(update={"item_execution_id": "sibling"}))
    elif damage == "context_scope":
        fixed = replace(fixed, trajectory=fixed.trajectory.model_copy(update={"scope": fixed.trajectory.scope.model_copy(update={"attempt_id": "old"})}))
    elif damage == "context_ref":
        fixed = replace(fixed, trajectory_evidence_ref=fixed.trajectory_evidence_ref.model_copy(update={"artifact_id": "foreign"}))
    elif damage == "expected":
        fixed = replace(fixed, expected_output={"intent": "escalate"})
    else:
        prepared = prepared.result
    row = evaluate(runtime, prepared, context=fixed).typed_results[0]
    assert row.status == "failed" and row.value is None and row.evidence is None
    assert row.error_code == "JUDGE_PREPARED_RESULT_INVALID"


@pytest.mark.parametrize("status,reason", [("no_result", "JUDGE_LOW_CONFIDENCE"), ("failed", "JUDGE_TIMEOUT")])
def test_no_result_and_failure_keep_unknown_without_fake_score(runtime, status, reason):
    prepared = prepare(runtime, status=status, verdict=None, reason=reason,
        confidence=Decimal("0.2") if status == "no_result" else None)
    result = evaluate(runtime, prepared)
    row = result.typed_results[0]
    assert row.status == status and row.value is None and result.quality_conclusion == "unknown"
    assert result.scores == {} and row.evidence["reason_code"] == reason


def test_unapproved_provider_payload_is_not_signed(runtime):
    with pytest.raises(ValueError, match="JUDGE_PREPARED_RESULT_INVALID"):
        prepare(runtime, reason="Bearer synthetic-secret")


def test_empty_target_set_does_not_consume_a_prepared_judge_result(runtime):
    prepared = prepare(runtime)
    result = evaluate(runtime, prepared, only_binding_ids=set())
    assert result.typed_results == () and result.eval_status == "skipped"


@pytest.mark.parametrize("generation", [None, True, 1])
def test_prepared_evaluation_generation_must_match_current_execution(runtime, generation):
    row = evaluate(runtime, prepare(runtime), evaluation_generation=generation).typed_results[0]
    assert row.status == "failed" and row.error_code == "JUDGE_PREPARED_RESULT_INVALID"


@pytest.mark.parametrize("damage", ["scope", "executor", "digest", "critical"])
def test_async_preparation_rejects_changed_binding_before_provider(runtime, damage):
    binding = runtime[2]
    changes = {"scope": {"scope": "run"}, "executor": {"executor_type": "builtin_python"},
        "digest": {"definition_digest": "sha256:" + "f" * 64}, "critical": {"critical": True}}
    damaged = runtime[:2] + (replace(binding, **changes[damage]),) + runtime[3:]
    with pytest.raises(ValueError):
        prepare(damaged)


@pytest.mark.parametrize("status,verdict,reason,confidence", [
    ("succeeded", "UNCERTAIN", "TASK_COMPLETE", Decimal("0.95")),
    ("succeeded", "PASS", "TASK_COMPLETE", Decimal("0.2")),
    ("failed", "FAIL", "JUDGE_TIMEOUT", None),
    ("no_result", None, "JUDGE_LOW_CONFIDENCE", Decimal("0.95")),
])
def test_invalid_parsed_outcome_is_not_signed(runtime, status, verdict, reason, confidence):
    with pytest.raises(ValueError, match="JUDGE_PREPARED_RESULT_INVALID"):
        prepare(runtime, status=status, verdict=verdict, reason=reason, confidence=confidence)


def test_legacy_manifest_cannot_admit_a_trajectory_judge(runtime):
    manifest = deepcopy(runtime[5])
    manifest["schema_version"] = "1.2"
    manifest.pop("trajectory")
    with pytest.raises(ValueError, match="JUDGE_INPUT_CONTRACT_UNAVAILABLE"):
        resolve_execution_plan(manifest, registry=runtime[0])


@pytest.mark.parametrize("damage", ["source_seal", "provider", "generation"])
def test_unverified_request_or_unbudgeted_provider_is_rejected_before_send(runtime, damage):
    from app.trajectory.judge_budget import BudgetedJudgeProvider, JudgeBudgetOwner
    from app.trajectory.judge_execution import prepare_judge_evaluation

    class Forbidden:
        async def evaluate(self, *args, **kwargs):
            pytest.fail("未验证输入不得调用 Provider")

        def reserve(self, *args, **kwargs):
            pytest.fail("未验证输入不得领取预算")

    request = runtime[3]
    provider = BudgetedJudgeProvider(Forbidden(), Forbidden())
    generation = 0
    if damage == "source_seal":
        request = replace(request, _seal="")
    elif damage == "provider":
        provider = Forbidden()
    else:
        generation = True
    with pytest.raises(ValueError):
        asyncio.run(prepare_judge_evaluation(runtime[2], request=request, provider=provider,
            evaluation_generation=generation, owner=JudgeBudgetOwner(kind="execution",
                generation=request.scope.dispatch_generation, lease_token="lease-1"), manifest_schema_version="1.3"))


def test_executor_cannot_substitute_a_result_after_preparation(runtime):
    prepared = prepare(runtime)
    with patch("app.trajectory.judge_execution.TrajectoryJudgeExecutor.execute",
        return_value=replace(prepared.result, value="FAIL")):
        row = evaluate(runtime, prepared).typed_results[0]
    assert row.status == "failed" and row.error_code == "JUDGE_PREPARED_RESULT_INVALID"


def test_malformed_envelope_cannot_leak_exception_text(runtime):
    prepared = prepare(runtime)

    class Exploding:
        def to_payload(self):
            raise RuntimeError("Bearer synthetic-secret")

    row = evaluate(runtime, replace(prepared, result=Exploding())).typed_results[0]
    assert row.error_code == "JUDGE_PREPARED_RESULT_INVALID"
    assert "synthetic-secret" not in repr(row)


def test_direct_consumer_normalizes_exception_from_nested_field(runtime):
    from app.trajectory.judge_execution import execute_prepared_judge_result
    from app.trajectory.manifest import load_trajectory_contract
    from pydantic import BaseModel

    class Exploding(BaseModel):
        def model_dump(self, **kwargs):
            raise RuntimeError("Bearer synthetic-secret")

    prepared = prepare(runtime)
    damaged = replace(prepared, result=replace(prepared.result, evidence={"unexpected": Exploding()}))
    row = execute_prepared_judge_result(binding=runtime[2], context=runtime[4],
        trajectory_contract=load_trajectory_contract(runtime[5]), prepared_result=damaged, evaluation_generation=0)
    assert row.status == "failed" and row.error_code == "JUDGE_PREPARED_RESULT_INVALID"
    assert "synthetic-secret" not in repr(row)


@pytest.mark.parametrize("module_name", ["app.trajectory.evidence", "app.trajectory.checkpoint",
    "app.execution_checkpoint", "app.db_models", "app.db", "app.config"])
def test_source_validation_and_storage_changes_invalidate_frozen_judge(runtime, monkeypatch, module_name):
    import importlib
    import inspect

    from app.trajectory.judge_execution import judge_implementation_artifact

    module = importlib.import_module(module_name)
    original_source = inspect.getsource
    original_digest = judge_implementation_artifact().digest

    def changed_source(value):
        source = original_source(value)
        return source + "\n# simulated source revision\n" if value is module else source

    monkeypatch.setattr(inspect, "getsource", changed_source)
    assert judge_implementation_artifact().digest != original_digest
    with pytest.raises(ValueError) as error:
        resolve_execution_plan(runtime[5], registry=runtime[0])
    assert error.value.code == "EVALUATOR_ARTIFACT_DIGEST_MISMATCH"
