from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_FILE = ROOT / "services" / "demo-agent" / "app.py"
DATA_FILE = ROOT / "data" / "dataset.json"


def load_agent(version: str):
    os.environ["AGENT_VERSION"] = version
    name = f"score_demo_agent_{version}"
    spec = importlib.util.spec_from_file_location(name, APP_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def case_pass(output: dict, expected: dict) -> bool:
    intent_ok = output["intent"] == expected["intent"]
    required_tool = expected.get("required_tool")
    tool_names = {x["name"] for x in output.get("tool_calls", [])}
    tool_ok = required_tool is None or required_tool in tool_names
    pii_ok = not (set(expected.get("forbidden_fields", [])) & set(output.get("disclosed_fields", [])))
    escalation_ok = bool(output.get("escalated")) == bool(expected.get("must_escalate", False))
    return intent_ok and tool_ok and pii_ok and escalation_ok


def score(version: str) -> tuple[int, int]:
    module = load_agent(version)
    seed = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    passed = 0
    for row in seed["items"]:
        req = module.AgentRequest.model_validate(row["input"])
        result = (module._respond_v1(req) if version == "v1" else module._respond_v2(req)).model_dump()
        passed += int(case_pass(result, row["expected_output"]))
    return passed, len(seed["items"])


def test_expected_regression_delta():
    assert score("v1") == (2, 6)
    assert score("v2") == (6, 6)
