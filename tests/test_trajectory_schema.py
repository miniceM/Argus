from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from app.trajectory.codec import canonical_digest, canonical_dumps, canonical_loads
from app.trajectory.paths import compile_jsonpath
from app.trajectory.schema import (
    StandardTrajectory,
    TrajectoryAcquisition,
    TrajectoryCompleteness,
    TrajectoryEvidenceRef,
    TrajectoryScope,
    TrajectoryStep,
)
from pydantic import ValidationError
from trajectory_fixtures import (
    acquisition_payload,
    completeness_payload,
    evidence_ref_payload,
    scope_payload,
    step_payload,
    trajectory_payload,
)


def test_exact_number_and_utc_round_trip_keeps_unknown_fields_null():
    local_time = datetime(2026, 10, 13, 15, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    step = step_payload(started_at=local_time, duration_ms=Decimal("12.000000000000000000000000001"))
    step["arguments_redacted"]["large"] = 9007199254740993
    trajectory = StandardTrajectory.model_validate(trajectory_payload(steps=[step]))
    encoded = canonical_dumps(trajectory)
    assert '"started_at":"2026-10-13T07:00:00.123456Z"' in encoded
    assert '"duration_ms":12.000000000000000000000000001' in encoded
    assert '"large":9007199254740993' in encoded
    assert '"ended_at":null' in encoded
    decoded = StandardTrajectory.model_validate(canonical_loads(encoded))
    assert decoded.steps[0].started_at == local_time.astimezone(UTC)
    assert decoded.steps[0].ended_at is None
    assert decoded.steps[0].duration_ms == Decimal("12.000000000000000000000000001")
    assert decoded.steps[0].arguments_redacted == step["arguments_redacted"]
    assert canonical_dumps(decoded) == encoded


@pytest.mark.parametrize(
    "changes",
    [
        {"launch_id": ""},
        {"attempt_id": "x" * 65},
        {"dataset_item_id": "客户"},
        {"root_observation_id": "root\n"},
        {"trace_id": "0" * 32},
        {"trace_id": "A" * 32},
        {"trace_id": "1" * 31},
        {"dispatch_generation": True},
        {"dispatch_generation": "1"},
        {"dispatch_generation": 0},
        {"headers": {"Authorization": "synthetic-secret"}},
    ],
)
def test_scope_rejects_invalid_identity_and_untrusted_extra_fields(changes):
    with pytest.raises(ValidationError):
        TrajectoryScope.model_validate(scope_payload(**changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "HTTP"),
        ("phase", "COMPLETED"),
        ("status", "PASS"),
        ("index", -1),
        ("index", True),
        ("index", "0"),
        ("duration_ms", False),
        ("duration_ms", 1.5),
        ("duration_ms", "1.5"),
        ("duration_ms", Decimal("-1")),
        ("duration_ms", Decimal("NaN")),
        ("duration_ms", Decimal("Infinity")),
        ("started_at", datetime(2026, 10, 13)),
        ("started_at", 1791874800),
        ("started_at", "2026-10-13T07:00:00"),
        ("observation_id", "different-id"),
        ("arguments_redacted", {"amount": 1.5}),
        ("argument_visibility", {"$.amount": "HIDDEN"}),
    ],
)
def test_step_rejects_coercion_and_unknown_semantics(field, value):
    with pytest.raises(ValidationError):
        TrajectoryStep.model_validate(step_payload(**{field: value}))


def test_timing_and_argument_absence_do_not_turn_into_zero_or_missing_paths():
    step = TrajectoryStep.model_validate(
        step_payload(
            started_at=None,
            ended_at=None,
            duration_ms=None,
            arguments_redacted=None,
            argument_visibility={},
        )
    )
    assert step.duration_ms is None and step.started_at is None and step.ended_at is None
    assert step.arguments_redacted is None and step.argument_visibility == {}
    explicit_null = TrajectoryStep.model_validate(step_payload(arguments_redacted={"amount": None}))
    assert explicit_null.arguments_redacted == {"amount": None}


