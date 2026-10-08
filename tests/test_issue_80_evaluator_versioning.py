"""Issue #80 - exact Evaluator version selection and release eligibility.

Maps 1:1 to the acceptance criteria of Argus Issue #80.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.evaluators import (  # noqa: E402
    ELIGIBILITY_REASON_MESSAGES,
    EvaluatorDefinition,
    EvaluatorRegistry,
    EvaluatorSelectionError,
    EvaluatorVersion,
    default_evaluator_registry,
)


def _version(
    evaluator_id: str,
    version: str,
    *,
    threshold: float = 1.0,
    result_type: str = "numeric",
    implementation_ref: str | None = "builtin:demo@1",
    executor_type: str = "builtin_python",
    category_values: tuple[str, ...] | None = None,
    description_shift: str = "",
) -> EvaluatorVersion:
    return EvaluatorVersion(
        evaluator_id=evaluator_id,
        version=version,
        result_type=result_type,
        scope="item",
        threshold=threshold,
        direction="higher_is_better",
        critical=False,
        implementation_ref=implementation_ref,
        executor_type=executor_type,
        input_contract={"type": "object", "description": f"input {description_shift}"},
        output_contract={"type": result_type},
        param_schema={"type": "object", "properties": {}},
        category_values=category_values,
    )


def _definition(
    evaluator_id: str,
    *,
    default_version: str = "1.0.0",
    execution_owner: str = "ARGUS",
    definition_source: str = "ARGUS_BUILTIN",
    display_description: str = "demo",
) -> EvaluatorDefinition:
    return EvaluatorDefinition(
        id=evaluator_id,
        name="Demo",
        display_description=display_description,
        definition_source=definition_source,
        execution_owner=execution_owner,
        default_version=default_version,
    )


# ---------------------------------------------------------------------------
# AC1: v1/v2 coexist; same id/version content change is rejected;
#      display-only description edits do not change measurement semantics.
# ---------------------------------------------------------------------------

def test_same_evaluator_exposes_multiple_coexisting_versions():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("latency_budget", default_version="2.0.0"))
    registry.register_version(_version("latency_budget", "1.0.0", threshold=1.0))
    registry.register_version(_version("latency_budget", "2.0.0", threshold=0.8))

    catalog = {entry["id"]: entry for entry in registry.list_versions()}["latency_budget"]
    assert [v["version"] for v in catalog["versions"]] == ["1.0.0", "2.0.0"]
    assert catalog["default_version"] == "2.0.0"

    # Both versions are individually addressable and keep their own threshold.
    assert registry.resolve("latency_budget", "1.0.0")["threshold"] == 1.0
    assert registry.resolve("latency_budget", "2.0.0")["threshold"] == 0.8


def test_republishing_same_version_with_new_semantics_is_rejected():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("latency_budget"))
    registry.register_version(_version("latency_budget", "1.0.0", threshold=1.0))

    with pytest.raises(EvaluatorSelectionError) as excinfo:
        registry.register_version(_version("latency_budget", "1.0.0", threshold=0.5))

    assert excinfo.value.code == "EVALUATOR_VERSION_IMMUTABLE_VIOLATION"
    # The original version is untouched.
    assert registry.resolve("latency_budget", "1.0.0")["threshold"] == 1.0


def test_republishing_identical_version_is_idempotent():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("latency_budget"))
    first = registry.register_version(_version("latency_budget", "1.0.0"))
    second = registry.register_version(_version("latency_budget", "1.0.0"))
    assert first is second


def test_display_description_change_does_not_change_measurement_digest():
    registry_v1 = EvaluatorRegistry(include_builtins=False)
    registry_v1.register_definition(_definition("latency_budget", display_description="原始说明"))
    registry_v1.register_version(_version("latency_budget", "1.0.0"))

    registry_v2 = EvaluatorRegistry(include_builtins=False)
    registry_v2.register_definition(
        _definition("latency_budget", display_description="改写后的展示说明")
    )
    registry_v2.register_version(_version("latency_budget", "1.0.0"))

    original = registry_v1.resolve("latency_budget", "1.0.0")["content_digest"]
    reworded = registry_v2.resolve("latency_budget", "1.0.0")["content_digest"]
    assert original == reworded

    # A semantic change, however, must move the digest.
    registry_v3 = EvaluatorRegistry(include_builtins=False)
    registry_v3.register_definition(_definition("latency_budget"))
    registry_v3.register_version(_version("latency_budget", "1.0.0", threshold=0.7))
    assert registry_v3.resolve("latency_budget", "1.0.0")["content_digest"] != original


# ---------------------------------------------------------------------------
# AC4: eligibility reasons for unusable executors / missing frozen identity;
#      disguising definition_source must not change eligibility.
# ---------------------------------------------------------------------------

def test_unsupported_executor_is_not_release_eligible_with_reason():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("remote_judge"))
    registry.register_version(
        _version("remote_judge", "1.0.0", executor_type="remote_http")
    )

    eligible, reasons = registry.release_eligibility("remote_judge", "1.0.0")
    assert eligible is False
    assert "EXECUTOR_UNSUPPORTED" in reasons
    assert ELIGIBILITY_REASON_MESSAGES["EXECUTOR_UNSUPPORTED"]


def test_missing_implementation_ref_is_not_release_eligible():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("no_artifact"))
    registry.register_version(_version("no_artifact", "1.0.0", implementation_ref=None))

    eligible, reasons = registry.release_eligibility("no_artifact", "1.0.0")
    assert eligible is False
    assert reasons == ["IMPLEMENTATION_REF_MISSING"]


def test_langfuse_owned_evaluator_is_not_release_eligible():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(
        _definition("online_rule", execution_owner="LANGFUSE", definition_source="LANGFUSE_ONLINE")
    )
    registry.register_version(_version("online_rule", "1.0.0"))

    eligible, reasons = registry.release_eligibility("online_rule", "1.0.0")
    assert eligible is False
    assert reasons == ["EXECUTION_OWNER_NOT_ARGUS"]


def test_disguising_definition_source_does_not_grant_eligibility():
    """Relabelling provenance must never change the eligibility answer."""
    honest = EvaluatorRegistry(include_builtins=False)
    honest.register_definition(
        _definition("online_rule", execution_owner="LANGFUSE", definition_source="LANGFUSE_ONLINE")
    )
    honest.register_version(_version("online_rule", "1.0.0"))

    disguised = EvaluatorRegistry(include_builtins=False)
    disguised.register_definition(
        _definition("online_rule", execution_owner="LANGFUSE", definition_source="ARGUS_BUILTIN")
    )
    disguised.register_version(_version("online_rule", "1.0.0"))

    assert honest.release_eligibility("online_rule", "1.0.0") == (
        disguised.release_eligibility("online_rule", "1.0.0")
    )
    assert disguised.release_eligibility("online_rule", "1.0.0")[0] is False


def test_categorical_without_enum_contract_is_not_release_eligible():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("tone"))
    registry.register_version(
        _version("tone", "1.0.0", result_type="categorical", category_values=None)
    )
    eligible, reasons = registry.release_eligibility("tone", "1.0.0")
    assert eligible is False
    assert "CATEGORICAL_VALUES_MISSING" in reasons


# ---------------------------------------------------------------------------
# AC5: server-side gate rejects unusable selections regardless of client.
# ---------------------------------------------------------------------------

def test_resolve_for_release_rejects_ineligible_version():
    registry = EvaluatorRegistry(include_builtins=False)
    registry.register_definition(_definition("online_rule", execution_owner="LANGFUSE"))
    registry.register_version(_version("online_rule", "1.0.0"))

    with pytest.raises(EvaluatorSelectionError) as excinfo:
        registry.resolve_for_release("online_rule", "1.0.0")

    assert excinfo.value.code == "EVALUATOR_NOT_RELEASE_ELIGIBLE"
    assert excinfo.value.eligibility_reasons == ["EXECUTION_OWNER_NOT_ARGUS"]
    payload = excinfo.value.to_payload()
    assert payload["evaluator_id"] == "online_rule"


def test_resolve_for_release_rejects_unknown_version():
    with pytest.raises(EvaluatorSelectionError) as excinfo:
        default_evaluator_registry.resolve_for_release("pii_safe", "9.9.9")
    assert excinfo.value.code == "EVALUATOR_VERSION_UNKNOWN"


def test_resolve_for_release_rejects_run_scope_for_item_launch():
    with pytest.raises(EvaluatorSelectionError) as excinfo:
        default_evaluator_registry.resolve_for_release("run_pass_rate", "1.0.0")
    assert excinfo.value.code == "EVALUATOR_SCOPE_UNSUPPORTED"


def test_builtin_diagnostics_remain_release_eligible():
    for evaluator_id in default_evaluator_registry.default_item_ids():
        spec = default_evaluator_registry.resolve_for_release(evaluator_id, "1.0.0")
        assert spec["release_eligible"] is True
        assert spec["eligibility_reasons"] == []


def test_builtin_catalog_exposes_contracts_for_every_default_version():
    catalog = {entry["id"]: entry for entry in default_evaluator_registry.list_versions()}
    for evaluator_id in default_evaluator_registry.default_item_ids():
        entry = catalog[evaluator_id]
        assert entry["versions"], f"{evaluator_id} must expose its versions"
        for version in entry["versions"]:
            assert version["content_digest"]
            assert version["result_type"]
            assert version["executor_type"]
            assert version["input_contract"]
            assert version["output_contract"]
            assert version["param_schema"]
            assert version["release_eligible"] is True


# ---------------------------------------------------------------------------
# AC5 (API level): bypassing the UI is rejected with a structured error and
#                  never creates a usable Launch.
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from app.main import app
    from fastapi.testclient import TestClient

    return TestClient(app)


_LAUNCH_BODY = {
    "agent_id": "banking-agent",
    "agent_version": "v1",
    "dataset_name": "banking-agent-regression",
}


def test_catalog_api_exposes_versions_and_eligibility(client):
    response = client.get("/api/v1/evaluators")
    assert response.status_code == 200
    catalog = {entry["id"]: entry for entry in response.json()}

    assert set(default_evaluator_registry.default_item_ids()).issubset(catalog)
    for entry in catalog.values():
        assert entry["versions"], f"{entry['id']} must expose at least one version"
        for version in entry["versions"]:
            assert version["content_digest"]
            assert isinstance(version["release_eligible"], bool)
            assert isinstance(version["eligibility_reasons"], list)


def test_api_rejects_unknown_version_and_creates_no_launch(client):
    before = client.get("/api/v1/experiment-launches").json()
    response = client.post(
        "/api/v1/experiment-launches",
        json={
            **_LAUNCH_BODY,
            "evaluator_selections": [{"id": "pii_safe", "version": "9.9.9"}],
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "EVALUATOR_VERSION_UNKNOWN"
    assert detail["version"] == "9.9.9"

    after = client.get("/api/v1/experiment-launches").json()
    assert len(after) == len(before)


def test_api_rejects_non_release_eligible_selection(client, monkeypatch):
    from app import evaluators as evaluators_module

    registry = evaluators_module.EvaluatorRegistry(include_builtins=False)
    registry.register_definition(
        evaluators_module.EvaluatorDefinition(
            id="online_rule",
            name="Online Rule",
            display_description="Langfuse 在线规则",
            definition_source="LANGFUSE_ONLINE",
            execution_owner="LANGFUSE",
            default_version="1.0.0",
        )
    )
    registry.register_version(
        evaluators_module.EvaluatorVersion(
            evaluator_id="online_rule",
            version="1.0.0",
            result_type="numeric",
            scope="item",
            threshold=1.0,
            direction="higher_is_better",
            critical=False,
            implementation_ref="langfuse:rule@1",
            executor_type="builtin_python",
            input_contract={},
            output_contract={},
            param_schema={},
        )
    )
    # manifest.py imports the registry by value, so both references must be
    # redirected for the server-side gate to observe the fixture registry.
    from app import manifest as manifest_module

    monkeypatch.setattr(evaluators_module, "default_evaluator_registry", registry)
    monkeypatch.setattr(manifest_module, "default_evaluator_registry", registry)

    response = client.post(
        "/api/v1/experiment-launches",
        json={
            **_LAUNCH_BODY,
            "evaluator_selections": [{"id": "online_rule", "version": "1.0.0"}],
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "EVALUATOR_NOT_RELEASE_ELIGIBLE"
    assert detail["eligibility_reasons"] == ["EXECUTION_OWNER_NOT_ARGUS"]


def test_api_rejects_unsupported_scope_selection(client):
    response = client.post(
        "/api/v1/experiment-launches",
        json={
            **_LAUNCH_BODY,
            "evaluator_selections": [{"id": "run_pass_rate", "version": "1.0.0"}],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "EVALUATOR_SCOPE_UNSUPPORTED"


def test_api_rejects_duplicate_and_ambiguous_selections(client):
    duplicate = client.post(
        "/api/v1/experiment-launches",
        json={
            **_LAUNCH_BODY,
            "evaluator_selections": [
                {"id": "pii_safe", "version": "1.0.0"},
                {"id": "pii_safe", "version": "1.0.0"},
            ],
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"]["code"] == "EVALUATOR_SELECTION_DUPLICATE"

    ambiguous = client.post(
        "/api/v1/experiment-launches",
        json={
            **_LAUNCH_BODY,
            "evaluator_ids": ["pii_safe"],
            "evaluator_selections": [{"id": "pii_safe", "version": "1.0.0"}],
        },
    )
    assert ambiguous.status_code == 400
    assert ambiguous.json()["detail"]["code"] == "EVALUATOR_SELECTION_AMBIGUOUS"


def test_api_freezes_the_exact_confirmed_version_into_the_manifest(client):
    response = client.post(
        "/api/v1/experiment-launches",
        json={
            **_LAUNCH_BODY,
            "evaluator_selections": [{"id": "pii_safe", "version": "1.0.0"}],
            "name": "issue-80-exact-version",
        },
    )
    assert response.status_code == 201, response.text
    manifest = response.json()["manifest"]
    frozen = manifest["evaluators"]
    assert len(frozen) == 1
    assert frozen[0]["id"] == "pii_safe"
    assert frozen[0]["version"] == "1.0.0"
    # The frozen binding carries the identity needed by later issues.
    assert frozen[0]["content_digest"]
    assert frozen[0]["implementation_ref"] == "builtin:pii_safe@1.0.0"
    assert frozen[0]["release_eligible"] is True
