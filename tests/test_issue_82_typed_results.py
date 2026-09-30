"""Issue #82 — typed, explainable EvaluationResults.

Covers the acceptance criteria of Argus Issue #82:
* all four result types round-trip with their original type and value;
* a real numeric 0 stays 0 while missing / NaN / Infinity / type-mismatch /
  failed / skipped each get their own status and are never shown as zero;
* every result carries frozen Binding / Manifest provenance;
* text and unordered categories never enter a mean; ``normalized_value`` may be
  null and only an explicitly frozen rule may produce a number;
* one failing Binding never erases the results that already succeeded;
* legacy numeric scores stay readable without coercing non-numeric values.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.aggregation import aggregate_run  # noqa: E402
from app.db_models import (  # noqa: E402
    EvaluationResultRecord,
    ExperimentItemExecutionRecord as Item,
    ExperimentLaunchRecord as Launch,
)
from app.evaluation_result_store import (  # noqa: E402
    load_typed_results,
    persist_typed_results,
)
from app.evaluator_binding import (  # noqa: E402
    EvaluatorBinding,
    evaluate_frozen_item,
    freeze_binding,
    normalization_rule_for,
)
from app.evaluator_results import (  # noqa: E402
    RESULT_STATUS_FAILED,
    RESULT_STATUS_NO_RESULT,
    RESULT_STATUS_SKIPPED,
    RESULT_STATUS_SUCCEEDED,
    ResultContractError,
    aggregate_numeric,
    build_result,
    coerce_typed_value,
    failed_result,
    legacy_numeric_results,
    normalized_value_for,
    project_legacy_scores,
    skipped_result,
)
from app.evaluators import (  # noqa: E402
    EvaluatorDefinition,
    EvaluatorRegistry,
    EvaluatorVersion,
    default_evaluator_registry,
)
from app.executor import SingleInvocationResult  # noqa: E402
from app.runner_identity import current_runner_identity  # noqa: E402

IDENTITY = current_runner_identity().model_dump()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_global_evaluator_registry():
    """Some acceptance cases register contract-violating providers on purpose."""
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


def freeze(evaluator_id: str, version: str = "1.0.0", registry=None) -> EvaluatorBinding:
    registry = registry or default_evaluator_registry
    definition = registry.definition(evaluator_id)
    resolved = registry.version(evaluator_id, version)
    return freeze_binding(definition, resolved, runner_identity=IDENTITY)


def manifest_for(*bindings: EvaluatorBinding, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.2",
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
            "max_retries": 0,
            "max_concurrency": 1,
            "rate_limit_per_minute": 60,
        },
        "dataset": {
            "items": [{"id": "0", "input": {}, "expected_output": {"expected_intent": "refund"}}]
        },
        "evaluators": [b.to_payload() for b in bindings],
        "runner": IDENTITY,
        **extra,
    }


def typed_registry() -> EvaluatorRegistry:
    """Register the contract-violating providers on the default registry.

    ``evaluate_frozen_item`` and the Worker resolve against the default registry,
    so the deterministic failure providers must live there for the integration
    path. The autouse fixture restores the registry afterwards.
    """
    from app import evaluators as ev

    registry = default_evaluator_registry
    for definition, version in [
        (
            EvaluatorDefinition(
                id="raising_evaluator",
                name="异常评测",
                display_description="总是抛错的确定性 Provider",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
            ),
            EvaluatorVersion(
                evaluator_id="raising_evaluator",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:raising_evaluator@1.0.0",
                executor_type="builtin_python",
                input_contract={"type": "object"},
                output_contract={"type": "number"},
                param_schema={"type": "object"},
                fn=ev.raising_evaluator,
            ),
        ),
        (
            EvaluatorDefinition(
                id="missing_value_evaluator",
                name="缺失值评测",
                display_description="返回空值的确定性 Provider",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
            ),
            EvaluatorVersion(
                evaluator_id="missing_value_evaluator",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:missing_value_evaluator@1.0.0",
                executor_type="builtin_python",
                input_contract={"type": "object"},
                output_contract={"type": "number"},
                param_schema={"type": "object"},
                fn=ev.missing_value_evaluator,
            ),
        ),
        (
            EvaluatorDefinition(
                id="non_finite_evaluator",
                name="非有限值评测",
                display_description="返回 NaN 的确定性 Provider",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
            ),
            EvaluatorVersion(
                evaluator_id="non_finite_evaluator",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:non_finite_evaluator@1.0.0",
                executor_type="builtin_python",
                input_contract={"type": "object"},
                output_contract={"type": "number"},
                param_schema={"type": "object"},
                fn=ev.non_finite_evaluator,
            ),
        ),
        (
            EvaluatorDefinition(
                id="type_mismatch_evaluator",
                name="类型不符评测",
                display_description="为 numeric 契约返回字符串的确定性 Provider",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
            ),
            EvaluatorVersion(
                evaluator_id="type_mismatch_evaluator",
                version="1.0.0",
                result_type="numeric",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:type_mismatch_evaluator@1.0.0",
                executor_type="builtin_python",
                input_contract={"type": "object"},
                output_contract={"type": "number"},
                param_schema={"type": "object"},
                fn=ev.type_mismatch_evaluator,
            ),
        ),
        (
            EvaluatorDefinition(
                id="out_of_range_category",
                name="枚举外分类",
                display_description="返回枚举外取值的确定性 Provider",
                definition_source="ARGUS_BUILTIN",
                execution_owner="ARGUS",
                default_version="1.0.0",
            ),
            EvaluatorVersion(
                evaluator_id="out_of_range_category",
                version="1.0.0",
                result_type="categorical",
                scope="item",
                threshold=1.0,
                direction="higher_is_better",
                critical=False,
                implementation_ref="builtin:out_of_range_category@1.0.0",
                executor_type="builtin_python",
                input_contract={"type": "object"},
                output_contract={"type": "string", "enum": ["a", "b"]},
                param_schema={"type": "object"},
                category_values=("a", "b"),
                fn=ev.out_of_range_category,
            ),
        ),
    ]:
        registry.register_definition(definition)
        registry.register_version(version)
    return registry


# ---------------------------------------------------------------------------
# AC1: four result types round-trip with their original type and value
# ---------------------------------------------------------------------------

def test_numeric_zero_is_a_real_zero():
    result = evaluate_frozen_item(
        manifest_for(freeze("intent_match")),
        output={"intent": "other"},
        expected_output={"expected_intent": "refund"},
    )
    typed = {r.evaluator_id: r for r in result.typed_results}
    zero = typed["intent_match"]
    assert zero.status == RESULT_STATUS_SUCCEEDED
    assert zero.result_type == "numeric"
    assert zero.value == 0.0
    assert zero.normalized_value == 0.0
    # The legacy projection carries the real zero, not a missing entry.
    assert result.scores == {"intent_match": 0.0}


def test_all_four_types_round_trip_through_evaluate_frozen_item():
    manifest = manifest_for(
        freeze("intent_match"),
        freeze("answer_present"),
        freeze("resolution_bucket"),
        freeze("answer_excerpt"),
    )
    result = evaluate_frozen_item(
        manifest,
        output={"intent": "other", "answer": "", "escalated": True},
        expected_output={"expected_intent": "refund"},
    )
    typed = {r.evaluator_id: r for r in result.typed_results}
    assert typed["intent_match"].value == 0.0
    assert typed["answer_present"].value is False
    assert typed["resolution_bucket"].value == "review"
    assert typed["answer_excerpt"].result_type == "text"
    assert isinstance(typed["answer_excerpt"].value, str)
    # Only numeric results enter the projection.
    assert set(result.scores) == {"intent_match"}


def test_typed_value_types_are_preserved_in_payloads():
    manifest = manifest_for(freeze("answer_present"), freeze("resolution_bucket"))
    result = evaluate_frozen_item(
        manifest, output={"answer": "ok", "escalated": False}, expected_output={}
    )
    payloads = {r.evaluator_id: r.to_payload() for r in result.typed_results}
    assert payloads["answer_present"]["value"] is True
    assert payloads["resolution_bucket"]["value"] == "resolved"


# ---------------------------------------------------------------------------
# AC2: 0 vs missing vs NaN vs Inf vs type mismatch vs failed vs skipped
# ---------------------------------------------------------------------------

def test_missing_value_is_no_result_not_zero():
    binding = freeze("intent_match")
    result = build_result(binding, None, raw_value_present=False)
    assert result.status == RESULT_STATUS_NO_RESULT
    assert result.value is None
    assert result.error_code == "EVALUATION_VALUE_MISSING"
    assert project_legacy_scores([result]) == {}


def test_nan_and_infinity_are_rejected_with_their_own_reason():
    binding = freeze("intent_match")
    for raw, code in (
        (float("nan"), "EVALUATION_VALUE_NOT_FINITE"),
        (float("inf"), "EVALUATION_VALUE_NOT_FINITE"),
        (float("-inf"), "EVALUATION_VALUE_NOT_FINITE"),
    ):
        result = build_result(binding, raw)
        assert result.status == RESULT_STATUS_FAILED
        assert result.error_code == code
        assert project_legacy_scores([result]) == {}


def test_type_mismatch_is_distinct_from_missing_and_non_finite():
    binding = freeze("intent_match")
    result = build_result(binding, "1.0")
    assert result.status == RESULT_STATUS_FAILED
    assert result.error_code == "EVALUATION_TYPE_MISMATCH"
    assert result.error_code != "EVALUATION_VALUE_MISSING"
    assert project_legacy_scores([result]) == {}


def test_boolean_is_never_silently_treated_as_a_number():
    binding = freeze("answer_present")
    ok = build_result(binding, True)
    assert ok.status == RESULT_STATUS_SUCCEEDED
    assert ok.value is True
    assert ok.normalized_value == 1.0
    # It is NOT in the numeric legacy projection.
    assert project_legacy_scores([ok]) == {}

    numeric_binding = freeze("intent_match")
    mismatch = build_result(numeric_binding, True)
    assert mismatch.status == RESULT_STATUS_FAILED
    assert mismatch.error_code == "EVALUATION_TYPE_MISMATCH"


def test_categorical_outside_the_frozen_enum_fails():
    registry = typed_registry()
    binding = freeze("out_of_range_category", registry=registry)
    result = build_result(binding, "nope")
    assert result.status == RESULT_STATUS_FAILED
    assert result.error_code == "EVALUATION_CATEGORY_NOT_ALLOWED"


def test_failure_and_skip_have_distinct_statuses():
    binding = freeze("intent_match")
    failed = failed_result(binding, error_code="EVALUATION_FAILED", error_message="boom")
    skipped = skipped_result(binding, reason="本轮不评测")
    assert failed.status == RESULT_STATUS_FAILED
    assert skipped.status == RESULT_STATUS_SKIPPED
    assert failed.status != skipped.status
    assert project_legacy_scores([failed, skipped]) == {}


def test_every_illegal_value_keeps_its_own_reason():
    binding = freeze("intent_match")
    codes = {
        build_result(binding, None, raw_value_present=False).error_code,
        build_result(binding, float("nan")).error_code,
        build_result(binding, "x").error_code,
        failed_result(binding).error_code,
        skipped_result(binding).error_code,
    }
    assert codes == {
        "EVALUATION_VALUE_MISSING",
        "EVALUATION_VALUE_NOT_FINITE",
        "EVALUATION_TYPE_MISMATCH",
        "EVALUATION_FAILED",
        "EVALUATION_SKIPPED",
    }


# ---------------------------------------------------------------------------
# AC3: provenance links each result to its frozen Binding and Manifest
# ---------------------------------------------------------------------------

def test_result_provenance_points_at_the_frozen_binding():
    binding = freeze("resolution_bucket")
    result = build_result(binding, "resolved", manifest_schema_version="1.2")
    prov = result.provenance
    assert prov.binding_id == binding.binding_id
    assert prov.evaluator_id == "resolution_bucket"
    assert prov.evaluator_version == binding.version
    assert prov.definition_digest == binding.definition_digest
    assert prov.executor_type == binding.executor_type
    assert prov.manifest_schema_version == "1.2"
    assert prov.contract_status == binding.contract_status


def test_forged_binding_payload_does_not_validate_as_a_release_measurement():
    """A binding whose digest does not match the registry must not produce a result."""
    payload = freeze("intent_match").to_payload()
    payload["definition_digest"] = "sha256:" + "0" * 64
    forged = EvaluatorBinding.from_payload(payload)
    assert forged is not None
    result = evaluate_frozen_item(
        {"schema_version": "1.2", "evaluators": [payload], "runner": IDENTITY},
        output={"intent": "refund"},
        expected_output={"expected_intent": "refund"},
    )
    # Fail closed: no measurement, no scores, UNKNOWN quality.
    assert result.typed_results == ()
    assert result.scores == {}
    assert result.eval_status == "failed"
    assert result.quality_conclusion == "unknown"
    # The failure is explained to the user instead of being scored as zero.
    assert result.eval_error


# ---------------------------------------------------------------------------
# AC4: text / unordered category never aggregate; normalized_value rules
# ---------------------------------------------------------------------------

def test_text_and_unordered_category_are_excluded_from_the_mean():
    text = build_result(freeze("answer_excerpt"), "some text")
    assert text.normalized_value is None

    registry = typed_registry()
    unordered = freeze("out_of_range_category", registry=registry)
    # An unordered category carries no normalization rule at all.
    unordered_ok = dataclasses.replace(unordered, normalization_rule=None)
    result = build_result(unordered_ok, "a")
    assert result.status == RESULT_STATUS_SUCCEEDED
    assert result.normalized_value is None

    numeric_zero = build_result(freeze("intent_match"), 0.0)
    means, counts = aggregate_numeric([text, result, numeric_zero])
    assert means == {"intent_match": 0.0}
    assert counts == {"intent_match": 1}


def test_ordered_category_normalization_uses_only_the_frozen_rule():
    binding = freeze("resolution_bucket")
    assert binding.normalization_rule["kind"] == "ordered_category"
    assert normalized_value_for("blocked", result_type="categorical", normalization_rule=binding.normalization_rule) == 0.0
    assert normalized_value_for("resolved", result_type="categorical", normalization_rule=binding.normalization_rule) == 2.0
    # Without a frozen rule nothing is numericized.
    assert normalized_value_for("resolved", result_type="categorical", normalization_rule=None) is None
    assert normalized_value_for("anything", result_type="text", normalization_rule=None) is None


def test_normalized_value_null_round_trips_and_stays_readable():
    binding = freeze("answer_excerpt")
    result = build_result(binding, "hello")
    assert result.normalized_value is None
    payload = result.to_payload()
    assert payload["normalized_value"] is None
    assert payload["value"] == "hello"


def test_run_aggregation_uses_numeric_only_and_reports_sample_counts():
    items = [
        {
            "dataset_item_id": "a",
            "execution_status": "succeeded",
            "eval_status": "succeeded",
            "quality_conclusion": "pass",
            "latency_ms": 1,
            "evaluation_results": [
                {"evaluator_id": "intent_match", "result_type": "numeric", "status": "succeeded", "value": 0.0},
                {"evaluator_id": "answer_present", "result_type": "boolean", "status": "succeeded", "value": False},
                {"evaluator_id": "resolution_bucket", "result_type": "categorical", "status": "succeeded", "value": "review"},
                {"evaluator_id": "answer_excerpt", "result_type": "text", "status": "succeeded", "value": "note"},
            ],
        }
    ]
    specs = [
        {"id": "intent_match", "scope": "item", "threshold": 1.0},
        {"id": "answer_present", "scope": "item", "threshold": 1.0},
        {"id": "resolution_bucket", "scope": "item", "threshold": 1.0},
        {"id": "answer_excerpt", "scope": "item", "threshold": 1.0},
    ]
    summary = aggregate_run(items, specs)
    # A real numeric 0 is aggregated as 0, with a valid sample count of 1.
    assert summary["score_means"]["intent_match"] == 0.0
    assert summary["score_counts"]["intent_match"] == 1
    # Text / category / boolean never contribute a fabricated 0.
    for non_numeric in ("answer_present", "resolution_bucket", "answer_excerpt"):
        assert summary["score_means"][non_numeric] is None
        assert summary["score_counts"][non_numeric] == 0


# ---------------------------------------------------------------------------
# AC5: partial failure keeps successes; empty dict never overwrites
# ---------------------------------------------------------------------------

def test_one_failing_binding_keeps_the_other_results():
    registry = typed_registry()
    manifest = manifest_for(
        freeze("intent_match", registry=registry),
        freeze("raising_evaluator", registry=registry),
        freeze("answer_present", registry=registry),
    )
    result = evaluate_frozen_item(
        manifest, output={"intent": "refund", "answer": "ok"}, expected_output={"expected_intent": "refund"}
    )
    typed = {r.evaluator_id: r for r in result.typed_results}
    assert typed["intent_match"].status == RESULT_STATUS_SUCCEEDED
    assert typed["intent_match"].value == 1.0
    assert typed["answer_present"].status == RESULT_STATUS_SUCCEEDED
    assert typed["answer_present"].value is True
    assert typed["raising_evaluator"].status == RESULT_STATUS_FAILED
    assert typed["raising_evaluator"].error_code == "EVALUATION_FAILED"
    # The failure is surfaced and the conclusion is fail-closed, not fabricated.
    assert result.eval_status == "failed"
    assert result.quality_conclusion == "unknown"
    # The numeric projection still keeps the successful numeric result.
    assert result.scores == {"intent_match": 1.0}


def test_contract_violation_does_not_discard_successful_siblings():
    registry = typed_registry()
    manifest = manifest_for(
        freeze("intent_match", registry=registry),
        freeze("non_finite_evaluator", registry=registry),
        freeze("type_mismatch_evaluator", registry=registry),
    )
    result = evaluate_frozen_item(
        manifest, output={"intent": "refund"}, expected_output={"expected_intent": "refund"}
    )
    typed = {r.evaluator_id: r for r in result.typed_results}
    assert typed["intent_match"].value == 1.0
    assert typed["non_finite_evaluator"].error_code == "EVALUATION_VALUE_NOT_FINITE"
    assert typed["type_mismatch_evaluator"].error_code == "EVALUATION_TYPE_MISMATCH"
    assert result.scores == {"intent_match": 1.0}


def test_persist_typed_results_is_idempotent_and_never_blanks_valid_rows(setup_runtime):
    db_mgr, *_ = setup_runtime
    registry = typed_registry()
    result = evaluate_frozen_item(
        manifest_for(freeze("intent_match", registry=registry)),
        output={"intent": "refund"},
        expected_output={"expected_intent": "refund"},
    )
    with db_mgr.get_session() as session:
        launch = Launch(
            id="l82",
            name="typed",
            status="COMPLETED",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            dataset_id="ds",
            dataset_name="ds",
            dataset_version="v1",
            manifest={},
        )
        session.add(launch)
        session.flush()
        item = Item(
            id="i82",
            launch_id="l82",
            dataset_item_id="case",
            execution_status="succeeded",
            eval_status="succeeded",
            quality_conclusion="pass",
        )
        session.add(item)
        session.flush()
        persist_typed_results(
            session, item_execution_id="i82", launch_id="l82", results=result.typed_results
        )
        session.commit()

    with db_mgr.get_session() as session:
        rows = load_typed_results(session, "i82")
        assert len(rows) == 1
        assert rows[0].evaluator_id == "intent_match"
        # Re-persisting an empty set must not delete the valid measurement.
        persist_typed_results(session, item_execution_id="i82", launch_id="l82", results=[])
        session.commit()
        assert len(load_typed_results(session, "i82")) == 1


def test_typed_value_keeps_its_json_type_in_the_database(setup_runtime):
    db_mgr, *_ = setup_runtime
    result = evaluate_frozen_item(
        manifest_for(freeze("answer_present"), freeze("resolution_bucket"), freeze("answer_excerpt")),
        output={"answer": "hello", "escalated": True},
        expected_output={},
    )
    with db_mgr.get_session() as session:
        launch = Launch(
            id="l82b",
            name="typed",
            status="COMPLETED",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            dataset_id="ds",
            dataset_name="ds",
            dataset_version="v1",
            manifest={},
        )
        session.add(launch)
        session.flush()
        item = Item(id="i82b", launch_id="l82b", dataset_item_id="case", execution_status="succeeded")
        session.add(item)
        session.flush()
        persist_typed_results(session, item_execution_id="i82b", launch_id="l82b", results=result.typed_results)
        session.commit()

    with db_mgr.get_session() as session:
        by_id = {r.evaluator_id: r for r in load_typed_results(session, "i82b")}
        assert by_id["answer_present"].value is True
        assert isinstance(by_id["answer_present"].value, bool)
        assert by_id["resolution_bucket"].value == "review"
        assert isinstance(by_id["answer_excerpt"].value, str)
        assert by_id["answer_excerpt"].normalized_value is None


# ---------------------------------------------------------------------------
# AC6: legacy numeric scores remain readable, never coerced
# ---------------------------------------------------------------------------

def test_legacy_numeric_scores_are_read_through_an_explicit_adapter():
    legacy = legacy_numeric_results({"intent_match": 1.0, "pii_safe": 0.0})
    assert [r.status for r in legacy] == [RESULT_STATUS_SUCCEEDED, RESULT_STATUS_SUCCEEDED]
    assert legacy[0].normalized_value == 1.0
    # Missing provenance is explicitly unknown, not fabricated.
    assert all(r.provenance.contract_status == "LEGACY_SCORES_ADAPTER" for r in legacy)
    assert all(r.error_code == "EVALUATION_PROVENANCE_UNKNOWN" for r in legacy)


def test_legacy_adapter_never_coerces_non_numeric_entries():
    legacy = legacy_numeric_results({"weird": "1.0", "flag": True})
    assert all(r.status == RESULT_STATUS_NO_RESULT for r in legacy)
    assert all(r.result_type == "unknown" for r in legacy)
    assert project_legacy_scores(legacy) == {}


def test_legacy_adapter_flags_non_finite_values():
    legacy = legacy_numeric_results({"bad": float("nan")})
    assert legacy[0].status == RESULT_STATUS_NO_RESULT
    assert legacy[0].error_code == "EVALUATION_VALUE_NOT_FINITE"


def test_historical_items_without_typed_results_still_aggregate():
    items = [
        {
            "dataset_item_id": "a",
            "execution_status": "succeeded",
            "eval_status": "succeeded",
            "quality_conclusion": "pass",
            "latency_ms": 1,
            "scores": {"intent_match": 1.0},
        }
    ]
    specs = [{"id": "intent_match", "scope": "item", "threshold": 1.0}]
    summary = aggregate_run(items, specs)
    assert summary["score_means"]["intent_match"] == 1.0
    assert summary["score_counts"]["intent_match"] == 1


# ---------------------------------------------------------------------------
# AC7: schema / contract behaviour of the coercion layer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,result_type,expected",
    [
        (1, "numeric", 1.0),
        (0, "numeric", 0.0),
        (True, "boolean", True),
        (False, "boolean", False),
        ("a", "categorical", "a"),
        ("note", "text", "note"),
    ],
)
def test_coerce_typed_value_preserves_valid_values(raw, result_type, expected):
    assert coerce_typed_value(raw, result_type=result_type) == expected


@pytest.mark.parametrize(
    "raw,result_type,code",
    [
        (None, "numeric", "EVALUATION_VALUE_MISSING"),
        ("1", "numeric", "EVALUATION_TYPE_MISMATCH"),
        (1, "boolean", "EVALUATION_TYPE_MISMATCH"),
        (1, "text", "EVALUATION_TYPE_MISMATCH"),
        ("z", "categorical", "EVALUATION_CATEGORY_NOT_ALLOWED"),
    ],
)
def test_coerce_typed_value_rejects_contract_violations(raw, result_type, code):
    with pytest.raises(ResultContractError) as exc:
        coerce_typed_value(raw, result_type=result_type, category_values=("a", "b"))
    assert exc.value.code == code


def test_binding_round_trips_the_typed_contract():
    binding = freeze("resolution_bucket")
    restored = EvaluatorBinding.from_payload(binding.to_payload())
    assert restored.category_values == binding.category_values
    assert restored.normalization_rule == binding.normalization_rule
    assert restored.binding_digest == binding.binding_digest


def test_normalization_rule_derivation_is_explicit():
    numeric = default_evaluator_registry.version("intent_match", "1.0.0")
    boolean = default_evaluator_registry.version("answer_present", "1.0.0")
    categorical = default_evaluator_registry.version("resolution_bucket", "1.0.0")
    text = default_evaluator_registry.version("answer_excerpt", "1.0.0")
    assert normalization_rule_for(numeric) is None
    assert normalization_rule_for(boolean)["kind"] == "boolean"
    assert normalization_rule_for(categorical)["kind"] == "ordered_category"
    assert normalization_rule_for(text) is None


# ---------------------------------------------------------------------------
# Worker integration: real worker + real DB, only the Agent HTTP is mocked
# ---------------------------------------------------------------------------

def _start_launch(setup_runtime, manifest, group):
    db_mgr, queue, _limiter, orchestrator, worker, _rec = setup_runtime
    launch = orchestrator.create_launch("test-agent", "v1", "ds", "v1", group, manifest)
    orchestrator.start_launch(launch.id)
    msgs = queue.read_group(group, count=1)
    return db_mgr, worker, launch.id, msgs[0]


def _run_worker(worker, msg, body):
    async def _run():
        with patch("app.worker.get_langfuse_client_safe", return_value=None):
            with patch(
                "app.worker.RemoteAgentExecutor.invoke_once", new_callable=AsyncMock
            ) as mock_invoke:
                mock_invoke.return_value = SingleInvocationResult(
                    status_code=200,
                    body=body,
                    raw_response="{}",
                    headers={},
                    duration_ms=3,
                    trace_context_received=True,
                    error_category=None,
                    error_message=None,
                    is_retryable=False,
                    may_have_side_effects=False,
                )
                return await worker.execute_item_message(*msg)

    return asyncio.run(_run())


def test_worker_persists_typed_results_for_a_real_item(setup_runtime):
    registry = typed_registry()
    manifest = manifest_for(
        freeze("intent_match", registry=registry),
        freeze("answer_present", registry=registry),
        freeze("resolution_bucket", registry=registry),
        freeze("answer_excerpt", registry=registry),
    )
    db_mgr, worker, _launch_id, msg = _start_launch(setup_runtime, manifest, "issue82-typed")
    assert _run_worker(worker, msg, {"intent": "other", "answer": "", "escalated": True}) is True

    with db_mgr.get_session() as session:
        item = session.get(Item, msg[1])
        assert item.execution_status.lower() == "succeeded"
        rows = {r.evaluator_id: r for r in load_typed_results(session, item.id)}
        assert set(rows) == {"intent_match", "answer_present", "resolution_bucket", "answer_excerpt"}
        assert rows["intent_match"].value == 0.0
        assert rows["answer_present"].value is False
        assert rows["resolution_bucket"].value == "review"
        assert isinstance(rows["answer_excerpt"].value, str)
        # The legacy projection is numeric-only.
        assert item.scores == {"intent_match": 0.0}


def test_worker_records_a_failure_without_dropping_successful_results(setup_runtime):
    registry = typed_registry()
    manifest = manifest_for(
        freeze("intent_match", registry=registry),
        freeze("raising_evaluator", registry=registry),
    )
    db_mgr, worker, _launch_id, msg = _start_launch(setup_runtime, manifest, "issue82-fail")
    assert _run_worker(worker, msg, {"intent": "refund"}) is True

    with db_mgr.get_session() as session:
        item = session.get(Item, msg[1])
        rows = {r.evaluator_id: r for r in load_typed_results(session, item.id)}
        assert rows["intent_match"].status == RESULT_STATUS_SUCCEEDED
        assert rows["raising_evaluator"].status == RESULT_STATUS_FAILED
        assert rows["raising_evaluator"].error_code == "EVALUATION_FAILED"
        assert item.quality_conclusion == "unknown"


# ---------------------------------------------------------------------------
# Snapshot round-trip: typed values survive into the immutable snapshot
# ---------------------------------------------------------------------------

def test_snapshot_preserves_typed_results(setup_runtime):
    import uuid
    from datetime import UTC, datetime

    from app.result_snapshots import create_result_snapshot

    db_mgr, *_ = setup_runtime
    registry = typed_registry()
    result = evaluate_frozen_item(
        manifest_for(
            freeze("intent_match", registry=registry),
            freeze("answer_present", registry=registry),
            freeze("resolution_bucket", registry=registry),
            freeze("answer_excerpt", registry=registry),
        ),
        output={"intent": "other", "answer": "", "escalated": True},
        expected_output={"expected_intent": "refund"},
    )

    launch_id = str(uuid.uuid4())
    manifest = manifest_for(freeze("intent_match", registry=registry))
    manifest["dataset"]["items"] = [
        {"id": "case-1", "input": {}, "expected_output": {"expected_intent": "refund"}, "metadata": {}}
    ]
    with db_mgr.get_session() as session:
        launch = Launch(
            id=launch_id,
            name="typed-snapshot",
            status="COMPLETED",
            quality_conclusion="fail",
            dataset_name="ds",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=manifest,
            completed_at=datetime.now(UTC),
        )
        session.add(launch)
        session.flush()
        item = Item(
            id=str(uuid.uuid4()),
            launch_id=launch_id,
            dataset_item_id="case-1",
            execution_status="succeeded",
            eval_status="succeeded",
            quality_conclusion="fail",
            scores={"intent_match": 0.0},
        )
        session.add(item)
        session.flush()
        persist_typed_results(
            session, item_execution_id=item.id, launch_id=launch_id, results=result.typed_results
        )
        session.commit()
        snapshot = create_result_snapshot(session, launch)
        assert snapshot is not None
        items = snapshot.items

    typed = {r["evaluator_id"]: r for r in items[0]["evaluation_results"]}
    assert typed["intent_match"]["value"] == 0.0
    assert typed["answer_present"]["value"] is False
    assert typed["resolution_bucket"]["value"] == "review"
    assert isinstance(typed["answer_excerpt"]["value"], str)
    # Every snapshot result carries its frozen provenance.
    assert typed["intent_match"]["provenance"]["binding_id"]
    assert typed["intent_match"]["provenance"]["manifest_schema_version"] == "1.2"
    # The legacy projection is present and numeric-only.
    assert items[0]["scores"] == {"intent_match": 0.0}


# ---------------------------------------------------------------------------
# API round-trip: the item endpoint exposes the typed results
# ---------------------------------------------------------------------------

def test_item_api_exposes_typed_results(setup_runtime, monkeypatch):
    import uuid

    from fastapi.testclient import TestClient

    from app import main

    db_mgr, *_ = setup_runtime
    registry = typed_registry()
    result = evaluate_frozen_item(
        manifest_for(
            freeze("intent_match", registry=registry),
            freeze("answer_present", registry=registry),
            freeze("resolution_bucket", registry=registry),
            freeze("answer_excerpt", registry=registry),
        ),
        output={"intent": "other", "answer": "", "escalated": True},
        expected_output={"expected_intent": "refund"},
    )

    launch_id = str(uuid.uuid4())
    with db_mgr.get_session() as session:
        launch = Launch(
            id=launch_id,
            name="typed-api",
            status="COMPLETED",
            quality_conclusion="fail",
            dataset_name="ds",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=manifest_for(freeze("intent_match", registry=registry)),
        )
        session.add(launch)
        session.flush()
        item = Item(
            id=str(uuid.uuid4()),
            launch_id=launch_id,
            dataset_item_id="case-1",
            execution_status="succeeded",
            eval_status="succeeded",
            quality_conclusion="fail",
            scores={"intent_match": 0.0},
        )
        session.add(item)
        session.flush()
        persist_typed_results(
            session, item_execution_id=item.id, launch_id=launch_id, results=result.typed_results
        )
        session.commit()
        item_id = item.id

    monkeypatch.setattr(main, "db_manager", db_mgr)
    client = TestClient(main.app)
    response = client.get(f"/api/v1/experiment-launches/{launch_id}/items")
    assert response.status_code == 200, response.text
    payload = response.json()[0]
    typed = {r["evaluator_id"]: r for r in payload["evaluation_results"]}

    assert typed["intent_match"]["result_type"] == "numeric"
    assert typed["intent_match"]["value"] == 0.0
    assert typed["intent_match"]["status"] == "succeeded"
    assert typed["answer_present"]["value"] is False
    assert typed["resolution_bucket"]["value"] == "review"
    assert isinstance(typed["answer_excerpt"]["value"], str)
    assert typed["answer_excerpt"]["normalized_value"] is None
    # Provenance travels with every result.
    assert typed["intent_match"]["provenance"]["binding_id"]
    assert typed["intent_match"]["provenance"]["contract_status"] == "FROZEN_VERIFIED"
    # The legacy projection stays numeric-only.
    assert payload["scores"] == {"intent_match": 0.0}
