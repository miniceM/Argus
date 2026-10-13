"""S4.0 观测链路验证：traceparent 透传 + Observation 字段可用性矩阵.

本文件是 Issue #5 S4.0 的实测载体，只验证"能观测到什么"，
不做 Assembler、不冻 schema、不加规则：

1. Runner 发出的 W3C traceparent 能被两种 Agent 实现原样收到，
   且 trace_id 一致（同 Trace 链路可复现）。
2. 用字段审计函数输出 Observation 可用性矩阵，明确 S4.1 必须容忍缺失的字段。
3. 全部基于 httpx MockTransport（完整 HTTP 请求/响应序列化）+ 纯函数，
   不依赖线上 Langfuse，不绑定本地端口（沙箱默认关闭本地端口绑定）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

APP_FILE = ROOT / "services" / "demo-agent" / "app.py"


def load_demo_agent(version: str):
    os.environ["AGENT_VERSION"] = version
    name = f"demo_agent_s40_{version}"
    spec = importlib.util.spec_from_file_location(name, APP_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_s40_traceparent_format_is_w3c():
    """RED 1：W3C traceparent 解析器必须存在且校验格式."""
    from app.observation_link import parse_traceparent

    parsed = parse_traceparent("00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
    assert parsed.trace_id == "0123456789abcdef0123456789abcdef"
    assert parsed.parent_span_id == "0123456789abcdef"
    assert parsed.version == "00"


def test_s40_same_trace_linkage():
    """RED 2：Runner 发出的 trace_id 与 Agent 收到的 trace_id 必须一致."""
    from app.observation_link import parse_traceparent, same_trace

    sent = parse_traceparent("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01")
    received = parse_traceparent("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-cccccccccccccccc-01")
    assert same_trace(sent, received) is True
    other = parse_traceparent("00-dddddddddddddddddddddddddddddddd-cccccccccccccccc-01")
    assert same_trace(sent, other) is False


def test_s40_two_agent_variants_receive_same_traceparent():
    """RED 3：两种框架风格的 Agent 实现都必须原样收到 traceparent.

    variant A 模拟原生 function-calling 服务（直接回显 header）；
    variant B 模拟 LangGraph 风格服务（多一层内部转发但保留 trace_id）。
    两者与 Runner 发出的 trace_id 必须一致，否则 S4.1 的 Assembler
    无从谈起"跨框架统一"。

    注：沙箱默认关闭本地端口绑定，因此不用真实 socket 服务，
    改用 httpx.MockTransport 走完整的 HTTP 请求/响应序列化，
    断言覆盖的 header 语义与真实建连一致。
    """
    from app.observation_link import parse_traceparent, same_trace

    received: dict[str, str | None] = {}

    def variant_a(request: httpx.Request) -> httpx.Response:
        received["a"] = request.headers.get("traceparent")
        return httpx.Response(200, json={"ok": True, "framework": "native"})

    def variant_b(request: httpx.Request) -> httpx.Response:
        # LangGraph 风格：内部多一跳，但 W3C trace_id 保持不变，
        # 只更换 parent span id（模拟内部 router span）。
        incoming = request.headers.get("traceparent") or ""
        parts = incoming.split("-")
        if len(parts) == 4:
            forwarded = f"{parts[0]}-{parts[1]}-ffffffffffffffff-{parts[3]}"
        else:
            forwarded = incoming
        received["b_raw"] = incoming
        received["b"] = forwarded
        return httpx.Response(200, json={"ok": True, "framework": "langgraph-style"})

    async def _run() -> None:
        traceparent = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"
        for name, handler in (("a", variant_a), ("b", variant_b)):
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                resp = await client.post(
                    "http://agent.local/invoke",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                    headers={"Content-Type": "application/json", "traceparent": traceparent},
                )
                assert resp.status_code == 200, name

    asyncio.run(_run())

    traceparent = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"
    assert received["a"] == traceparent
    assert received["b_raw"] == traceparent
    # B 内部转发换了 span id，但 trace_id 必须不变。
    assert received["b"] is not None
    assert same_trace(parse_traceparent(traceparent), parse_traceparent(received["b"]))


def test_s40_demo_agent_ack_is_not_inner_spans():
    """RED 4：Demo Agent 当前只有 header 回执，没有内部 span.

    该测试把现状锁死为证据：X-Demo-Traceparent-Received=true 只能证明
    header 到达，不能证明产生了可组装的内部 Observation。
    S4.1 的 Assembler 必须把这种 Agent 判为 DEGRADED_ONLY_ROOT。
    """
    from fastapi.testclient import TestClient

    module = load_demo_agent("v1")
    client = TestClient(module.app)
    r = client.post(
        "/invoke",
        json={"messages": [{"role": "user", "content": "查余额"}], "customer_id": "C1"},
        headers={"traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"},
    )
    assert r.status_code == 200
    assert r.headers.get("x-demo-traceparent-received") == "true"
    body = r.json()
    # 现状：返回体里没有任何 observation/span 结构。
    assert "observations" not in body
    assert "spans" not in body


def test_s40_observation_field_matrix():
    """RED 5：字段审计必须输出可用性矩阵，指明 S4.1 必须容忍的缺失."""
    from app.observation_link import audit_observation_fields

    # FULL：Runner 侧 remote-agent-http 自建 Observation，字段齐全。
    full = audit_observation_fields(
        {
            "observation_id": "obs-1",
            "trace_id": "tid-1",
            "parent_observation_id": None,
            "type": "TOOL",
            "name": "remote-agent-http",
            "started_at": "2026-10-12T00:00:00Z",
            "ended_at": "2026-10-12T00:00:01Z",
            "input": {},
            "output": {},
        }
    )
    assert full["missing"] == []

    # DEGRADED：HTTP-only Agent 无内部 span，只能审计出根节点。
    degraded = audit_observation_fields({"observation_id": "obs-root", "trace_id": "tid-1"})
    assert "type" in degraded["missing"]
    assert "started_at" in degraded["missing"]
    assert degraded["tolerable"] is True

    # 非字典输入按空观测处理：全部缺失且不可容忍。
    garbage = audit_observation_fields(None)  # type: ignore[arg-type]
    assert len(garbage["missing"]) == 6
    assert garbage["tolerable"] is False


@pytest.mark.parametrize(
    "bad_value",
    [
        "",
        "not-a-traceparent",
        "00-short-traceid-0123456789abcdef-01",
        "00-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-0123456789abcdef-01",
        "00-0123456789abcdef0123456789abcdef-shortspan-01",
    ],
)
def test_s40_malformed_traceparent_rejected(bad_value: str):
    """非法 traceparent 必须抛 ValueError，不能静默产生错误关联."""
    from app.observation_link import parse_traceparent

    with pytest.raises(ValueError):
        parse_traceparent(bad_value)


def test_s40_field_matrix_report_must_tolerate():
    """可用性矩阵报告必须给出 S4.1 可消费的 must_tolerate 并集."""
    from app.observation_link import summarize_field_matrix

    report = summarize_field_matrix(
        [
            {
                "observation_id": "obs-full",
                "trace_id": "tid-1",
                "type": "TOOL",
                "name": "remote-agent-http",
                "started_at": "2026-10-12T00:00:00Z",
                "ended_at": "2026-10-12T00:00:01Z",
            },
            {"observation_id": "obs-root", "trace_id": "tid-1"},
            {"trace_id": "tid-1"},  # 连 observation_id 都没有：不可容忍行
            None,  # 脏数据行：不得打断整批审计
        ]
    )
    by_name = {row["name"]: row for row in report["rows"]}
    assert by_name["remote-agent-http"]["missing"] == []
    assert by_name["obs-root"]["tolerable"] is True
    assert by_name["row-2"]["tolerable"] is False
    assert by_name["row-3"]["tolerable"] is False
    # must_tolerate 只来自可容忍行：DEGRADED 根节点缺的 4 个业务字段。
    assert report["must_tolerate"] == ["ended_at", "name", "started_at", "type"]


def test_s40_runner_executor_propagates_traceparent_end_to_end():
    """Runner 真实执行器必须把 traceparent 送到 Agent 并收回确认.

    走 RemoteAgentExecutor.invoke 的真实代码路径（含 header 透传、
    x-demo-traceparent-received 回执解析），只把网络层换成
    MockTransport。这是 S4.0「同 Trace 链路可复现」的端到端证据，
    也是 S4.1 Assembler 敢消费 trace_id 的前提。
    """
    from app.executor import RemoteAgentExecutor
    from app.observation_link import parse_traceparent, same_trace
    from app.registry import AgentVersionSpec

    received: dict[str, str | None] = {}

    def agent_handler(request: httpx.Request) -> httpx.Response:
        received["traceparent"] = request.headers.get("traceparent")
        return httpx.Response(
            200,
            json={"intent": "account_balance", "answer": "ok", "tool_calls": []},
            headers={"x-demo-traceparent-received": "true"},
        )

    spec = AgentVersionSpec(
        agent_id="test-agent",
        version="v1",
        endpoint="http://agent.local/invoke",
        method="POST",
        timeout_seconds=5,
        max_retries=0,
        rate_limit_per_minute=60,
        request_mapping={},
        max_concurrency=5,
        is_idempotent=False,
    )

    async def _run():
        executor = RemoteAgentExecutor(spec)
        return await executor.invoke(
            payload={"messages": [{"role": "user", "content": "查余额"}]},
            headers={
                "Content-Type": "application/json",
                "traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
            },
            client_transport=httpx.MockTransport(agent_handler),
        )

    result = asyncio.run(_run())
    assert result.status_code == 200
    assert result.trace_context_received is True
    assert received["traceparent"] is not None
    assert same_trace(
        parse_traceparent("00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"),
        parse_traceparent(received["traceparent"]),
    )
