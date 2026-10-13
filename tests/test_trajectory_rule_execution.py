from dataclasses import replace

import pytest
import test_trajectory_manifest as launch_fixtures
from app.evaluator_binding import EvaluatorBindingError, evaluate_frozen_item, freeze_binding
from app.evaluators import default_evaluator_registry as registry
from test_trajectory_manifest import create
from test_trajectory_rules import ORDER, A, B, trajectory
from test_trajectory_rules import context as rule_context

launch_context = launch_fixtures.context


@pytest.fixture
def context(launch_context):
    _, contract = trajectory(with_contract=True)
    return (*launch_context[:3], contract)


def selection(params):
    return {"id": "trajectory." + params["rule"], "version": "1.0.0", "params": params}


def test_new_rules_are_opt_in_and_do_not_replace_published_output_metrics():
    assert registry.default_item_ids() == sorted(["intent_match", "required_tool_match", "pii_safe", "escalation_match"])
    for rule in ("require_tool", "forbid_tool", "max_tool_calls", "tool_order"):
        definition = registry.definition("trajectory." + rule)
        version = registry.version(definition.id, "1.0.0")
        assert not definition.default_selected and version.result_type == "boolean"
        assert version.input_contract["input_kind"] == "trajectory"


def test_canonical_params_affect_measurement_binding_identity_and_request_idempotency(context):
    params = {"rule": "max_tool_calls", "tool_identity": B, "max_count": 1}
    first = create(context, evaluator_selections=[selection(params)], idempotency_key="observed-config")
    canonical = params | {"phase": "EXECUTED", "scope": "ROOT_SUBTREE", "workflow_scope_id": None}
    same = create(context, evaluator_selections=[selection(canonical)], idempotency_key="observed-config")
    assert same.id == first.id
    second = create(context, evaluator_selections=[selection(params | {"max_count": 0})])
    assert first.manifest["measurement_digest"] != second.manifest["measurement_digest"]
    bound = first.manifest["evaluators"][0]
    assert bound["params"] == canonical and bound["normalization_rule"] is None
    assert bound["binding_id"] != second.manifest["evaluators"][0]["binding_id"]
    with pytest.raises(ValueError, match="Idempotency key conflict"):
        create(context, evaluator_selections=[selection(params | {"max_count": 0})], idempotency_key="observed-config")


@pytest.mark.parametrize("scenario,expected", [("serial", True), ("b-a-b", False), ("parallel", None)])
def test_shared_executor_preserves_boolean_and_missing_typed_results(context, scenario, expected):
    launch = create(context, evaluator_selections=[selection(ORDER)])
    value = rule_context(trajectory(scenario))
    result = evaluate_frozen_item(launch.manifest, output=value.output, expected_output={}, context=value)
    assert len(result.typed_results) == 1
    measured = result.typed_results[0]
    assert measured.status == ("succeeded" if expected is not None else "no_result")
    assert measured.value is expected and measured.normalized_value is None and result.scores == {}
    assert measured.evidence["evidence_ref"] == value.trajectory_evidence_ref.model_dump(mode="json")
    assert measured.provenance.binding_id == launch.manifest["evaluators"][0]["binding_id"]


def test_missing_context_is_unknown_without_calling_legacy_output_rule(context):
    launch = create(context, evaluator_selections=[selection({"rule": "require_tool", "tool_identity": A})])
    result = evaluate_frozen_item(launch.manifest, output={"tool_calls": [{"name": A}]}, expected_output={})
    assert result.typed_results[0].status == "no_result" and result.quality_conclusion == "unknown"
    assert result.eval_status == "failed" and result.eval_error is None


def test_requested_phase_freezes_its_own_capability(context):
    launch = create(context, evaluator_selections=[selection({"rule": "forbid_tool", "tool_identity": B, "phase": "REQUESTED"})])
    assert launch.manifest["evaluators"][0]["input_contract"]["required_capabilities"] == ["tool_requests"]