def test_causal_references_and_warning_codes_are_stable_sets_without_inventing_edges():
    step = TrajectoryStep.model_validate(step_payload(causal_predecessor_ids=["b", "a", "b"]))
    assert step.causal_predecessor_ids == ["a", "b"]
    assert TrajectoryStep.model_validate(step_payload()).causal_predecessor_ids == []
    coverage = TrajectoryCompleteness.model_validate(completeness_payload(warnings=["Z_CODE", "A_CODE", "Z_CODE"]))
    assert coverage.warnings == ["A_CODE", "Z_CODE"]


def test_completeness_capability_and_acquisition_are_separate_axes():
    coverage = completeness_payload(status="OBSERVED_COMPLETE")
    ready = TrajectoryAcquisition.model_validate(
        acquisition_payload(
            state="READY",
            completeness=coverage,
            evidence_ref=evidence_ref_payload(),
            retryable=False,
        )
    )
    assert ready.completeness.status == "OBSERVED_COMPLETE"
    assert ready.completeness.capabilities["human_events"] == "NONE"
    partial = TrajectoryAcquisition.model_validate(
        acquisition_payload(
            state="READY",
            completeness=completeness_payload(),
            evidence_ref=evidence_ref_payload(),
            retryable=False,
        )
    )
    assert partial.state == "READY" and partial.completeness.status == "PARTIAL"
    unavailable = TrajectoryAcquisition.model_validate(
        acquisition_payload(
            state="UNAVAILABLE",
            reason_code="TRAJECTORY_BACKEND_UNAVAILABLE",
            retryable=False,
        )
    )
    assert unavailable.completeness is None and unavailable.evidence_ref is None


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "READY"},
        {"state": "PENDING", "evidence_ref": evidence_ref_payload()},
        {"state": "UNAVAILABLE"},
        {"state": "UNAVAILABLE", "reason_code": "ERROR", "completeness": completeness_payload()},
        {"state": "UNAVAILABLE", "reason_code": "ERROR", "evidence_ref": evidence_ref_payload()},
        {"state": "READY", "completeness": completeness_payload(), "evidence_ref": evidence_ref_payload()},
    ],
)
def test_acquisition_rejects_half_ready_and_unavailable_evidence(changes):
    with pytest.raises(ValidationError):
        TrajectoryAcquisition.model_validate(acquisition_payload(**changes))


def test_all_six_capabilities_are_explicit_and_no_unknown_capability_is_accepted():
    payload = completeness_payload()
    del payload["capabilities"]["timing"]
    with pytest.raises(ValidationError):
        TrajectoryCompleteness.model_validate(payload)
    payload = completeness_payload()
    payload["capabilities"]["hidden_calls"] = "COMPLETE"
    with pytest.raises(ValidationError):
        TrajectoryCompleteness.model_validate(payload)


@pytest.mark.parametrize("location", ["schema_version", "provenance"])
def test_unregistered_schema_versions_are_rejected(location):
    payload = trajectory_payload()
    if location == "provenance":
        payload[location]["schema_version"] = "2.0"
    else:
        payload[location] = "1.1"
    with pytest.raises(ValidationError):
        StandardTrajectory.model_validate(payload)


@pytest.mark.parametrize("digest", ["5" * 64, "sha256:abc", "sha256:" + "A" * 64])
def test_evidence_reference_requires_canonical_digest_format(digest):
    with pytest.raises(ValidationError):
        TrajectoryEvidenceRef.model_validate(evidence_ref_payload(trajectory_digest=digest))


def test_duplicate_steps_and_missing_scope_root_are_rejected():
    with pytest.raises(ValidationError):
        StandardTrajectory.model_validate(trajectory_payload(steps=[step_payload(), step_payload(index=1)]))
    with pytest.raises(ValidationError):
        StandardTrajectory.model_validate(trajectory_payload(scope=scope_payload(root_observation_id="absent")))


def test_validation_message_does_not_echo_untrusted_input():
    with pytest.raises(ValidationError) as error:
        TrajectoryScope.model_validate(scope_payload(trace_id="synthetic-secret"))
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("path", ["$", "$.transfer.amount", "$.items[0].currency", "$.items[12]"])
def test_jsonpath_accepts_only_static_members_and_indices(path):
    assert isinstance(compile_jsonpath(path), tuple)


