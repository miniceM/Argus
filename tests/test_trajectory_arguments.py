"""T6 参数规则：精确比较、有限语法和可见性，不把合成证据计为来源准入。"""

from decimal import Decimal

import pytest
from app.evaluator_binding import EvaluatorBinding
from app.trajectory.codec import canonical_dumps, canonical_loads
from app.trajectory.rules import evaluate_trajectory_rule, parse_rule_params
from test_trajectory_manifest import context as _launch_context
from test_trajectory_rules import A, assert_measured, assert_unknown, context, partial, reseal, trajectory

launch_context = _launch_context

PARAMS = {"rule": "tool_argument_match", "tool_identity": A, "phase": "REQUESTED", "scope": "ROOT_SUBTREE",
    "workflow_scope_id": None, "path": "$.customer_id", "operator": "eq", "expected_json": '"synthetic-001"', "require_call": True}


def run(value, **changes):
    params = PARAMS | changes
    frozen = params | {"expected_json": canonical_dumps(canonical_loads(params["expected_json"]))}
    binding = EvaluatorBinding.from_payload({"id": "trajectory.tool_argument_match", "version": "1.0.0",
        "binding_id": "synthetic-arguments", "scope": "item", "result_type": "boolean",
        "definition_digest": "sha256:" + "a" * 64, "params": frozen,
        "input_contract": {"input_kind": "trajectory", "trajectory_schema_version": "1.0",
            "algorithm_version": "tool-arguments-1.0", "rule_id": "trajectory.tool_argument_match",
            "required_capabilities": ["tool_requests" if frozen["phase"] == "REQUESTED" else "tool_executions", "tool_arguments"]},
        "output_contract": {"type": "boolean"}})
    return evaluate_trajectory_rule(context(value), params, binding=binding, manifest_schema_version="1.3")


def argument(value, *, state="AVAILABLE", calls="COMPLETE"):
    def change(payload):
        payload["completeness"]["capabilities"]["tool_requests"] = calls
        for step in payload["steps"]:
            if step["step_id"] == "req-a":
                step["arguments_redacted"] = {"customer_id": value} if state == "AVAILABLE" else {}
                step["argument_visibility"] = {"$.customer_id": state} if state else {}
    return reseal(trajectory(), change)


@pytest.mark.parametrize("actual,expected,operator,result", [
    ("synthetic-001", '"synthetic-001"', "eq", True), ("other", '"synthetic-001"', "eq", False),
    ("5", "5", "eq", False), (True, "1", "eq", False), (1, "true", "ne", False),
    (None, "null", "eq", True), (None, '"null"', "eq", False),
    (9007199254740993, "9007199254740992", "gt", True),
    (Decimal("0.12345678901234567890123456789"), "0.12345678901234567890123456788", "gt", True),
    (Decimal("5.000"), "5", "eq", True), (5, "5.00", "lte", True),
    ("USD", '["USD","EUR"]', "one_of", True), ("5", "[5,6]", "one_of", False),
    ("abcde", '"bcd"', "contains", True), ([1, 2], "2", "contains", True),
])
def test_exact_json_comparison_does_not_coerce_types(actual, expected, operator, result):
    measured = run(argument(actual), expected_json=expected, operator=operator)
    assert_measured(measured, result)
    assert set(measured.evidence) == {"reason_code", "evidence_step_ids", "evidence_ref"}
    assert "expected_json" not in canonical_dumps(measured.evidence)


@pytest.mark.parametrize("state,expected", [("MISSING", False), ("REDACTED", None), ("INVALID", None), (None, None)])
def test_explicit_missing_is_a_violation_but_unknown_visibility_is_not(state, expected):
    measured = run(argument(None, state=state))
    assert_unknown(measured) if expected is None else assert_measured(measured, expected)


def test_no_calls_requires_coverage_and_the_frozen_require_call_switch():
    assert_measured(run(trajectory("zero")), False)
    assert_measured(run(trajectory("zero"), require_call=False), True)
    assert_unknown(run(partial(trajectory("zero"), tool_requests="PARTIAL")))


def test_partial_coverage_retains_known_mismatch_but_cannot_prove_all_calls_complied():
    assert_measured(run(argument("wrong", calls="PARTIAL")), False)
    assert_unknown(run(argument("synthetic-001", calls="PARTIAL")))


@pytest.mark.parametrize("path", ["$..id", "$.items[*]", "$.items[-1]", "$.items[1:2]", "$['id']", "$.x()", "$.items[?(@.id)]"])
def test_unsafe_jsonpath_grammar_is_rejected_with_a_finite_code(path):
    with pytest.raises(ValueError, match="TRAJECTORY_JSONPATH_INVALID"):
        parse_rule_params(PARAMS | {"path": path})


@pytest.mark.parametrize("operator", ["regex", "eval", "matches", "__import__"])
def test_unapproved_operator_has_a_stable_rejection(operator):
    with pytest.raises(ValueError, match="TRAJECTORY_RULE_OPERATOR_UNSUPPORTED"):
        parse_rule_params(PARAMS | {"operator": operator})


