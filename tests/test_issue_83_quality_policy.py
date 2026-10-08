"""Issue #83 — independent, frozen QualityPolicy with an explicit UNKNOWN semantics.

Covers the acceptance criteria of Argus Issue #83:
* a Launch without ``overall_pass`` still gets a complete, independent policy
  decision, and the four diagnostic metrics keep the Demo v1=2/6, v2=6/6 result;
* the same measurements under a lax vs. a strict policy reach different
  quality conclusions, and changing only threshold/critical/operator moves the
  *policy* digest while leaving the *measurement* digest untouched;
* numeric lower-is-better is judged with ``<=``; boolean / categorical are only
  judged by an explicit typed match; illegal rules are rejected before a Launch
  can be created;
* complete evidence yields correct PASS/FAIL, while missing / failed / timed out /
  required-skipped evidence yields UNKNOWN with a visible reason that never
  masquerades as a quality FAIL;
* a 4 PASS / 1 FAIL / 1 UNKNOWN cohort reports an 80% decided pass rate and 5/6
  coverage; an all-UNKNOWN cohort reports no pass rate at all;
* the run-scope derived metric cannot be submitted as an Item Evaluator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "services" / "eval-runner") not in sys.path:
    sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.aggregation import aggregate_run  # noqa: E402
from app.evaluator_binding import (  # noqa: E402
    EvaluatorBinding,
    freeze_binding,
    manifest_measurement_digest,
    manifest_quality_policy,
)
from app.evaluators import default_evaluator_registry  # noqa: E402
from app.quality_policy import (  # noqa: E402
    QUALITY_CONCLUSION_FAIL,
    QUALITY_CONCLUSION_PASS,
    QUALITY_CONCLUSION_UNKNOWN,
    QualityPolicy,
    QualityPolicyError,
    QualityRule,
    default_quality_policy,
    evaluate_quality_policy,
    freeze_quality_policy,
)

DIAGNOSTIC_IDS = ("escalation_match", "intent_match", "pii_safe", "required_tool_match")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _binding(evaluator_id: str, version: str = "1.0.0") -> EvaluatorBinding:
    definition = default_evaluator_registry.definition(evaluator_id)
    resolved = default_evaluator_registry.version(evaluator_id, version)
    return freeze_binding(
        definition,
        resolved,
        runner_identity={"build_id": "test-build-001", "runner_version": "0.1.0"},
    )


def _numeric_binding() -> EvaluatorBinding:
    return _binding("intent_match")


def _lower_is_better_binding() -> EvaluatorBinding:
    """A numeric binding whose frozen direction is lower-is-better."""
    base = _numeric_binding()
    return EvaluatorBinding(**{**base.__dict__, "direction": "lower_is_better", "threshold": 0.2})


def _policy(
    rules: tuple[QualityRule, ...],
    *,
    policy_id: str = "policy-1",
    version: str = "1.0.0",
) -> QualityPolicy:
    return freeze_quality_policy(
        policy_id=policy_id,
        version=version,
        rules=rules,
        bindings={rule.evaluator_id: _numeric_binding() for rule in rules},
    )


def _result(
    evaluator_id: str,
    value: Any,
    *,
    result_type: str = "numeric",
    status: str = "succeeded",
    error_code: str | None = None,
) -> dict[str, Any]:
    return {
        "evaluator_id": evaluator_id,
        "evaluator_version": "1.0.0",
        "result_type": result_type,
        "status": status,
        "value": value,
        "error_code": error_code,
    }


# ---------------------------------------------------------------------------
# AC1 — an independent policy judges a Launch that never selected overall_pass
# ---------------------------------------------------------------------------

def test_default_policy_is_independent_of_the_composite_evaluator():
    policy = default_quality_policy([_binding(eid) for eid in DIAGNOSTIC_IDS])
    assert {rule.evaluator_id for rule in policy.rules} == set(DIAGNOSTIC_IDS)
    assert "overall_pass" not in {rule.evaluator_id for rule in policy.rules}
    assert all(rule.required for rule in policy.rules)


def test_default_policy_marks_every_diagnostic_required_and_operator_aware():
    policy = default_quality_policy([_binding(eid) for eid in DIAGNOSTIC_IDS])
    for rule in policy.rules:
        assert rule.required is True
        assert rule.operator in {">=", "<="}
        assert rule.threshold == pytest.approx(1.0)


def test_demo_pass_rate_is_unchanged_without_overall_pass():
    """v1 keeps 2/6 and v2 keeps 6/6 when the policy replaces the composite."""
    policy = default_quality_policy([_binding(eid) for eid in DIAGNOSTIC_IDS])
    v1 = [
        {eid: _result(eid, 1.0 if passed else 0.0) for eid in DIAGNOSTIC_IDS}
        for passed in (True, False, True, False, False, False)
    ]
    decisions = [evaluate_quality_policy(results, policy) for results in v1]
    assert sum(d.conclusion == QUALITY_CONCLUSION_PASS for d in decisions) == 2
    assert sum(d.conclusion == QUALITY_CONCLUSION_FAIL for d in decisions) == 4

    v2 = [{eid: _result(eid, 1.0) for eid in DIAGNOSTIC_IDS} for _ in range(6)]
    decisions_v2 = [evaluate_quality_policy(results, policy) for results in v2]
    assert sum(d.conclusion == QUALITY_CONCLUSION_PASS for d in decisions_v2) == 6


def test_launch_without_overall_pass_gets_a_complete_decision():
    policy = default_quality_policy([_binding(eid) for eid in DIAGNOSTIC_IDS])
    results = {eid: _result(eid, 1.0) for eid in DIAGNOSTIC_IDS}
    decision = evaluate_quality_policy(results, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_PASS
    assert decision.decided_by == "QUALITY_POLICY"
    assert len(decision.rule_evaluations) == 4


# ---------------------------------------------------------------------------
# AC2 — different policies, different conclusions; separate digests
# ---------------------------------------------------------------------------

def test_same_measurements_reach_different_conclusions_under_lax_and_strict_policy():
    lax = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5),))
    strict = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.9),))
    results = {"intent_match": _result("intent_match", 0.7)}
    assert evaluate_quality_policy(results, lax).conclusion == QUALITY_CONCLUSION_PASS
    assert evaluate_quality_policy(results, strict).conclusion == QUALITY_CONCLUSION_FAIL


def test_policy_digest_changes_while_measurement_digest_stays_stable():
    binding = _numeric_binding()
    lenient = freeze_quality_policy(
        policy_id="policy-1",
        version="1.0.0",
        rules=(QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5),),
        bindings={"intent_match": binding},
    )
    strict = freeze_quality_policy(
        policy_id="policy-1",
        version="1.0.0",
        rules=(QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.9),),
        bindings={"intent_match": binding},
    )
    assert lenient.policy_digest != strict.policy_digest

    manifest_a = {"evaluators": [binding.to_payload()], "quality_policy": lenient.to_payload()}
    manifest_b = {"evaluators": [binding.to_payload()], "quality_policy": strict.to_payload()}
    assert manifest_measurement_digest(manifest_a) == manifest_measurement_digest(manifest_b)


def test_critical_flag_and_operator_also_only_move_the_policy_digest():
    binding = _numeric_binding()
    plain = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5),),
        bindings={"intent_match": binding},
    )
    critical = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(
            QualityRule(
                evaluator_id="intent_match", operator=">=", threshold=0.5, critical=True
            ),
        ),
        bindings={"intent_match": binding},
    )
    assert plain.policy_digest != critical.policy_digest
    assert (
        manifest_measurement_digest(
            {"evaluators": [binding.to_payload()], "quality_policy": plain.to_payload()}
        )
        == manifest_measurement_digest(
            {"evaluators": [binding.to_payload()], "quality_policy": critical.to_payload()}
        )
    )


# ---------------------------------------------------------------------------
# AC3 — typed operators, and illegal rules rejected at creation
# ---------------------------------------------------------------------------

def test_lower_is_better_numeric_is_judged_with_lte():
    binding = _lower_is_better_binding()
    policy = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(QualityRule(evaluator_id="intent_match", operator="<=", threshold=0.2),),
        bindings={"intent_match": binding},
    )
    assert (
        evaluate_quality_policy({"intent_match": _result("intent_match", 0.1)}, policy).conclusion
        == QUALITY_CONCLUSION_PASS
    )
    assert (
        evaluate_quality_policy({"intent_match": _result("intent_match", 0.4)}, policy).conclusion
        == QUALITY_CONCLUSION_FAIL
    )


def test_boolean_rule_requires_an_explicit_expected_value():
    binding = _binding("answer_present")
    policy = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(
            QualityRule(
                evaluator_id="answer_present",
                operator="==",
                expected_value=False,
                result_type="boolean",
            ),
        ),
        bindings={"answer_present": binding},
    )
    results = {"answer_present": _result("answer_present", False, result_type="boolean")}
    decision = evaluate_quality_policy(results, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_PASS
    assert decision.rule_evaluations[0].observed_value is False


def test_boolean_true_is_never_silently_equal_to_one():
    binding = _binding("answer_present")
    policy = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(
            QualityRule(
                evaluator_id="answer_present",
                operator="==",
                expected_value=True,
                result_type="boolean",
            ),
        ),
        bindings={"answer_present": binding},
    )
    results = {"answer_present": _result("answer_present", False, result_type="boolean")}
    decision = evaluate_quality_policy(results, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_FAIL
    assert "1" not in str(decision.rule_evaluations[0].explanation)


def test_categorical_rule_uses_an_explicit_matching_value():
    binding = _binding("resolution_bucket")
    policy = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(
            QualityRule(
                evaluator_id="resolution_bucket",
                operator="==",
                expected_value="resolved",
                result_type="categorical",
            ),
        ),
        bindings={"resolution_bucket": binding},
    )
    assert (
        evaluate_quality_policy(
            {"resolution_bucket": _result("resolution_bucket", "resolved", result_type="categorical")},
            policy,
        ).conclusion
        == QUALITY_CONCLUSION_PASS
    )
    assert (
        evaluate_quality_policy(
            {"resolution_bucket": _result("resolution_bucket", "review", result_type="categorical")},
            policy,
        ).conclusion
        == QUALITY_CONCLUSION_FAIL
    )


def test_text_result_is_evidence_only_and_cannot_become_a_required_rule():
    binding = _binding("answer_excerpt")
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(
                QualityRule(
                    evaluator_id="answer_excerpt",
                    operator="==",
                    expected_value="说明",
                    result_type="text",
                    required=True,
                ),
            ),
            bindings={"answer_excerpt": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_TYPE_UNSUPPORTED"


def test_unknown_operator_is_rejected_at_creation():
    binding = _numeric_binding()
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(QualityRule(evaluator_id="intent_match", operator="~=", threshold=0.5),),
            bindings={"intent_match": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_OPERATOR_UNKNOWN"


def test_operator_and_result_type_mismatch_is_rejected():
    binding = _binding("answer_present")
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(
                QualityRule(
                    evaluator_id="answer_present",
                    operator=">=",
                    threshold=0.5,
                    result_type="boolean",
                ),
            ),
            bindings={"answer_present": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_OPERATOR_TYPE_MISMATCH"


def test_rule_referencing_an_unselected_evaluator_is_rejected():
    binding = _numeric_binding()
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(QualityRule(evaluator_id="not_selected", operator=">=", threshold=0.5),),
            bindings={"intent_match": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_EVALUATOR_UNKNOWN"


def test_non_finite_threshold_is_rejected():
    binding = _numeric_binding()
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(QualityRule(evaluator_id="intent_match", operator=">=", threshold=float("inf")),),
            bindings={"intent_match": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_THRESHOLD_INVALID"


def test_categorical_expected_value_outside_the_frozen_enum_is_rejected():
    binding = _binding("resolution_bucket")
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(
                QualityRule(
                    evaluator_id="resolution_bucket",
                    operator="==",
                    expected_value="not-a-bucket",
                    result_type="categorical",
                ),
            ),
            bindings={"resolution_bucket": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_CATEGORY_NOT_ALLOWED"


def test_duplicate_rule_for_one_evaluator_is_rejected():
    binding = _numeric_binding()
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(
                QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5),
                QualityRule(evaluator_id="intent_match", operator="<=", threshold=0.9),
            ),
            bindings={"intent_match": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_RULE_DUPLICATE"


def test_policy_without_any_required_rule_is_rejected():
    binding = _numeric_binding()
    with pytest.raises(QualityPolicyError) as excinfo:
        freeze_quality_policy(
            policy_id="p",
            version="1.0.0",
            rules=(
                QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5, required=False),
            ),
            bindings={"intent_match": binding},
        )
    assert excinfo.value.code == "QUALITY_POLICY_NO_REQUIRED_RULE"


# ---------------------------------------------------------------------------
# AC4 — PASS / FAIL / UNKNOWN truth table
# ---------------------------------------------------------------------------

def test_complete_satisfied_evidence_is_pass():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),))
    decision = evaluate_quality_policy({"intent_match": _result("intent_match", 1.0)}, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_PASS
    assert not decision.unknown_reasons


def test_complete_violated_evidence_is_fail():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),))
    decision = evaluate_quality_policy({"intent_match": _result("intent_match", 0.2)}, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_FAIL
    assert decision.rule_evaluations[0].conclusion == QUALITY_CONCLUSION_FAIL
    assert decision.rule_evaluations[0].explanation


def test_missing_required_evidence_is_unknown_not_fail():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),))
    decision = evaluate_quality_policy({}, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.unknown_reasons
    assert "FAIL" not in decision.unknown_reasons[0]


def test_failed_required_evidence_is_unknown_with_its_reason():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),))
    decision = evaluate_quality_policy(
        {
            "intent_match": _result(
                "intent_match", None, status="failed", error_code="EVALUATION_FAILED"
            )
        },
        policy,
    )
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.rule_evaluations[0].conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.unknown_reasons


def test_no_result_and_skipped_required_evidence_are_unknown():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),))
    for status, code in (
        ("no_result", "EVALUATION_VALUE_MISSING"),
        ("skipped", "EVALUATION_SKIPPED"),
    ):
        decision = evaluate_quality_policy(
            {
                "intent_match": _result(
                    "intent_match", None, status=status, error_code=code
                )
            },
            policy,
        )
        assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN, status


def test_a_real_zero_is_judged_as_a_measurement_not_as_missing_evidence():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),))
    decision = evaluate_quality_policy({"intent_match": _result("intent_match", 0)}, policy)
    assert decision.conclusion == QUALITY_CONCLUSION_FAIL
    assert decision.rule_evaluations[0].observed_value == 0


def test_known_violation_stays_visible_while_another_rule_is_unknown():
    policy = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(
            QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),
            QualityRule(evaluator_id="pii_safe", operator=">=", threshold=0.8),
        ),
        bindings={"intent_match": _numeric_binding(), "pii_safe": _binding("pii_safe")},
    )
    decision = evaluate_quality_policy(
        {
            "intent_match": _result("intent_match", 0.1),
            "pii_safe": _result("pii_safe", None, status="failed", error_code="EVALUATION_FAILED"),
        },
        policy,
    )
    # Insufficient evidence wins the overall conclusion...
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    # ...but the known violation is still reported per rule.
    by_id = {rule.evaluator_id: rule for rule in decision.rule_evaluations}
    assert by_id["intent_match"].conclusion == QUALITY_CONCLUSION_FAIL
    assert by_id["pii_safe"].conclusion == QUALITY_CONCLUSION_UNKNOWN


def test_optional_diagnostic_failure_does_not_change_the_quality_conclusion():
    policy = freeze_quality_policy(
        policy_id="p",
        version="1.0.0",
        rules=(
            QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.8),
            QualityRule(
                evaluator_id="pii_safe", operator=">=", threshold=0.8, required=False
            ),
        ),
        bindings={"intent_match": _numeric_binding(), "pii_safe": _binding("pii_safe")},
    )
    decision = evaluate_quality_policy(
        {
            "intent_match": _result("intent_match", 1.0),
            "pii_safe": _result("pii_safe", None, status="failed", error_code="EVALUATION_FAILED"),
        },
        policy,
    )
    assert decision.conclusion == QUALITY_CONCLUSION_PASS
    # The evaluation anomaly is still surfaced, it just does not decide quality.
    by_id = {rule.evaluator_id: rule for rule in decision.rule_evaluations}
    assert by_id["pii_safe"].conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert by_id["pii_safe"].explanation


def test_required_rule_without_any_policy_is_unknown():
    """No policy at all can never yield a quality conclusion (fail closed)."""
    decision = evaluate_quality_policy(
        {"intent_match": _result("intent_match", 1.0)}, None
    )
    assert decision.conclusion == QUALITY_CONCLUSION_UNKNOWN
    assert decision.decided_by == "NO_FROZEN_POLICY"


# ---------------------------------------------------------------------------
# AC5 — cohort denominators
# ---------------------------------------------------------------------------

def _cohort(conclusions: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "dataset_item_id": f"case-{index}",
            "execution_status": "succeeded",
            "eval_status": "succeeded" if conclusion != QUALITY_CONCLUSION_UNKNOWN else "failed",
            "quality_conclusion": conclusion,
            "evaluation_results": [_result("intent_match", 1.0)],
        }
        for index, conclusion in enumerate(conclusions)
    ]


def test_four_pass_one_fail_one_unknown_reports_80_percent_and_five_sixths():
    summary = aggregate_run(
        _cohort(
            [QUALITY_CONCLUSION_PASS] * 4
            + [QUALITY_CONCLUSION_FAIL, QUALITY_CONCLUSION_UNKNOWN]
        ),
        [{"id": "intent_match", "version": "1.0.0", "scope": "item", "result_type": "numeric"}],
    )
    assert summary["quality_pass_count"] == 4
    assert summary["quality_fail_count"] == 1
    assert summary["quality_unknown_count"] == 1
    assert summary["decided_pass_rate"] == pytest.approx(0.8)
    assert summary["decision_coverage"] == pytest.approx(5 / 6)


def test_all_unknown_cohort_reports_no_pass_rate():
    summary = aggregate_run(
        _cohort([QUALITY_CONCLUSION_UNKNOWN] * 6),
        [{"id": "intent_match", "version": "1.0.0", "scope": "item", "result_type": "numeric"}],
    )
    assert summary["decided_pass_rate"] is None
    assert summary["decision_coverage"] == pytest.approx(0.0)
    assert summary["quality_unknown_count"] == 6


def test_empty_cohort_reports_null_denominators():
    summary = aggregate_run(
        [],
        [{"id": "intent_match", "version": "1.0.0", "scope": "item", "result_type": "numeric"}],
    )
    assert summary["decided_pass_rate"] is None
    assert summary["decision_coverage"] is None


def test_legacy_pass_rate_keys_remain_available():
    """The historical keys keep working so old consumers do not break."""
    summary = aggregate_run(
        _cohort([QUALITY_CONCLUSION_PASS, QUALITY_CONCLUSION_FAIL]),
        [{"id": "intent_match", "version": "1.0.0", "scope": "item", "result_type": "numeric"}],
    )
    assert summary["pass_rate"] == pytest.approx(0.5)
    assert summary["evaluation_coverage"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# AC6 — frozen policy plumbing and run-scope rejection
# ---------------------------------------------------------------------------

def test_manifest_quality_policy_reads_the_frozen_policy():
    policy = _policy((QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5),))
    manifest = {"evaluators": [_numeric_binding().to_payload()], "quality_policy": policy.to_payload()}
    restored = manifest_quality_policy(manifest)
    assert restored is not None
    assert restored.policy_digest == policy.policy_digest
    assert restored.rules[0].threshold == pytest.approx(0.5)


def test_manifest_quality_policy_reports_none_for_a_legacy_manifest():
    assert manifest_quality_policy({"evaluators": [], "quality_policy": {"mode": "legacy"}}) is None
    assert manifest_quality_policy({}) is None


def test_policy_round_trips_through_its_payload():
    policy = _policy(
        (
            QualityRule(evaluator_id="intent_match", operator=">=", threshold=0.5, critical=True),
        )
    )
    restored = QualityPolicy.from_payload(policy.to_payload())
    assert restored.policy_digest == policy.policy_digest
    assert restored.rules[0].critical is True


def test_run_scope_derived_metric_cannot_be_an_item_evaluator():
    from app.evaluators import EvaluatorSelectionError

    assert default_evaluator_registry.version("run_pass_rate", "1.0.0").scope == "run"
    with pytest.raises(EvaluatorSelectionError):
        default_evaluator_registry.resolve_for_release("run_pass_rate", "1.0.0", required_scope="item")
