"""Judge 默认策略使用冻结模式，诊断值不能变成必要发布证据。"""

from dataclasses import replace

import pytest
from app.quality_policy import (
    QualityPolicyError,
    QualityRule,
    default_quality_policy,
    evaluate_quality_policy,
    freeze_quality_policy,
)
from test_trajectory_judge_execution import registered


def numeric_binding():
    from app.evaluator_binding import freeze_binding
    from app.evaluators import EvaluatorRegistry
    from app.runner_identity import current_runner_identity

    registry = EvaluatorRegistry()
    return freeze_binding(registry.definition("intent_match"), registry.version("intent_match", "1.0.0"),
        runner_identity=current_runner_identity().model_dump())


@pytest.mark.parametrize("mode,required", [("diagnostic", False), ("release", True)])
def test_default_judge_rule_compares_pass_and_respects_frozen_mode(mode, required):
    _, _, judge = registered(mode=mode)
    policy = default_quality_policy([numeric_binding(), judge])
    rule = next(rule for rule in policy.rules if rule.evaluator_id == judge.evaluator_id)
    assert rule.operator == "==" and rule.expected_value == "PASS"
    assert rule.result_type == "categorical" and rule.required is required
    assert rule.critical is False
    assert policy.policy_id == "default-typed-required"
    assert "diagnostic Judge 为可选诊断" in policy.description
    assert "release Judge 为必要规则" in policy.description


def test_release_judge_alone_has_default_required_rule():
    _, _, judge = registered(mode="release")
    policy = default_quality_policy([judge])
    assert len(policy.rules) == 1 and policy.rules[0].required


def test_diagnostic_judge_alone_cannot_create_publishable_policy():
    _, _, judge = registered(mode="diagnostic")
    with pytest.raises(QualityPolicyError) as error:
        default_quality_policy([judge])
    assert error.value.code == "QUALITY_POLICY_NO_REQUIRED_RULE"


@pytest.mark.parametrize("mode,required,critical,code", [
    ("diagnostic", True, False, "JUDGE_DIAGNOSTIC_RULE_REQUIRED"),
    ("diagnostic", False, True, "CRITICAL_JUDGE_SINGLE_SOURCE"),
    ("release", True, True, "CRITICAL_JUDGE_SINGLE_SOURCE"),
])
def test_explicit_judge_rule_cannot_bypass_mode_or_critical_admission(mode, required, critical, code):
    _, _, judge = registered(mode=mode)
    numeric = numeric_binding()
    with pytest.raises(QualityPolicyError) as error:
        freeze_quality_policy(policy_id="judge-policy", version="1.0",
            bindings={judge.evaluator_id: judge, numeric.evaluator_id: numeric},
            rules=[
                QualityRule(evaluator_id=numeric.evaluator_id, result_type="numeric", operator=">=", threshold=1),
                QualityRule(evaluator_id=judge.evaluator_id, result_type="categorical", operator="==",
                    expected_value="PASS", required=required, critical=critical),
            ])
    assert error.value.code == code
    assert error.value.evaluator_id == judge.evaluator_id


def test_default_policy_rejects_changed_frozen_binding():
    _, _, judge = registered(mode="release")
    judge = replace(judge, binding_id="bind_changed")
    with pytest.raises(QualityPolicyError) as error:
        default_quality_policy([numeric_binding(), judge])
    assert error.value.code == "JUDGE_BINDING_INVALID"


@pytest.mark.parametrize("mode", ["diagnostic", "release"])
@pytest.mark.parametrize("status,value,judge_conclusion", [
    ("succeeded", "PASS", "pass"),
    ("succeeded", "FAIL", "fail"),
    ("failed", None, "unknown"),
    ("no_result", None, "unknown"),
    ("skipped", None, "unknown"),
])
def test_judge_default_policy_retains_optional_diagnostics_and_blocks_required_unknown(mode, status, value, judge_conclusion):
    _, _, judge = registered(mode=mode)
    numeric = numeric_binding()
    policy = default_quality_policy([numeric, judge])
    decision = evaluate_quality_policy({
        numeric.evaluator_id: {"status": "succeeded", "value": 1.0},
        judge.evaluator_id: {"status": status, "value": value},
    }, policy)
    assert decision.rule_for(judge.evaluator_id).conclusion == judge_conclusion
    assert decision.conclusion == ("pass" if mode == "diagnostic" else judge_conclusion)
    # 判定通过不代替 Evaluator 的正式发布资格。
    assert judge.release_eligible is False
