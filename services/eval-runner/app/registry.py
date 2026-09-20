from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class AgentVersionSpec:
    agent_id: str
    version: str
    endpoint: str
    method: str
    timeout_seconds: float
    max_retries: int
    rate_limit_per_minute: int
    request_mapping: dict[str, str]


class AgentRegistry:
    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}

    def list(self) -> dict[str, Any]:
        return self._raw.get("agents", {})

    def get(self, agent_id: str, version: str) -> AgentVersionSpec:
        try:
            raw = self._raw["agents"][agent_id]["versions"][version]
        except KeyError as exc:
            raise KeyError(f"Unknown agent/version: {agent_id}:{version}") from exc

        return AgentVersionSpec(
            agent_id=agent_id,
            version=version,
            endpoint=raw["endpoint"],
            method=raw.get("method", "POST").upper(),
            timeout_seconds=float(raw.get("timeout_seconds", 30)),
            max_retries=int(raw.get("max_retries", 1)),
            rate_limit_per_minute=int(raw.get("rate_limit_per_minute", 600)),
            request_mapping=dict(raw.get("request_mapping", {})),
        )


def _resolve_dot_path(obj: Any, path: str) -> Any:
    value = obj
    for part in path.split("."):
        if isinstance(value, dict):
            value = value[part]
        else:
            value = getattr(value, part)
    return value


def map_request(dataset_input: Any, mapping: dict[str, str]) -> dict[str, Any]:
    root = {"input": dataset_input}
    return {target: _resolve_dot_path(root, source) for target, source in mapping.items()}
