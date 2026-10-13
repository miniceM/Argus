from decimal import Decimal

import pytest
from app.trajectory.contract import TrajectoryLimits
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
