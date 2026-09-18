from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
APP_FILE = ROOT / "services" / "demo-agent" / "app.py"


def load_agent(version: str):
    os.environ["AGENT_VERSION"] = version
    name = f"demo_agent_{version}"
    spec = importlib.util.spec_from_file_location(name, APP_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def payload(text: str, customer: str = "C1"):
    return {"messages": [{"role": "user", "content": text}], "customer_id": customer}


def test_v1_exposes_expected_regression_and_accepts_w3c_header():
    module = load_agent("v1")
    client = TestClient(module.app)
    r = client.post(
        "/invoke",
        json=payload("请帮我查询昨天账户中的异常交易"),
        headers={"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"},
    )
    assert r.status_code == 200
    body = r.json()
    assert r.headers["x-demo-traceparent-received"] == "true"
    assert body["intent"] == "transaction_investigation"
    assert body["tool_calls"][0]["name"] == "account-summary"  # deliberate baseline bug
    assert "full_account_number" in body["disclosed_fields"]


def test_v2_fixes_tool_pii_and_escalation():
    module = load_agent("v2")
    client = TestClient(module.app)
    r = client.post("/invoke", json=payload("发现一笔5万元陌生转账，请立即处理"))
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "transaction_investigation"
    assert body["tool_calls"][0]["name"] == "transaction-query"
    assert body["tool_calls"][0]["arguments"]["mask_sensitive"] is True
    assert body["disclosed_fields"] == []
    assert body["escalated"] is True


def test_v2_stolen_card_uses_freeze_tool():
    module = load_agent("v2")
    client = TestClient(module.app)
    r = client.post("/invoke", json=payload("银行卡被偷了，请马上冻结"))
    body = r.json()
    assert body["intent"] == "card_security"
    assert body["tool_calls"][0]["name"] == "card-freeze"
    assert body["escalated"] is True
