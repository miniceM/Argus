"""S4 共享工厂：只含合成、已获准字段，不模拟线上 Observation。"""

from datetime import UTC, datetime
from decimal import Decimal


def scope_payload(**overrides):
    return {
        "launch_id": "launch-1",
        "item_execution_id": "item-1",
        "dataset_item_id": "dataset-item-1",
        "attempt_id": "attempt-1",
        "dispatch_generation": 1,
        "trace_id": "1" * 32,
        "root_observation_id": "root-1",
        **overrides,
    }


def completeness_payload(**overrides):
    return {
        "status": "PARTIAL",
        "capabilities": {
            "tool_executions": "COMPLETE",
            "tool_requests": "COMPLETE",
            "tool_arguments": "COMPLETE",
            "human_events": "NONE",
            "ordering": "NONE",
            "timing": "PARTIAL",
        },
        "warnings": [],
        **overrides,
    }


def provenance_payload(**overrides):
    return {
        "provider": "langfuse",
        "source_profile_id": "native-otel",
        "source_profile_version": "1.0",
        "schema_version": "1.0",
        "assembler_version": "1.0",
        "tool_mapping_version": "1.0",
        "tool_mapping_digest": "sha256:" + "2" * 64,
        "redaction_policy_version": "1.0",
        "redaction_policy_digest": "sha256:" + "3" * 64,
        "trajectory_contract_digest": "sha256:" + "4" * 64,
        "trajectory_digest": "sha256:" + "5" * 64,
        **overrides,
    }


def step_payload(**overrides):
    return {
        "step_id": "root-1",
        "observation_id": "root-1",
        "parent_step_id": "envelope-outside-root",
        "index": 0,
        "type": "CUSTOM",
        "raw_name": "remote-agent-http",
        "tool_identity": None,
        "phase": "EXECUTED",
        "status": "SUCCEEDED",
        "started_at": datetime(2026, 10, 13, 7, 0, 0, 123456, tzinfo=UTC),
        "ended_at": None,
        "duration_ms": None,
        "arguments_redacted": {"amount": Decimal("0.12345678901234567890123456789")},
        "argument_visibility": {"$.amount": "AVAILABLE"},
        "result_summary": None,
        "error_code": None,
        **overrides,
    }


def trajectory_payload(**overrides):
    return {
        "schema_version": "1.0",
        "scope": scope_payload(),
        "input_redacted": None,
        "output_redacted": {"accepted": True},
        "completeness": completeness_payload(),
        "steps": [step_payload()],
        "provenance": provenance_payload(),
        **overrides,
    }


def evidence_ref_payload(**overrides):
    return {
        "artifact_id": "artifact-1",
        "trajectory_digest": "sha256:" + "5" * 64,
        "trajectory_contract_digest": "sha256:" + "4" * 64,
        "schema_version": "1.0",
        **overrides,
    }


def acquisition_payload(**overrides):
    return {
        "scope": scope_payload(),
        "state": "PENDING",
        "completeness": None,
        "evidence_ref": None,
        "reason_code": None,
        "retryable": True,
        "deadline_at": datetime(2026, 10, 13, 7, 1, tzinfo=UTC),
        **overrides,
    }
