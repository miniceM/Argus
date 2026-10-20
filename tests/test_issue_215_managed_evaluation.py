"""#215 只配置采纳契约，不回收/执行远程评分。"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.evaluator_binding import evaluate_frozen_item  # noqa: E402
from app.manifest import LaunchService  # noqa: E402
from test_launch_manifest_freeze import setup_db  # noqa: E402


def contract():
    return {
        "project_id": "project-215",
        "environment": "production",
        "target": {"name": "remote-agent-http", "type": "TOOL"},
        "scores": [
            {
                "name": "correctness",
                "data_type": "boolean",
                "required": True,
                "rule_id": "rule-215",
                "evaluator_id": "judge-215",
            }
        ],
    }


def rules():
    return [
        {
            "evaluator_id": "correctness",
            "result_type": "boolean",
            "operator": "==",
            "expected_value": True,
            "required": True,
        }
    ]


@pytest.fixture
def service(tmp_path):
    db, registry = setup_db(tmp_path)
    return LaunchService(db, registry)


def create(service, **overrides):
    return service.create_launch(
        agent_id="banking-agent", agent_version="v1", dataset_name="banking-agent-regression", **overrides
    )


def managed(service, **overrides):
    args = dict(evaluation_source="langfuse_managed", managed_evaluation=contract(), quality_policy_rules=rules())
    args.update(overrides)
    return create(service, **args)


def test_builtin_manifest_and_idempotency_unchanged(service):
    old = create(service, idempotency_key="builtin")
    explicit = create(service, evaluation_source="builtin", idempotency_key="builtin")
    assert old.id == explicit.id
    assert "evaluation_source" not in old.manifest
    assert "managed_evaluation" not in old.manifest
    assert old.manifest["evaluators"]


def test_managed_freezes_copies_and_reads_contract(service):
    selected = contract()
    launch = managed(service, managed_evaluation=selected, idempotency_key="managed")
    frozen = copy.deepcopy(launch.manifest)
    selected["scores"][0]["rule_id"] = "changed-remotely"
    assert launch.manifest == frozen
    assert frozen["evaluation_source"] == "langfuse_managed"
    assert frozen["evaluators"] == []
    assert frozen["managed_evaluation"]["execution_semantics"] == "langfuse_latest"
    assert frozen["managed_evaluation"]["scores"][0]["rule_id"] == "rule-215"
    assert frozen["managed_evaluation"]["acceptance_digest"]
    with service.db_manager.get_session() as session:
        from app.db_models import ExperimentLaunchRecord

        assert session.get(ExperimentLaunchRecord, launch.id).manifest == frozen
    assert managed(service, idempotency_key="managed").id == launch.id
    with pytest.raises(ValueError, match="[Ii]dempotency"):
        managed(service, managed_evaluation=selected, idempotency_key="managed")


def test_managed_never_runs_builtin_or_passes_without_scores(service):
    launch = managed(service)
    result = evaluate_frozen_item(launch.manifest, output={"answer": "correct"}, expected_output={"answer": "correct"})
    assert result.quality_conclusion == "unknown"
    assert result.scores == {}
    assert not result.typed_results
    assert "LANGFUSE_MANAGED_SCORES_PENDING" in result.eval_error
    assert result.quality_decision.releasable is False


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda c: c.update(project_id=""), "MANAGED_IDENTITY_REQUIRED"),
        (lambda c: c.update(environment="staging"), "MANAGED_ENVIRONMENT_MISMATCH"),
        (lambda c: c["target"].update(name="argus-run-item-replay"), "MANAGED_TARGET_UNSUPPORTED"),
        (lambda c: c["scores"][0].update(name="  "), "MANAGED_SCORE_NAME_INVALID"),
        (lambda c: c["scores"].append(copy.deepcopy(c["scores"][0])), "MANAGED_SCORE_DUPLICATE"),
        (lambda c: c["scores"][0].update(data_type="invalid"), "MANAGED_SCORE_TYPE_UNSUPPORTED"),
        (lambda c: c["scores"][0].update(data_type="text"), "MANAGED_REQUIRED_TYPE_UNSUPPORTED"),
        (lambda c: c["scores"][0].update(project_id="another"), "MANAGED_PROJECT_MISMATCH"),
        (lambda c: c["scores"][0].update(rule_id=""), "MANAGED_RULE_REFERENCE_REQUIRED"),
    ],
)
def test_invalid_contracts_rejected(service, mutate, code):
    selected = contract()
    mutate(selected)
    with pytest.raises(ValueError) as exc:
        managed(service, managed_evaluation=selected)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "policy",
    [
        None,
        [],
        [{"evaluator_id": "unselected", "operator": "==", "expected_value": True, "result_type": "boolean"}],
        [{"evaluator_id": "correctness", "operator": ">=", "threshold": 1, "result_type": "numeric"}],
        [{**rules()[0], "required": False}],
    ],
)
def test_invalid_policy_rejected(service, policy):
    with pytest.raises(ValueError):
        managed(service, quality_policy_rules=policy)


def test_rejects_mixed_or_missing_configuration(service):
    for args in [
        dict(managed_evaluation=contract()),
        dict(evaluation_source="other"),
        dict(evaluation_source="langfuse_managed"),
        dict(evaluation_source="langfuse_managed", managed_evaluation=contract(), evaluator_ids=["answer_present"]),
    ]:
        with pytest.raises(ValueError):
            create(service, **args)


@pytest.fixture
def api_client(service):
    from app.api_launches import get_services
    from app.limiter import MemoryAgentLimiter
    from app.main import app
    from app.orchestrator import LaunchOrchestrator
    from app.queue import MemoryQueueAdapter
    from fastapi.testclient import TestClient

    orchestrator = LaunchOrchestrator(service.db_manager, MemoryQueueAdapter(), MemoryAgentLimiter())
    app.dependency_overrides[get_services] = lambda: (service.db_manager, service.registry, service, orchestrator)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_services, None)


def api_payload():
    return dict(
        agent_id="banking-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        evaluation_source="langfuse_managed",
        managed_evaluation=contract(),
        quality_policy={"rules": rules()},
    )


def test_api_creates_and_queries_immutable_contract(api_client):
    payload = api_payload()
    response = api_client.post("/api/v1/experiment-launches", json=payload)
    assert response.status_code == 201, response.text
    created = response.json()
    payload["managed_evaluation"]["scores"][0]["rule_id"] = "changed"
    read = api_client.get(f"/api/v1/experiment-launches/{created['id']}")
    assert read.status_code == 200
    assert read.json()["manifest"] == created["manifest"]
    assert read.json()["quality_conclusion"].lower() == "unknown"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"evaluation_source": ""}, "EVALUATION_SOURCE_INVALID"),
        ({"evaluation_source": "unknown"}, "EVALUATION_SOURCE_INVALID"),
        ({"evaluation_source": "builtin"}, "EVALUATION_SOURCE_CONFLICT"),
        ({"managed_evaluation": None}, "MANAGED_CONTRACT_REQUIRED"),
        ({"evaluator_ids": []}, "EVALUATION_SOURCE_CONFLICT"),
        ({"quality_policy": None}, "MANAGED_QUALITY_POLICY_REQUIRED"),
    ],
)
def test_api_errors_have_stable_codes(api_client, patch, code):
    payload = api_payload()
    payload.update(patch)
    response = api_client.post("/api/v1/experiment-launches", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == code
    assert response.json()["detail"]["recovery"]


def test_api_rejects_unknown_fields_and_malformed_types(api_client):
    for patch in [dict(implementation_version="latest"), dict(scores=[]), dict(project_id=7)]:
        payload = api_payload()
        payload["managed_evaluation"].update(patch)
        assert api_client.post("/api/v1/experiment-launches", json=payload).status_code == 422


def test_multiple_score_types_and_optional_text(service):
    selected = contract()
    selected["scores"] += [
        {"name": "accuracy", "data_type": "numeric", "required": True, "rule_id": "numeric-rule"},
        {
            "name": "category",
            "data_type": "categorical",
            "required": False,
            "rule_id": "category-rule",
            "category_values": ["safe", "unsafe"],
        },
        {"name": "explanation", "data_type": "text", "required": False, "rule_id": "text-rule"},
    ]
    policy = rules() + [
        {"evaluator_id": "accuracy", "result_type": "numeric", "operator": ">=", "threshold": 0.8},
        {
            "evaluator_id": "category",
            "result_type": "categorical",
            "operator": "==",
            "expected_value": "safe",
            "required": False,
        },
    ]
    launch = managed(service, managed_evaluation=selected, quality_policy_rules=policy)
    assert len(launch.manifest["managed_evaluation"]["scores"]) == 4
    policy[-1]["expected_value"] = "not-a-category"
    with pytest.raises(ValueError) as exc:
        managed(service, managed_evaluation=selected, quality_policy_rules=policy)
    assert exc.value.code == "QUALITY_POLICY_CATEGORY_NOT_ALLOWED"


def test_configured_project_and_environment_must_match(service, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "another-project")
    with pytest.raises(ValueError) as exc:
        managed(service)
    assert exc.value.code == "MANAGED_PROJECT_MISMATCH"
    monkeypatch.delenv("LANGFUSE_PROJECT_ID")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "another-environment")
    with pytest.raises(ValueError) as exc:
        managed(service)
    assert exc.value.code == "MANAGED_ENVIRONMENT_MISMATCH"


def test_legacy_reader_and_measurement_digest(service):
    from app.evaluator_binding import manifest_measurement_digest
    from app.managed_evaluation import evaluation_source

    assert evaluation_source(None) == "builtin"
    assert evaluation_source({"schema_version": "1.0"}) == "builtin"
    launch = managed(service)
    assert manifest_measurement_digest(launch.manifest) == launch.manifest["measurement_digest"]
    changed = copy.deepcopy(launch.manifest)
    changed["managed_evaluation"]["scores"][0]["name"] = "different"
    assert manifest_measurement_digest(changed) != manifest_measurement_digest(launch.manifest)


def test_worker_successful_http_keeps_quality_unknown(service, setup_runtime):
    from test_issue_84_eval_only_retry import _item, _run_item

    launch = managed(service)
    manifest = copy.deepcopy(launch.manifest)
    # 复用独立 Worker/Queue 测试环境，不调用真实 Agent 或 Langfuse。
    manifest["agent"].update(
        agent_id="test-agent", agent_version_id="test-agent-v1", endpoint="http://localhost/invoke", request_mapping={}
    )
    manifest["dataset"]["items"] = [{"id": "0", "input": {}, "expected_output": {"answer": "ok"}}]
    db, _, _, _, _, item_id, calls, ok = _run_item(setup_runtime, manifest, "managed215", {"answer": "ok"})
    assert ok and calls == 1
    item = _item(db, item_id)
    assert item.execution_status == "succeeded"
    assert item.quality_conclusion == "unknown"
    assert item.scores == {}
    assert item.quality_evaluation["releasable"] is False


@pytest.mark.parametrize("threshold", ["NaN", "Infinity", "-Infinity"])
def test_api_rejects_non_finite_threshold_with_json_safe_error(api_client, threshold):
    payload = api_payload()
    payload["managed_evaluation"]["scores"][0]["data_type"] = "numeric"
    payload["quality_policy"]["rules"] = [
        {"evaluator_id": "correctness", "result_type": "numeric", "operator": ">=", "threshold": threshold}
    ]
    response = api_client.post("/api/v1/experiment-launches", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "QUALITY_POLICY_THRESHOLD_INVALID"


def test_managed_summary_cannot_accept_injected_local_result(service):
    from types import SimpleNamespace

    from app.evaluator_binding import summarize_typed_results

    launch = managed(service)
    injected = SimpleNamespace(evaluator_id="correctness", result_type="boolean", status="succeeded", value=True)
    result = summarize_typed_results([], [injected], manifest=launch.manifest)
    assert result.quality_conclusion == "unknown"
    assert result.quality_decision.releasable is False
    assert result.typed_results == ()
    assert result.scores == {}