@pytest.mark.parametrize("path", ["$.Authorization", "$.Cookie", "$.card_number", "$.cardNumber", "$.id_card"])
def test_sensitive_targets_cannot_enter_frozen_rule_params(path):
    with pytest.raises(ValueError, match="TRAJECTORY_SENSITIVE_MATCH_FIELD"):
        parse_rule_params(PARAMS | {"path": path})


@pytest.mark.parametrize("literal", ['"4111111111111111"', '"Bearer synthetic-token"', '"11010519491231002X"'])
def test_sensitive_expected_literals_are_rejected_without_echoing_the_value(literal):
    with pytest.raises(ValueError, match="TRAJECTORY_SENSITIVE_MATCH_FIELD") as failure:
        parse_rule_params(PARAMS | {"expected_json": literal})
    assert literal not in str(failure.value)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", '{"x":1}', '[1,"1"]'])
def test_invalid_or_heterogeneous_literals_cannot_be_frozen(literal):
    with pytest.raises(ValueError):
        parse_rule_params(PARAMS | {"operator": "one_of", "expected_json": literal})


def test_observed_unknown_call_does_not_hide_a_later_argument_violation():
    def change(payload):
        for step in payload["steps"]:
            if step["step_id"] == "req-a":
                step.update(arguments_redacted={}, argument_visibility={"$.customer_id": "REDACTED"})
            elif step["step_id"] == "req-b":
                step.update(tool_identity=A, arguments_redacted={"customer_id": "wrong"}, argument_visibility={"$.customer_id": "AVAILABLE"})
        payload["completeness"]["capabilities"]["tool_arguments"] = "PARTIAL"
    assert_measured(run(reseal(trajectory(), change)), False)


def test_governed_source_normalization_masks_safe_alias_pii_before_artifact_and_digest():
    from test_trajectory_assembler import assemble, configured, prepared
    rows, contract, scope = prepared()
    contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {"content_policy_version": "pii-v1"})
    for row in rows:
        if row["id"] == "req-a":
            row["input"] = {"customer_id": "4111111111111111"}
    result = assemble(rows, contract, scope)
    assert "4111111111111111" not in canonical_dumps(result)
    request = next(step for step in result.steps if step.step_id == "req-a")
    assert request.argument_visibility["$.customer_id"] == "REDACTED"


def test_registered_argument_rule_freezes_canonical_literal_and_its_own_capabilities():
    from app.evaluator_binding import freeze_binding
    from app.evaluators import default_evaluator_registry as registry
    definition = registry.definition("trajectory.tool_argument_match")
    bound = freeze_binding(definition, registry.version(definition.id, "1.0.0"), runner_identity={"build_id": "synthetic"}, params=PARAMS)
    assert not definition.default_selected and bound.normalization_rule is None
    assert bound.input_contract["required_capabilities"] == ["tool_requests", "tool_arguments"]
    assert bound.input_contract["algorithm_version"] == "tool-arguments-1.0"


@pytest.mark.parametrize("mode", ["valid", "unapproved_path", "legacy_content"])
def test_formal_argument_binding_checks_the_frozen_redaction_contract(mode, request):
    from app.evaluator_binding import EvaluatorBindingError
    from test_trajectory_assembler import configured
    from test_trajectory_manifest import create
    base = request.getfixturevalue("launch_context")
    _, contract = trajectory(with_contract=True)
    if mode == "legacy_content":
        contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
            "content_policy_version": None, "string_policies": None})
    else:
        contract = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
            "content_policy_version": "pii-v1", "string_policies": {"$.customer_id": "identifier"}})
    configured_context = (*base[:3], contract)
    params = PARAMS | ({"path": "$.unapproved"} if mode == "unapproved_path" else {})
    if mode == "valid":
        launch = create(configured_context, evaluator_selections=[{"id": "trajectory.tool_argument_match", "version": "1.0.0", "params": params}])
        assert launch.manifest["evaluators"][0]["params"]["expected_json"] == '"synthetic-001"'
    else:
        from app.trajectory.manifest import TrajectoryManifestError
        error = TrajectoryManifestError if mode == "legacy_content" else EvaluatorBindingError
        code = "CONTENT_POLICY_REQUIRED" if mode == "legacy_content" else "TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED"
        with pytest.raises(error, match=code):
            create(configured_context, evaluator_selections=[{"id": "trajectory.tool_argument_match", "version": "1.0.0", "params": params}])


def governed_contract():
    from test_trajectory_assembler import configured
    _, contract = trajectory(with_contract=True)
    return configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {
        "content_policy_version": "pii-v1", "string_policies": {"$.customer_id": "identifier"}})


@pytest.mark.parametrize("path,code", [("$.unapproved", "TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED"),
    ("$..private", "TRAJECTORY_JSONPATH_INVALID"), ("$.Authorization", "TRAJECTORY_SENSITIVE_MATCH_FIELD")])