def test_invalid_rule_parameters_and_missing_trajectory_contract_fail_before_creation(context):
    with pytest.raises(EvaluatorBindingError) as failure:
        create(context, evaluator_selections=[selection({"rule": "max_tool_calls", "tool_identity": B, "max_count": True})])
    assert failure.value.code == "TRAJECTORY_RULE_PARAMS_INVALID"
    with pytest.raises(EvaluatorBindingError) as failure:
        context[2].create_launch(agent_id="banking-agent", agent_version="native", dataset_name="banking-agent-regression",
            evaluator_selections=[selection(ORDER)])
    assert failure.value.code == "TRAJECTORY_RULE_CONTRACT_REQUIRED"


def test_helper_source_drift_cannot_execute_under_a_frozen_identity(context, monkeypatch):
    launch = create(context, evaluator_selections=[selection(ORDER)])
    import app.trajectory.bindings as bindings
    original = bindings.implementation_dependencies
    monkeypatch.setattr(bindings, "implementation_dependencies", lambda fn: (*original(fn), "sha256:" + "f" * 64))
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=rule_context(trajectory()))
    assert result.eval_status == "failed" and result.quality_conclusion == "unknown" and result.scores == {}
    assert not result.typed_results


def test_frozen_output_binding_artifact_and_normalization_are_unchanged(context):
    version = registry.version("answer_present", "1.0.0")
    bound = freeze_binding(registry.definition("answer_present"), version, runner_identity={})
    assert bound.normalization_rule["kind"] == "boolean" and bound.params == {}


@pytest.mark.parametrize("field,value", [("algorithm_version", "unsupported"), ("trajectory_schema_version", "2.0"),
    ("rule_id", "trajectory.forbid_tool")])
def test_registered_definition_cannot_lie_about_the_algorithm_contract(field, value):
    version = registry.version("trajectory.tool_order", "1.0.0")
    version = replace(version, input_contract=version.input_contract | {field: value})
    with pytest.raises(EvaluatorBindingError):
        freeze_binding(registry.definition("trajectory.tool_order"), version, runner_identity={}, params=ORDER)


def test_typed_adapter_rejects_foreign_provenance_and_unsafe_evidence(context, monkeypatch):
    from app.evaluator_binding import BuiltinPythonExecutor, EvaluatorBinding
    from app.evaluator_results import build_result
    launch = create(context, evaluator_selections=[selection(ORDER)])
    binding = EvaluatorBinding.from_payload(launch.manifest["evaluators"][0])
    raw = build_result(binding, True, manifest_schema_version="1.3", evidence={"reason_code": "synthetic-secret"})
    for forged in (replace(raw, provenance=replace(raw.provenance, binding_id="foreign")), raw):
        monkeypatch.setattr(BuiltinPythonExecutor, "execute", lambda *args, result=forged, **kwargs: result)
        result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=rule_context(trajectory()))
        assert result.typed_results[0].status == "failed"
        assert "synthetic-secret" not in str(result.typed_results[0].to_payload())


def test_context_contract_must_match_the_launch_contract_before_measurement(context):
    from test_trajectory_rules import reseal
    launch = create(context, evaluator_selections=[selection(ORDER)])
    value = reseal(trajectory(), lambda p: p["provenance"].update(trajectory_contract_digest="sha256:" + "0" * 64))
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=rule_context(value))
    assert result.typed_results[0].status == "failed"
    assert result.typed_results[0].error_code == "TRAJECTORY_RULE_INPUT_INVALID"


@pytest.mark.parametrize("status", ["succeeded", "no_result"])
def test_typed_adapter_never_passes_through_untrusted_error_text(context, monkeypatch, status):
    from app.evaluator_binding import BuiltinPythonExecutor, EvaluatorBinding
    from app.trajectory.rules import evaluate_trajectory_rule
    launch = create(context, evaluator_selections=[selection(ORDER)])
    bound = EvaluatorBinding.from_payload(launch.manifest["evaluators"][0])
    ctx = rule_context(trajectory()) if status == "succeeded" else None
    raw = evaluate_trajectory_rule(ctx, ORDER, binding=bound, manifest_schema_version="1.3")
    forged = replace(raw, error_message="synthetic-secret")
    monkeypatch.setattr(BuiltinPythonExecutor, "execute", lambda *a, **kw: forged)
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=ctx)
    assert result.typed_results[0].status == "failed"
    assert "synthetic-secret" not in str(result.typed_results[0].to_payload())


