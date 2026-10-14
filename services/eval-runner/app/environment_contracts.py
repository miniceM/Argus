"""环境请求与冻结证据分离：旧证据不允许读取时补造或规范化。"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MISSING = "SNAPSHOT_ENVIRONMENT_MISSING"
INVALID = "SNAPSHOT_ENVIRONMENT_INVALID"
MISMATCH = "BASELINE_ENVIRONMENT_MISMATCH"


def normalize_environment(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("environment must be a string")
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", normalized):
        raise ValueError("environment must be 1-64 lowercase letters, digits, dots, underscores, or hyphens")
    return normalized


def environment_reason(manifest: Mapping[str, Any] | None) -> str | None:
    if manifest is None:
        return MISSING
    if not isinstance(manifest, Mapping):
        return INVALID
    comparison = manifest.get("comparison")
    if comparison is None:
        return MISSING
    if not isinstance(comparison, Mapping):
        return INVALID
    value = comparison.get("environment")
    if value is None:
        return MISSING
    try:
        return None if normalize_environment(value) == value else INVALID
    except ValueError:
        return INVALID


def frozen_environment(manifest: Mapping[str, Any]) -> str | None:
    if environment_reason(manifest):
        return None
    return manifest["comparison"]["environment"]


class EnvironmentContractError(ValueError):
    def __init__(self, code: str, *, expected_environment: str, manifest: Mapping[str, Any], snapshot_id: str | None = None):
        self.code = code
        self.expected_environment = expected_environment
        self.snapshot_environment = frozen_environment(manifest)
        self.snapshot_id = snapshot_id
        super().__init__(f"Frozen snapshot environment is unavailable or does not match {expected_environment} ({code})")

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "expected_environment": self.expected_environment,
                "snapshot_environment": self.snapshot_environment, "snapshot_id": self.snapshot_id}


def environment_match_reasons(manifest: Mapping[str, Any], expected: str) -> list[str]:
    reason = environment_reason(manifest)
    if reason:
        return [reason]
    return [] if frozen_environment(manifest) == expected else [MISMATCH]


def require_snapshot_environment(snapshot: Any, expected: str) -> None:
    reasons = environment_match_reasons(snapshot.manifest, expected)
    if reasons:
        raise EnvironmentContractError(reasons[0], expected_environment=expected,
                                       manifest=snapshot.manifest, snapshot_id=snapshot.id)


def comparison_environment_reasons(baseline: Mapping[str, Any] | None, candidate: Mapping[str, Any] | None) -> list[str]:
    reasons = list(dict.fromkeys(reason for reason in (environment_reason(baseline), environment_reason(candidate)) if reason))
    if not reasons and frozen_environment(baseline) != frozen_environment(candidate):
        reasons.append(MISMATCH)
    return reasons
