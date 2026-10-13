"""S4.0 审计原语的单元回归；这些测试不构成真实 Langfuse 证据。"""

import pytest


def test_real_scope_requires_valid_w3c_parent():
    from app.observation_link import parse_traceparent

    parsed = parse_traceparent("00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
    assert parsed.trace_id == "0123456789abcdef0123456789abcdef"
    assert parsed.parent_span_id == "0123456789abcdef"


@pytest.mark.parametrize("value", [
    "ff-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    "00-00000000000000000000000000000000-0123456789abcdef-01",
    "00-0123456789abcdef0123456789abcdef-0000000000000000-01",
    "00-0123456789abcdef0123456789abcdef-0123456789abcdef-1",
    "00-0123456789abcdef0123456789abcdef-0123456789abcdef-gg",
    "000-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    "00-ABCDEF0123456789abcdef0123456789-0123456789abcdef-01",
    "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01-extra",
])
def test_invalid_context_cannot_be_reported_as_real_lineage(value):
    from app.observation_link import parse_traceparent

    with pytest.raises(ValueError):
        parse_traceparent(value)


def test_invalid_header_is_not_repeated_in_error_message():
    from app.observation_link import parse_traceparent

    untrusted = "untrusted-header-value-must-not-be-logged"
    with pytest.raises(ValueError) as caught:
        parse_traceparent(untrusted)
    assert untrusted not in str(caught.value)


def test_future_version_keeps_known_fields_without_interpreting_suffix():
    from app.observation_link import parse_traceparent

    parsed = parse_traceparent("01-0123456789abcdef0123456789abcdef-0123456789abcdef-01-future-data")
    assert parsed.version == "01"
    assert parsed.parent_span_id == "0123456789abcdef"
