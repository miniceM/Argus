from decimal import Decimal

import pytest
from app.trajectory.contract import RedactionPolicy, TrajectoryLimits
from app.trajectory.redaction import project_json
from pydantic import BaseModel


def test_allowlisted_nested_scalar_projection_preserves_exact_numbers_and_visibility():
    result = project_json(
        '{"customer":{"id":9007199254740993.00000000000000001,"token":"synthetic-secret"},"other":3}',
        ["$.customer.id", "$.customer.missing"],
        limits=TrajectoryLimits(),
    )
    assert result.value == {"customer": {"id": Decimal("9007199254740993.00000000000000001")}}
    assert result.visibility == {"$.customer.id": "AVAILABLE", "$.customer.missing": "MISSING"}
    assert result.complete is True


@pytest.mark.parametrize("value", [None])
def test_absent_entire_arguments_does_not_prove_missing_fields(value):
    result = project_json(value, ["$.customer_id"], limits=TrajectoryLimits())
    assert result.value is None and result.visibility == {} and not result.complete


def test_captured_json_null_is_invalid_shape_instead_of_uncollected_arguments():
    result = project_json("null", ["$.customer_id"], limits=TrajectoryLimits())
    assert result.value == {} and result.visibility == {"$.customer_id": "INVALID"}
    assert not result.complete and result.warnings == ["ARGUMENTS_INVALID"]


@pytest.mark.parametrize(
    "value", ["{broken", '{"id":1,"id":2}', '{"id":NaN}', {"id": 1.5}, '{"id":{"token":"synthetic-secret"}}']
)
def test_invalid_json_float_and_selected_container_never_return_raw_values(value):
    result = project_json(value, ["$.id"], limits=TrajectoryLimits())
    assert result.value == {} and result.visibility == {"$.id": "INVALID"}
    assert not result.complete


def test_hidden_ancestor_overrides_available_nested_value():
    result = project_json(
        {"customer": {"id": "synthetic-secret"}},
        ["$.customer.id"],
        limits=TrajectoryLimits(),
        hidden_paths=["$.customer"],
    )
    assert result.value == {} and result.visibility == {"$.customer.id": "REDACTED"}


@pytest.mark.parametrize("paths,expected,warnings", [
    (["$.customer.id"], {"$.customer.id": "REDACTED"}, []),
    (["$.customer.id", "$.other"], {"$.customer.id": "REDACTED"}, ["ARGUMENTS_NOT_COLLECTED"]),
    (["$.other"], {}, ["ARGUMENTS_NOT_COLLECTED"]),
])
def test_explicit_hidden_paths_survive_absent_payload_without_inventing_other_fields(paths, expected, warnings):
    result = project_json(None, paths, limits=TrajectoryLimits(), hidden_paths=["$.customer"],
                          source_visibility="REDACTED")
    assert result.visibility == expected and result.warnings == warnings and not result.complete
    assert result.value == ({} if expected else None)


@pytest.mark.parametrize("value", [None, {"customer_id": "synthetic-secret"}])
def test_explicit_uncollected_marker_is_not_invalid_or_available_parameter_evidence(value):
    result = project_json(value, ["$.customer_id"], limits=TrajectoryLimits(), source_visibility="UNCOLLECTED")
    assert result.value is None and result.visibility == {} and not result.complete
    assert result.warnings == ["ARGUMENTS_NOT_COLLECTED"]


@pytest.mark.parametrize(
    "limits,value",
    [
        ({"max_string_bytes": 3}, '{"id":"long"}'),
        ({"max_numeric_digits": 2}, '{"id":123}'),
        ({"max_json_depth": 1}, '{"id":{"value":1}}'),
    ],
)
def test_smaller_contract_json_budgets_apply_before_projection(limits, value):
    result = project_json(value, ["$.id"], limits=TrajectoryLimits.model_validate(limits))
    assert result.visibility == {"$.id": "INVALID"} and result.value == {}


def test_projection_does_not_mutate_input_and_static_array_paths_work():
    original = {"items": [{"id": 7, "token": "synthetic-secret"}]}
    result = project_json(original, ["$.items[0].id"], limits=TrajectoryLimits())
    assert result.value == {"items": [{"id": 7}]}
    result.value["items"][0]["id"] = 8
    assert original["items"][0]["id"] == 7


@pytest.mark.parametrize("visibility", [{}, [], 1, True])
def test_visibility_marker_requires_exact_enum_string(visibility):
    result = project_json({"id": "synthetic-secret"}, ["$.id"], limits=TrajectoryLimits(), source_visibility=visibility)
    assert result.value == {} and result.visibility == {"$.id": "INVALID"}


def test_large_existing_array_index_is_invalid_without_unbounded_projection_allocation():
    result = project_json({"items": [None] * 1001}, ["$.items[1000]"], limits=TrajectoryLimits())
    assert result.visibility == {"$.items[1000]": "INVALID"}
    assert result.value == {"items": []}
    assert not result.complete