def test_resealed_manifest_still_cannot_bypass_argument_policy(launch_context, path, code):
    from copy import deepcopy

    from app.evaluator_binding import EvaluatorBindingError, manifest_measurement_digest, resolve_execution_plan
    from test_trajectory_manifest import create
    launched = create(launch_context, contract=governed_contract(), evaluator_selections=[
        {"id": "trajectory.tool_argument_match", "version": "1.0.0", "params": PARAMS}])
    manifest = deepcopy(launched.manifest)
    manifest["evaluators"][0]["params"]["path"] = path
    manifest["measurement_digest"] = manifest_measurement_digest(manifest)
    manifest["contract_digests"]["measurement"]["digest"] = manifest["measurement_digest"]
    with pytest.raises(EvaluatorBindingError, match=code):
        resolve_execution_plan(manifest)


@pytest.mark.parametrize("operator,literal,code", [("regex", '"safe"', "TRAJECTORY_RULE_OPERATOR_UNSUPPORTED"),
    ("eq", '"4111111111111111"', "TRAJECTORY_SENSITIVE_MATCH_FIELD")])
def test_freeze_rejects_invalid_matcher_with_finite_error_without_input(operator, literal, code):
    from app.evaluator_binding import EvaluatorBindingError, freeze_binding
    from app.evaluators import default_evaluator_registry as registry
    with pytest.raises(EvaluatorBindingError, match=code) as error:
        freeze_binding(registry.definition("trajectory.tool_argument_match"), registry.version("trajectory.tool_argument_match", "1.0.0"),
            runner_identity={"build_id": "synthetic"}, params=PARAMS | {"operator": operator, "expected_json": literal})
    assert literal not in str(error.value)


def test_redaction_and_quality_policy_changes_affect_only_their_own_frozen_identities(launch_context):
    from test_trajectory_assembler import configured
    from test_trajectory_manifest import create
    contract = governed_contract()
    selection = [{"id": "trajectory.tool_argument_match", "version": "1.0.0", "params": PARAMS}]
    first = create(launch_context, contract=contract, evaluator_selections=selection).manifest
    policy_only = create(launch_context, contract=contract, evaluator_selections=selection, quality_policy_rules=[{
        "evaluator_id": "trajectory.tool_argument_match", "result_type": "boolean", "operator": "==", "expected_value": False}]).manifest
    changed = configured(contract, redaction_policy=contract.redaction_policy.model_dump() | {"version": "governed-2"})
    redaction_only = create(launch_context, contract=changed, evaluator_selections=selection).manifest
    assert contract.tool_mapping.digest == changed.tool_mapping.digest
    assert contract.redaction_policy.digest != changed.redaction_policy.digest and contract.digest != changed.digest
    assert first["measurement_digest"] == policy_only["measurement_digest"] != redaction_only["measurement_digest"]
    assert first["quality_policy"]["policy_digest"] != policy_only["quality_policy"]["policy_digest"]
    assert first["trajectory"] == policy_only["trajectory"]


@pytest.mark.parametrize("actual,expected_status,expected_value", [("synthetic-001", "succeeded", True),
    ("wrong", "succeeded", False), ("4111111111111111", "no_result", None)])
def test_frozen_dispatcher_adapts_argument_results_without_literal_or_raw_arguments(launch_context, actual, expected_status, expected_value):
    from app.evaluator_binding import evaluate_frozen_item
    from test_trajectory_assembler import assemble, prepared
    from test_trajectory_manifest import create
    rows, _, scope = prepared()
    for row in rows:
        if row["id"] == "req-a":
            row["input"] = {"customer_id": actual}
    contract = governed_contract()
    evidence = assemble(rows, contract, scope)
    launched = create(launch_context, contract=contract, evaluator_selections=[
        {"id": "trajectory.tool_argument_match", "version": "1.0.0", "params": PARAMS}])
    result = evaluate_frozen_item(launched.manifest, output={}, expected_output={}, context=context(evidence))
    typed = result.typed_results[0]
    assert (typed.status, typed.value) == (expected_status, expected_value)
    assert set(typed.evidence) == {"reason_code", "evidence_step_ids", "evidence_ref"}
    assert actual not in canonical_dumps(typed.evidence) and "expected_json" not in canonical_dumps(typed.evidence)
    # 此处是合成 source/Ref 的 adapter 验证，不能冒充正式 Owned Loader 的资格验收。
    assert result.required_evidence_status != "COMPLETE"


def test_formal_freeze_cannot_put_free_text_in_match_literals_through_safe_alias(launch_context):
    from app.evaluator_binding import EvaluatorBindingError
    from test_trajectory_manifest import create
    with pytest.raises(EvaluatorBindingError, match="TRAJECTORY_ARGUMENT_POLICY_UNAPPROVED"):
        create(launch_context, contract=governed_contract(), evaluator_selections=[
            {"id": "trajectory.tool_argument_match", "version": "1.0.0", "params": PARAMS | {"expected_json": '"Alice Example"'}}])
