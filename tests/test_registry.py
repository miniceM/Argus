from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.registry import AgentRegistry, map_request  # noqa: E402


def test_registry_and_request_mapping():
    registry = AgentRegistry(str(ROOT / "config" / "agents.yaml"))
    spec = registry.get("banking-agent", "v2")
    assert spec.endpoint.endswith("demo-agent-v2:8080/invoke")
    inp = {"messages": [{"role": "user", "content": "hello"}], "customer_id": "C100"}
    mapped = map_request(inp, spec.request_mapping)
    assert mapped["messages"] == inp["messages"]
    assert mapped["customer_id"] == "C100"