@pytest.mark.parametrize("nested", [False, True])
def test_sdk_model_is_rejected_before_any_serialization(nested):
    class SdkPayload(BaseModel):
        id: str = "synthetic-secret"

        def model_dump(self, *args, **kwargs):
            raise AssertionError("source SDK serialization must not execute")

    value = {"id": SdkPayload()} if nested else SdkPayload()
    result = project_json(value, ["$.id"], limits=TrajectoryLimits())
    assert result.value == {} and result.visibility == {"$.id": "INVALID"}
    assert result.warnings == ["ARGUMENTS_INVALID"] and not result.complete


@pytest.mark.parametrize("value", ["4111111111111111", "4111 1111 1111 1111", "11010519491231002X", "Bearer synthetic-token", ["USD", "4111111111111111"]])
def test_governed_content_policy_removes_sensitive_values_even_under_safe_aliases(value):
    result = project_json({"customer_id": value}, ["$.customer_id"], limits=TrajectoryLimits(), content_policy_version="pii-v1")
    assert result.value == {} and result.visibility == {"$.customer_id": "REDACTED"}
    assert not result.complete and str(value) not in str(result.model_dump())


def test_content_policy_is_explicit_versioned_and_legacy_serialization_stays_stable():
    legacy = RedactionPolicy(version="legacy", input_paths=[], output_paths=[], argument_paths=["$.customer_id"], result_paths=[])
    assert "content_policy_version" not in legacy.model_dump()
    governed = RedactionPolicy.model_validate(legacy.model_dump() | {"content_policy_version": "pii-v1"})
    assert governed.digest != legacy.digest


def test_governed_scalar_array_is_allowed_without_admitting_nested_containers():
    safe = project_json({"values": [1, 2]}, ["$.values"], limits=TrajectoryLimits(), content_policy_version="pii-v1")
    assert safe.value == {"values": [1, 2]} and safe.visibility == {"$.values": "AVAILABLE"}
    unsafe = project_json({"values": [{"token": "synthetic-secret"}]}, ["$.values"], limits=TrajectoryLimits(), content_policy_version="pii-v1")
    assert unsafe.visibility == {"$.values": "INVALID"} and unsafe.value == {}


@pytest.mark.parametrize("text", ["Alice Example needs assistance", "plain-business-code"])
def test_allowlisted_alias_does_not_authorize_unclassified_string_content(text):
    result = project_json({"customer_id": text}, ["$.customer_id"], limits=TrajectoryLimits(), content_policy_version="pii-v1")
    assert result.value == {} and result.visibility["$.customer_id"] == "REDACTED"


@pytest.mark.parametrize("kind,text,visible", [("identifier", "synthetic-001", True),
    ("identifier", "Alice Example", False), ("identifier", "prefix4111111111111111suffix", False),
    ("currency", "USD", True), ("currency", "arbitrary text", False)])
def test_fixed_string_semantics_and_content_detection_are_both_required(kind, text, visible):
    result = project_json({"value": text}, ["$.value"], limits=TrajectoryLimits(), content_policy_version="pii-v1",
        string_policies={"$.value": kind})
    assert result.visibility["$.value"] == ("AVAILABLE" if visible else "REDACTED")
    assert result.value == ({"value": text} if visible else {})


def test_string_semantics_cannot_be_activated_without_governed_version_or_allowlist():
    base = {"version": "governed", "input_paths": [], "output_paths": [], "argument_paths": ["$.value"], "result_paths": []}
    with pytest.raises(ValueError):
        RedactionPolicy.model_validate(base | {"string_policies": {"$.value": "identifier"}})
    with pytest.raises(ValueError):
        RedactionPolicy.model_validate(base | {"content_policy_version": "pii-v1", "string_policies": {"$.other": "identifier"}})


def test_governed_scalar_array_remains_finitely_bounded_without_partial_values():
    result = project_json({"values": [1] * 1001}, ["$.values"], limits=TrajectoryLimits(), content_policy_version="pii-v1")
    assert result.value == {} and result.visibility["$.values"] == "INVALID"


@pytest.mark.parametrize("key", ["social_security_number", "socialSecurityNumber", "socialSecurityNUMBER", "customer_social_security_number", "SSNNumber", "SSNNUMBER", "customerSSNNumber", "SSNValue", "SocialSecurityNo", "PANNumber", "SSNVALUE", "SSNFIELD", "SSNDATA",
    "SocialSecurityID", "social_security_id", "customerSocialSecurityId", "socialSecurityIdentifier"])
def test_governed_projection_rejects_split_sensitive_field_allowlists(key):
    path = f"$.{key}"
    with pytest.raises(ValueError, match="TRAJECTORY_REDACTION_SECRET_PATH_FORBIDDEN"):
        project_json({key: "synthetic001"}, [path], limits=TrajectoryLimits(),
                     content_policy_version="pii-v1", string_policies={path: "identifier"})


@pytest.mark.parametrize("value", ["123-45-6789", "prefix123-45-6789suffix", ["synthetic-001", "123-45-6789"]])
def test_governed_identifier_redacts_formatted_ssn_under_safe_alias(value):
    result = project_json({"customer_id": value}, ["$.customer_id"], limits=TrajectoryLimits(),
                          content_policy_version="pii-v1", string_policies={"$.customer_id": "identifier"})
    assert result.value == {} and result.visibility["$.customer_id"] == "REDACTED"
    assert not result.complete
