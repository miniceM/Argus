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


def environment_reason(manifest: Mapping[str, Any]) -> str | None:
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
