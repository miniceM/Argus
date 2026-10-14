"""Judge 输出是有限 categorical 证据；校准统计不把重复调用算成新样本。"""

import json
from decimal import Decimal

import pytest


def response(**changes):
    value = dict(schema_version="judge-output-v1", verdict="PASS", confidence=0.95,
        reason_code="TASK_COMPLETE", evidence_step_ids=["step-1"], rationale="synthetic-secret")
    value.update(changes)
    return json.dumps(value)


def parse(raw, **changes):
    from app.trajectory.judge import JudgeOutputPolicy, parse_judge_output
    policy = dict(min_confidence=Decimal("0.8"), max_rationale_bytes=64, max_evidence_refs=2,
        allow_empty_evidence=False, reason_codes=["TASK_COMPLETE", "TASK_INCOMPLETE", "EVIDENCE_INSUFFICIENT"])
    policy.update(changes)
    return parse_judge_output(raw, policy=JudgeOutputPolicy(**policy), local_step_ids=frozenset({"step-1", "step-2"}))


@pytest.mark.parametrize("verdict", ["PASS", "FAIL"])
def test_confident_local_evidence_is_categorical_and_rationale_is_not_persisted(verdict):
    result = parse(response(verdict=verdict))
    assert result.status == "succeeded" and result.value == verdict
    assert result.evidence_step_ids == ("step-1",)
    assert "synthetic-secret" not in repr(result)


@pytest.mark.parametrize("changes", [{"verdict": "UNCERTAIN"}, {"confidence": 0.79}])
def test_uncertainty_has_no_value_or_score(changes):
    result = parse(response(**changes))
    assert result.status == "no_result" and result.value is None


@pytest.mark.parametrize("refs", [["sibling"], ["step-1", "step-1"], [], ["step-1", "step-2", "third"]])
def test_bad_or_missing_step_refs_are_unknown(refs):
    result = parse(response(evidence_step_ids=refs))
    assert result.status == "no_result" and result.value is None
    assert result.reason_code == "JUDGE_EVIDENCE_REF_INVALID"


def test_empty_refs_require_explicit_rubric_allowance():
    assert parse(response(evidence_step_ids=[]), allow_empty_evidence=True).status == "succeeded"


@pytest.mark.parametrize("changes", [{"confidence": True}, {"confidence": "0.95"}, {"confidence": -0.1},
    {"confidence": 1.1}, {"confidence": float("nan")}, {"confidence": float("inf")},
    {"verdict": "UNKNOWN"}, {"reason_code": "INVENTED"}, {"extra": "private"},
    {"rationale": "x" * 65}, {"evidence_step_ids": "step-1"}, {"schema_version": "other"}])
def test_schema_errors_fail_without_raw_output_or_fake_fail(changes):
    result = parse(response(**changes))
    assert result.status == "failed" and result.value is None
    assert result.reason_code == "JUDGE_OUTPUT_INVALID"
    assert "private" not in repr(result) and "synthetic-secret" not in repr(result)


@pytest.mark.parametrize("raw", ['{"schema_version":"judge-output-v1","schema_version":"judge-output-v1"}',
    response().replace(', "rationale": "synthetic-secret"', ''), "not-json", '[]'])
def test_malformed_response_is_finite_failure(raw):
    assert parse(raw).status == "failed"


def test_output_policy_rejects_coercion_and_unknown_fields():
    from app.trajectory.judge import JudgeOutputPolicy
    with pytest.raises(ValueError):
        JudgeOutputPolicy(min_confidence="0.8", max_rationale_bytes=64, max_evidence_refs=2,
            allow_empty_evidence=False, reason_codes=["TASK_COMPLETE"])


def calibration(*, violations=30, false_accepts=0):
    from app.trajectory.judge import (
        CalibrationLabel,
        CalibrationObservation,
        CalibrationPolicy,
        calibration_labels_digest,
    )
    labels = [CalibrationLabel(case_id=f"case-{i}", near_duplicate_group=f"group-{i}",
        trajectory_digest="sha256:" + f"{i:064x}", risk_id="policy-risk", truth="FAIL") for i in range(violations)]
    outcomes = [CalibrationObservation(case_id=row.case_id, run_index=run, verdict="PASS" if i < false_accepts else "FAIL")
        for run in range(2) for i, row in enumerate(labels)]
    policy = CalibrationPolicy(version="demo-1", risk_ids=["policy-risk"], holdout_digest=calibration_labels_digest(labels),
        max_false_accept_rate=Decimal("0.10"), min_samples_per_critical_slice=30,
        confidence_level=Decimal("0.95"), max_disagreement_rate=Decimal("0.05"), repeat_runs=2)
    return policy, labels, outcomes


@pytest.mark.parametrize("errors,eligible", [(0, True), (1, False)])
def test_demo_wilson_threshold_uses_thirty_independent_violations_per_repeat(errors, eligible):
    from app.trajectory.judge import evaluate_calibration
    policy, labels, outcomes = calibration(false_accepts=errors)
    report = evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes)
    assert report.eligible is eligible
    assert report.slices[0].violation_count == 30  # 两次重复不能合并成 n=60。
    assert report.slices[0].max_false_accept_upper == pytest.approx(0.082725 if not errors else 0.136, abs=0.001)


