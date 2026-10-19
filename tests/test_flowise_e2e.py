"""#213 示例契约测试；此处的本地 HTTP fixture 不代表真实模型验收。"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/eval-runner"))
from app.registry import map_request  # noqa: E402


def load_example(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples/flowise" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_mapping_isolates_sessions_and_excludes_ground_truth():
    e2e = load_example("e2e")
    cases = e2e.materialize_cases("unique-run")
    version = e2e.version_body("agent", "success", "native-flow", "success", "pinned-image")
    assert len(cases) == 5
    assert len({c["input"]["overrideConfig"]["sessionId"] for c in cases}) == 5
    assert version["endpoint"].endswith("/api/v1/prediction/native-flow")
    assert version["max_retries"] == 0 and version["is_idempotent"] is False
    for case in cases:
        body = map_request(case["input"], version["request_mapping"])
        assert body == case["input"]
        assert "expected_output" not in body and "scenario" not in body
        assert body["streaming"] is False


def test_flow_uses_native_tools_and_disables_memory():
    graph = json.loads((ROOT / "examples/flowise/agentflow.json").read_text())
    agent = next(n["data"]["inputs"] for n in graph["nodes"] if n["data"]["name"] == "agentAgentflow")
    assert agent["agentEnableMemory"] is False
    serialized = json.dumps(agent)
    assert "calculator" in serialized and "requestsGet" in serialized
    assert "evidence-tool:8080" in serialized


def test_attempt_checks_reject_fake_success_and_duplicate_invocations():
    e2e = load_example("e2e")
    item = {"id": "item", "execution_status": "succeeded"}
    attempt = {"http_status": 200}
    e2e.validate_attempts([item], {"item": [attempt]}, "success")
    with pytest.raises(AssertionError):
        e2e.validate_attempts([item], {"item": [attempt, attempt]}, "success")
    with pytest.raises(AssertionError):
        e2e.validate_attempts([item], {"item": [attempt]}, "auth")
    item["execution_status"] = "failed"
    for scenario, attempt in [
        ("auth", {"http_status": 401}),
        (
            "timeout",
            {
                "http_status": None,
                "error_type": "AMBIGUOUS_OUTCOME",
                "request_phase": "MAY_HAVE_BEEN_SENT",
                "latency_ms": 1001,
            },
        ),
        ("unreachable", {"http_status": 500}),
    ]:
        e2e.validate_attempts([item], {"item": [attempt]}, scenario)


def test_independent_business_tool_records_real_http_receipt(tmp_path):
    tool = load_example("evidence_tool")
    tool.LOG = tmp_path / "receipts.jsonl"
    server = tool.ThreadingHTTPServer(("127.0.0.1", 0), tool.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:

        def get(path):
            with urllib.request.urlopen(base + path) as response:
                return json.load(response)

        assert get("/health") == {"status": "ok"}
        assert get("/receipts") == []
        receipt = get("/run-tools")
        assert receipt["case_id"] == "run-tools" and receipt["value"] == 1266
        assert len(receipt["receipt"]) == 24
        assert get("/receipts") == [receipt]
        with pytest.raises(urllib.error.HTTPError) as error:
            get("/invalid/path")
        assert error.value.code == 400
        assert get("/receipts") == [receipt]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def native_fixture():
    namespace = "i213-unit"
    receipt = {"case_id": namespace + "-tools", "receipt": "independent-server-receipt"}
    outputs = {}
    executions = []
    for name, text in [("plain", "READY"), ("tools", receipt["receipt"]), ("structured", '{"status":"ok","value":42}')]:
        output = {"text": text, "sessionId": namespace + "-" + name, "executionId": name}
        outputs[name] = {"candidate": {"output": output}}
        executions.append({"id": name, "sessionId": namespace + "-" + name, "state": "FINISHED"})
    outputs["tools"]["candidate"]["output"]["agentFlowExecutedData"] = [
        {
            "nodeId": "agentAgentflow_0",
            "data": {
                "input": {"agentEnableMemory": False, "messages": [{"role": "user", "content": "current item"}]},
                "output": {
                    "usageMetadata": {"total_tokens": 15},
                    "usedTools": [
                        {"tool": "calculator", "toolOutput": "1266"},
                        {"tool": "evidence_lookup", "toolOutput": json.dumps(receipt)},
                    ],
                },
            },
        }
    ]
    executions += [
        {"id": str(i), "sessionId": namespace + "-timeout", "state": state}
        for i, state in enumerate(["FINISHED", "ERROR"])
    ]
    return [{"scenario": "success", "case_outputs": outputs}], executions, [receipt], namespace


def test_native_verifier_requires_independent_receipt_and_unique_executions():
    e2e = load_example("e2e")
    launches, executions, receipts, namespace = native_fixture()
    assert e2e.validate_native_evidence(launches, executions, receipts, namespace)["session_isolation"] == "PASS"
    with pytest.raises(AssertionError):
        e2e.validate_native_evidence(launches, executions, [], namespace)
    with pytest.raises(AssertionError):
        e2e.validate_native_evidence(launches, executions + [executions[0]], receipts, namespace)
    node = launches[0]["case_outputs"]["tools"]["candidate"]["output"]["agentFlowExecutedData"][0]["data"]
    node["input"]["messages"].append({"role": "user", "content": "secret-marker-" + namespace})
    with pytest.raises(AssertionError):
        e2e.validate_native_evidence(launches, executions, receipts, namespace)


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"])
def test_wait_recognizes_actual_launch_terminal_statuses(status):
    e2e = load_example("e2e")

    class Client:
        def argus(self, path):
            return {"status": status}

    import time

    assert e2e.wait_launch(Client(), "existing", time.monotonic() + 1)["status"] == status