def test_public_launch_creation_carries_exact_opt_in_contract_and_params(context):
    from decimal import Decimal

    from app.api_launches import get_services, router
    from app.db_models import ExperimentLaunchRecord
    from app.trajectory.codec import canonical_dumps
    from app.trajectory.contract import TrajectoryContract
    from app.trajectory.manifest import load_trajectory_contract
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_services] = lambda: (*context[:3], None)
    raw = context[3].model_dump()
    raw["limits"]["quiet_window_seconds"] = Decimal("0.12345678901234567890123456789")
    contract = TrajectoryContract.model_validate(raw)
    payload = {"agent_id": "banking-agent", "agent_version": "native",
        "dataset_name": "banking-agent-regression", "evaluator_selections": [selection(ORDER)],
        "trajectory_contract_json": canonical_dumps(contract)}
    with TestClient(app) as client:
        response = client.post("/api/v1/experiment-launches", json=payload)
    assert response.status_code == 201, response.json()
    with context[0].get_session() as session:
        launch = session.get(ExperimentLaunchRecord, response.json()["id"])
        assert launch.manifest["schema_version"] == "1.3"
        assert launch.manifest["evaluators"][0]["params"]["mode"] == "EVERY_AFTER"
        assert load_trajectory_contract(launch.manifest).limits.quiet_window_seconds == raw["limits"]["quiet_window_seconds"]


@pytest.mark.parametrize("raw", ["broken-synthetic-secret", '{"unexpected":"synthetic-secret"}', '[1]', '{}'])
def test_invalid_public_contract_is_a_finite_error_without_values(context, raw):
    from app.api_launches import create_experiment_launch
    from app.models import ExperimentLaunchCreateRequest
    from fastapi import HTTPException
    payload = ExperimentLaunchCreateRequest.model_validate({"agent_id": "banking-agent", "agent_version": "native",
        "dataset_name": "banking-agent-regression", "evaluator_selections": [selection(ORDER)], "trajectory_contract_json": raw})
    with pytest.raises(HTTPException) as failure:
        create_experiment_launch(payload, idempotency_key=None, services=(*context[:3], None))
    assert failure.value.status_code == 400
    assert "TRAJECTORY_MANIFEST_CONTRACT_INVALID" in str(failure.value.detail) and "synthetic-secret" not in str(failure.value.detail)


@pytest.mark.parametrize("injected_flag", [False, True])
def test_public_contract_without_trajectory_binding_is_rejected_before_creation(context, injected_flag):
    from app.api_launches import get_services, router
    from app.db_models import ExperimentLaunchRecord
    from app.trajectory.codec import canonical_dumps
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_services] = lambda: (*context[:3], None)
    with TestClient(app) as client:
        response = client.post("/api/v1/experiment-launches", json={"agent_id": "banking-agent", "agent_version": "native",
            "dataset_name": "banking-agent-regression", "trajectory_contract_json": canonical_dumps(context[3]), "capture_only": injected_flag})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "TRAJECTORY_RULE_BINDING_REQUIRED"
    with context[0].get_session() as session:
        assert not session.scalars(select(ExperimentLaunchRecord)).all()


def test_contract_only_calibration_requires_an_explicit_internal_opt_in(context):
    kwargs = {"agent_id": "banking-agent", "agent_version": "native", "dataset_name": "banking-agent-regression",
        "trajectory_contract": context[3]}
    with pytest.raises(EvaluatorBindingError) as failure:
        context[2].create_launch(**kwargs)
    assert failure.value.code == "TRAJECTORY_RULE_BINDING_REQUIRED"
    assert context[2].create_launch(**kwargs, capture_only=True).manifest["schema_version"] == "1.3"


def test_jsonpath_source_drift_invalidates_the_frozen_rule_artifact(context, monkeypatch):
    import app.trajectory.bindings as bindings
    import app.trajectory.paths as paths
    launch = create(context, evaluator_selections=[selection(ORDER)])
    original = bindings.inspect.getsource
    monkeypatch.setattr(bindings.inspect, "getsource", lambda obj: original(obj) + ("\n# parser changed" if obj is paths else ""))
    result = evaluate_frozen_item(launch.manifest, output={}, expected_output={}, context=rule_context(trajectory()))
    assert result.eval_status == "failed" and result.quality_conclusion == "unknown"
    assert not result.typed_results