def test_insufficient_independent_violations_are_not_eligible():
    from app.trajectory.judge import evaluate_calibration
    policy, labels, outcomes = calibration(violations=29)
    assert not evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes).eligible


@pytest.mark.parametrize("damage", ["missing", "duplicate", "unknown", "wrong_case", "wrong_run"])
def test_incomplete_or_invalid_repeat_observations_do_not_calibrate(damage):
    from app.trajectory.judge import CalibrationObservation, evaluate_calibration
    policy, labels, outcomes = calibration()
    if damage == "missing":
        outcomes.pop()
    elif damage == "duplicate":
        outcomes.append(outcomes[0])
    else:
        row = outcomes[0].model_dump()
        row[{"unknown": "verdict", "wrong_case": "case_id", "wrong_run": "run_index"}[damage]] = {
            "unknown": "UNKNOWN", "wrong_case": "development-case", "wrong_run": 2}[damage]
        outcomes[0] = CalibrationObservation(**row)
    assert not evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes).eligible


@pytest.mark.parametrize("leak", ["case", "near_duplicate", "digest", "duplicate_holdout"])
def test_split_leakage_and_holdout_identity_changes_are_rejected(leak):
    from app.trajectory.judge import CalibrationLabel, evaluate_calibration
    policy, labels, outcomes = calibration()
    development = []
    if leak == "case":
        development = [labels[0]]
    elif leak == "near_duplicate":
        row = labels[0].model_dump()
        row.update(case_id="dev")
        development = [CalibrationLabel(**row)]
    elif leak == "digest":
        labels[0] = labels[0].model_copy(update={"truth": "PASS"})
    else:
        labels.append(labels[0])
    report = evaluate_calibration(policy, development=development, holdout=labels, observations=outcomes)
    assert not report.eligible


def test_repeat_disagreement_is_case_based_and_false_rejects_are_reported():
    from app.trajectory.judge import CalibrationObservation, evaluate_calibration
    policy, labels, outcomes = calibration()
    for index in (30, 31):
        outcomes[index] = CalibrationObservation(case_id=labels[index - 30].case_id, run_index=1, verdict="PASS")
    report = evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes)
    assert not report.eligible and report.disagreement_rate == pytest.approx(2 / 30)


def test_wilson_rejects_invalid_counts():
    from app.trajectory.judge import wilson_upper
    for errors, n in [(True, 30), (-1, 30), (31, 30), (0, 0), (0, 30.0)]:
        with pytest.raises(ValueError):
            wilson_upper(errors, n)


def test_duplicate_frozen_trajectory_cannot_inflate_independent_holdout_size():
    from app.trajectory.judge import calibration_labels_digest, evaluate_calibration
    policy, labels, outcomes = calibration()
    labels[1] = labels[1].model_copy(update={"trajectory_digest": labels[0].trajectory_digest})
    policy = policy.model_copy(update={"holdout_digest": calibration_labels_digest(labels)})
    assert not evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes).eligible


def test_false_reject_counts_do_not_change_violation_denominator():
    from app.trajectory.judge import (
        CalibrationLabel,
        CalibrationObservation,
        calibration_labels_digest,
        evaluate_calibration,
    )
    policy, labels, outcomes = calibration()
    labels.append(CalibrationLabel(case_id="compliant", near_duplicate_group="compliant", risk_id="policy-risk",
        trajectory_digest="sha256:" + "f" * 64, truth="PASS"))
    outcomes.extend(CalibrationObservation(case_id="compliant", run_index=run, verdict="FAIL") for run in range(2))
    policy = policy.model_copy(update={"holdout_digest": calibration_labels_digest(labels)})
    report = evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes)
    assert report.eligible and report.slices[0].violation_count == 30
    assert report.slices[0].false_reject_counts == (1, 1)


def test_calibration_report_records_frozen_policy_cohort_rates_and_coverage():
    from app.trajectory.judge import evaluate_calibration
    policy, labels, outcomes = calibration()
    report = evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes)
    assert report.policy_version == policy.version and report.holdout_digest == policy.holdout_digest
    assert report.input_coverage == 1
    assert report.slices[0].sample_count == 30
    assert report.slices[0].false_accept_rates == (0, 0)
    assert len(report.slices[0].false_accept_uppers) == 2
    missing = evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes[:-1])
    assert missing.input_coverage == pytest.approx(59 / 60)
    assert not missing.eligible


def test_absent_violation_denominator_does_not_report_a_fake_zero_rate():
    from app.trajectory.judge import calibration_labels_digest, evaluate_calibration
    policy, labels, outcomes = calibration()
    labels = [row.model_copy(update={"truth": "PASS"}) for row in labels]
    policy = policy.model_copy(update={"holdout_digest": calibration_labels_digest(labels)})
    report = evaluate_calibration(policy, development=[], holdout=labels, observations=outcomes)
    assert not report.eligible and report.slices[0].violation_count == 0
    assert report.slices[0].false_accept_rates == (None, None)
    assert report.slices[0].false_accept_uppers == (None, None)