@pytest.mark.parametrize(
    "path",
    [
        "$..amount",
        "$.items[*]",
        "$.items[-1]",
        "$.items[1:2]",
        "$['amount']",
        "$.items[?(@.x)]",
        "$.amount()",
        "$.items[01]",
        "$.items[" + "9" * 65 + "]",
        "$" + ".a" * 33,
    ],
)
def test_dynamic_ambiguous_and_over_budget_jsonpaths_are_rejected(path):
    with pytest.raises(ValueError, match="TRAJECTORY_JSONPATH_"):
        compile_jsonpath(path)
    with pytest.raises(ValidationError):
        TrajectoryStep.model_validate(step_payload(argument_visibility={path: "AVAILABLE"}))


def test_paths_at_the_segment_budget_boundary_keep_exact_tokens():
    assert compile_jsonpath("$.items[0].currency") == ("items", 0, "currency")
    assert len(compile_jsonpath("$" + ".a" * 32)) == 32


def test_canonical_numbers_ignore_extra_zeroes_but_not_precision_or_value():
    first = {"z": Decimal("-0.00"), "a": Decimal("1.2300")}
    second = {"a": Decimal("1.23"), "z": 0}
    assert canonical_dumps(first) == '{"a":1.23,"z":0}'
    assert canonical_digest(first) == canonical_digest(second)
    assert canonical_digest(first) != canonical_digest({"a": Decimal("1.230000000000000000000000001"), "z": 0})


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        1.5,
        Decimal("NaN"),
        Decimal("1e65"),
        Decimal("1e-65"),
        10**64,
        "界" * 1366,
        "\ud800",
        {1: "not-a-json-key"},
    ],
)
def test_unbounded_or_lossy_json_values_are_rejected_in_schema_and_encoder(value):
    with pytest.raises(ValidationError):
        TrajectoryStep.model_validate(step_payload(arguments_redacted=value))
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_"):
        canonical_dumps(value)


def test_json_depth_and_utf8_byte_limits_are_checked_at_boundaries():
    value = "界" * 1365
    assert canonical_loads(canonical_dumps(value)) == value
    for _ in range(32):
        value = [value]
    assert canonical_loads(canonical_dumps(value)) == value
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_DEPTH_LIMIT"):
        canonical_dumps([value])
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_DEPTH_LIMIT"):
        canonical_loads("[" * 33 + "0" + "]" * 33)


def test_empty_container_at_maximum_value_depth_round_trips():
    value = []
    for _ in range(32):
        value = [value]
    assert canonical_loads(canonical_dumps(value)) == value


def test_full_trajectory_depth_budget_includes_the_step_envelope():
    value = 1
    for _ in range(29):
        value = [value]
    trajectory = StandardTrajectory.model_validate(trajectory_payload(steps=[step_payload(arguments_redacted=value)]))
    assert canonical_loads(canonical_dumps(trajectory))["steps"][0]["arguments_redacted"] == value
    with pytest.raises(ValidationError, match="TRAJECTORY_JSON_DEPTH_LIMIT"):
        StandardTrajectory.model_validate(trajectory_payload(steps=[step_payload(arguments_redacted=[value])]))


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', "NaN", "1e65", "1e-65", '"\\ud800"', "not-json"])
def test_decoder_rejects_invalid_or_ambiguous_evidence_without_echo(raw):
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_") as error:
        canonical_loads(raw)
    assert raw not in str(error.value)


def test_encoded_artifact_size_has_an_independent_byte_cap():
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_BYTE_LIMIT"):
        canonical_dumps({"amount": 12}, max_bytes=1)
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_BYTE_LIMIT"):
        canonical_loads('{"amount":12}', max_bytes=1)


def test_encoder_stops_when_budget_is_exceeded_before_visiting_later_values():
    # 后面的非法对象只能在访问时出错；早先内容已越界，不能再编码整个结构。
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_BYTE_LIMIT"):
        canonical_dumps(["a" * 4096, object()], max_bytes=100)


def test_numeric_upper_and_lower_boundaries_round_trip_without_decimal_context_rounding():
    from decimal import localcontext

    with localcontext() as context:
        context.prec = 3
        payload = {"max_integer": 10**64 - 1, "small": Decimal("1e-64"), "exact": Decimal("1.23456789")}
        assert canonical_loads(canonical_dumps(payload)) == payload
    with pytest.raises(ValueError, match="TRAJECTORY_JSON_NUMBER_LIMIT"):
        canonical_dumps(Decimal("1e64"))
