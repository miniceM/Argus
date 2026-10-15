"""Issue #198 Real LLM Agent 单元测试。

通过独立模块名加载 services/real-llm-agent/app.py，避免与 Eval Runner 的
app 包或其他服务的 app.py 混淆。所有模拟上游均基于真实 openai SDK +
httpx.MockTransport：验证请求路径、鉴权头、禁止 SDK 自动重试、错误映射与日志脱敏。
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from fastapi.testclient import TestClient


class _AsyncMockTransport(httpx.AsyncBaseTransport):
    """事件循环内执行 handler；支持 async handler (供 deadline 阻塞测试)。"""

    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        result = self._handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "services" / "real-llm-agent" / "app.py"
SERVICE_MODULE_NAME = "real_llm_agent_service"

DEFAULTS: dict[str, Any] = {
    "base_url": "https://model.invalid/v1",
    "api_key": "test-api-key-not-real",
    "model_name": "test-model",
}
BEARER = f"Bearer {DEFAULTS['api_key']}"


def _load_service_module():
    spec = importlib.util.spec_from_file_location(SERVICE_MODULE_NAME, SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def svc():
    return _load_service_module()


@pytest.fixture
def settings(svc):
    return svc.Settings(**DEFAULTS)


# --- 模拟上游工具 ---------------------------------------------------------


def _recording_ok_transport(calls: dict[str, Any], **payload_overrides: Any):
    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] = calls.get("count", 0) + 1
        calls["last_request"] = request
        calls["last_body"] = json.loads(request.content)
        calls["last_auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json=_ok_completion(**payload_overrides),
            headers={"content-type": "application/json"},
        )

    return handler


def _status_transport(status: int, body: Any, calls: dict[str, Any] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls["count"] = calls.get("count", 0) + 1
        if isinstance(body, Exception):
            raise body
        return httpx.Response(
            status, json=body, headers={"content-type": "application/json"}
        )

    return handler


def _ok_completion(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "model": "gw-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "完整回答"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }
    body.update(overrides)
    return body


def _default_sdk_client(handler: Callable[[httpx.Request], httpx.Response]):
    timeouts = httpx.Timeout(50.0, connect=5.0, write=10.0, pool=5.0)
    http_client = httpx.AsyncClient(
        transport=_AsyncMockTransport(handler), base_url=DEFAULTS["base_url"]
    )
    return openai.AsyncOpenAI(
        api_key=DEFAULTS["api_key"],
        base_url=DEFAULTS["base_url"],
        http_client=http_client,
        max_retries=0,
        timeout=timeouts,
    )


def _invoke(
    svc,
    payload: dict[str, Any],
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    deadline_seconds: float | None = None,
):
    calls: dict[str, Any] = {"count": 0}
    if handler is None:
        handler = _recording_ok_transport(calls)

    def factory(_settings, **kwargs):
        return _default_sdk_client(handler)

    settings_kwargs = dict(DEFAULTS)
    if deadline_seconds is not None:
        settings_kwargs["deadline_seconds"] = deadline_seconds
    settings = svc.Settings(**settings_kwargs)
    app = svc.create_app(settings=settings, client_factory=factory)
    with TestClient(app) as client:
        resp = client.post("/invoke", json=payload)
    return resp, calls


BOT_MESSAGES = [
    {"role": "system", "content": "你是银行客服"},
    {"role": "user", "content": "查询余额"},
]


# --- 无效业务输入 ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"messages": [], "customer_id": "c1"}, id="empty-messages"),
        pytest.param(
            {"messages": [{"role": "user", "content": "   "}]}, id="blank-content"
        ),
        pytest.param(
            {"messages": [{"role": "tool", "content": "x"}]}, id="unsupported-role"
        ),
        pytest.param(
            {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
            id="extra-config-field",
        ),
        pytest.param({}, id="missing-messages"),
    ],
)
def test_invalid_business_input_returns_422_without_upstream(svc, payload):
    calls: dict[str, Any] = {"count": 0}
    handler = _recording_ok_transport(calls)
    resp, upcalls = _invoke(svc, payload, handler)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    # 具名 detail 只保留 loc/msg/type，不回显收到的真实值
    dump = json.dumps(body["error"])
    for detail in body["error"]["details"]:
        assert set(detail) <= {"loc", "msg", "type"}
    if payload.get("customer_id"):
        assert payload["customer_id"] not in dump
    if payload.get("temperature") is not None:
        assert json.dumps(payload["temperature"]) not in dump
    if payload.get("messages"):
        for message in payload["messages"]:
            content_value = message.get("content") if isinstance(message, dict) else None
            if isinstance(content_value, str) and content_value.strip():
                assert content_value not in dump
    assert upcalls["count"] == 0


# --- 成功路径 -------------------------------------------------------------


def test_invoke_success_sends_chat_completions(svc):
    resp, calls = _invoke(svc, {"messages": BOT_MESSAGES, "customer_id": "c-9"})
    assert resp.status_code == 200
    assert calls["count"] == 1
    request: httpx.Request = calls["last_request"]
    assert request.method == "POST"
    assert str(request.url) == "https://model.invalid/v1/chat/completions"
    assert calls["last_auth"] == BEARER
    sent_body = calls["last_body"]
    assert sent_body["model"] == DEFAULTS["model_name"]
    assert sent_body["messages"] == BOT_MESSAGES
    assert sent_body["stream"] is False
    assert set(sent_body) <= {"model", "messages", "stream"}
    assert "customer_id" not in sent_body


def test_invoke_success_response_shape(svc):
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) <= {"answer", "model", "usage", "request_id"}
    assert body["answer"] == "完整回答"
    assert body["model"] == "gw-model"
    assert body["request_id"] == "chatcmpl-abc123"
    assert body["usage"] == {
        "input_tokens": 3,
        "output_tokens": 5,
        "total_tokens": 8,
    }
    for forbidden in ("intent", "tool_calls", "escalated"):
        assert forbidden not in body


def test_invoke_preserves_provider_model_and_null_when_missing(svc):
    calls = {"count": 0}
    resp, _ = _invoke(
        svc,
        {"messages": BOT_MESSAGES},
        _recording_ok_transport(calls, model="weird-gateway-model"),
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "weird-gateway-model"

    partial = {
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "回答"}, "finish_reason": "stop"}
        ],
    }
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, _status_transport(200, partial))
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] is None
    assert body["request_id"] is None
    assert body["usage"] is None


# --- Usage 提取 -----------------------------------------------------------


USAGE_CASES = [
    pytest.param(
        {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        id="full-usage",
    ),
    pytest.param(None, None, id="usage-missing"),
    pytest.param(
        {"completion_tokens": 5},
        {"input_tokens": None, "output_tokens": 5, "total_tokens": None},
        id="partial-missing-input",
    ),
    pytest.param(
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        id="explicit-zero-kept",
    ),
    pytest.param(
        {"prompt_tokens": -1, "completion_tokens": 5, "total_tokens": 8},
        {"input_tokens": None, "output_tokens": 5, "total_tokens": 8},
        id="negative-token-invalid",
    ),
    pytest.param(
        {"prompt_tokens": True},
        {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        id="bool-token-invalid",
    ),
    pytest.param(
        {"prompt_tokens": 3.5},
        {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        id="float-token-invalid",
    ),
    pytest.param(
        {"prompt_tokens": "3"},
        {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        id="string-token-invalid",
    ),
]


@pytest.mark.parametrize("raw_usage,expected", USAGE_CASES)
def test_usage_extraction_never_fabricates(svc, raw_usage, expected):
    payload = _ok_completion(usage=raw_usage)
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, _status_transport(200, payload))
    assert resp.status_code == 200
    assert resp.json()["usage"] == expected


# --- 无效响应体 -----------------------------------------------------------


def _payload_with_content(content: Any) -> dict[str, Any]:
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "model": "gw-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"id": "c", "object": "chat.completion", "choices": []}, id="empty-choices"),
        pytest.param(_payload_with_content(""), id="empty-content"),
        pytest.param(_payload_with_content(None), id="null-content"),
        pytest.param(_payload_with_content("   "), id="blank-content"),
        pytest.param(_payload_with_content([{"type": "tool_use"}]), id="non-string-content"),
        pytest.param([1, 2, 3], id="non-object-body"),
        pytest.param({"id": "c", "no-choices": True}, id="malformed-shape"),
    ],
)
def test_invalid_provider_response_maps_to_502_invalid_response(svc, payload):
    handler = _status_transport(200, payload)
    resp, calls = _invoke(svc, {"messages": BOT_MESSAGES}, handler)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "MODEL_INVALID_RESPONSE"
    # 由于已向上游发出请求（测试目标本身是响应处理），此处不断言请求数。


# --- 上游错误映射 ---------------------------------------------------------


def _openai_error_payload(message: str, error_type: str = "invalid_request_error", code: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "type": error_type}
    if code is not None:
        error["code"] = code
    return {"error": error}


ERROR_CASES = [
    pytest.param(
        401,
        _openai_error_payload("Invalid API key provided: test-api-key-not-real", code="invalid_api_key"),
        401,
        "MODEL_AUTH_FAILED",
        id="401-auth",
    ),
    pytest.param(
        403,
        _openai_error_payload("Permission denied", "invalid_request_error", "insufficient_quota"),
        403,
        "MODEL_PERMISSION_DENIED",
        id="403-permission",
    ),
    pytest.param(
        429,
        _openai_error_payload("Rate limit reached", "requests", "rate_limit_exceeded"),
        429,
        "MODEL_RATE_LIMITED",
        id="429-rate-limit",
    ),
    pytest.param(
        500,
        _openai_error_payload("Internal server error", "internal_error"),
        502,
        "MODEL_UPSTREAM_ERROR",
        id="500-upstream",
    ),
    pytest.param(
        503,
        _openai_error_payload("Service unavailable", "internal_error"),
        502,
        "MODEL_UPSTREAM_ERROR",
        id="503-upstream",
    ),
]


@pytest.mark.parametrize("upstream_status,body,expected_status,expected_code", ERROR_CASES)
def test_upstream_error_status_mapping(svc, upstream_status, body, expected_status, expected_code):
    calls: dict[str, Any] = {"count": 0}
    handler = _status_transport(upstream_status, body, calls)
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, handler)
    assert resp.status_code == expected_status
    error = resp.json()["error"]
    assert error["code"] == expected_code
    assert "test-api-key-not-real" not in resp.text
    assert calls["count"] == 1, "max_retries=0 下 SDK 不允许发起第二次请求"


class _ConnectErrorHandler:
    def __init__(self):
        self.count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        raise httpx.ConnectError("connection refused")


def test_connection_error_maps_to_502_model_unreachable(svc):
    handler = _ConnectErrorHandler()
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, handler)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "MODEL_UNREACHABLE"
    assert handler.count == 1


def test_read_timeout_maps_to_504_model_timeout(svc):
    class _Handler:
        def __init__(self):
            self.count = 0

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.count += 1
            raise httpx.ReadTimeout("read timed out")

    handler = _Handler()
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, handler)
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "MODEL_TIMEOUT"
    assert handler.count == 1


def test_total_deadline_maps_to_504_model_timeout(svc):
    class _Handler:
        def __init__(self):
            self.count = 0

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            self.count += 1
            await asyncio.sleep(0.4)
            return httpx.Response(200, json=_ok_completion())

    handler = _Handler()
    resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, handler, deadline_seconds=0.05)
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "MODEL_TIMEOUT"
    assert handler.count == 1


# --- 安全与日志 -----------------------------------------------------------


def test_provider_error_never_leaks_key_or_body(svc, caplog):
    bad = _openai_error_payload(
        "Invalid API key provided: test-api-key-not-real", code="invalid_api_key"
    )
    with caplog.at_level(logging.DEBUG, logger="real_llm_agent"):
        resp, _ = _invoke(svc, {"messages": BOT_MESSAGES}, _status_transport(401, bad))
    assert resp.status_code == 401
    text = resp.text
    assert "test-api-key-not-real" not in text
    assert BEARER not in text
    assert "Invalid API key" not in text
    logs = caplog.text
    assert "test-api-key-not-real" not in logs
    assert "查询余额" not in logs
    assert "Invalid API key" not in logs


def test_success_logs_do_not_contain_answer_or_key(svc, caplog):
    with caplog.at_level(logging.DEBUG, logger="real_llm_agent"):
        resp, _ = _invoke(svc, {"messages": BOT_MESSAGES})
    assert resp.status_code == 200
    assert "查询余额" not in caplog.text
    assert "完整回答" not in caplog.text
    assert "test-api-key-not-real" not in caplog.text


# --- health 与生命周期 ----------------------------------------------------


def test_health_does_not_contact_upstream(svc):
    calls: dict[str, Any] = {"count": 0}
    settings = svc.Settings(**DEFAULTS)
    app = svc.create_app(
        settings=settings,
        client_factory=lambda s: _default_sdk_client(_recording_ok_transport(calls)),
    )
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert calls["count"] == 0
    assert resp.json()["status"] == "ok"


def test_client_factory_runs_once_and_closes_on_shutdown(svc):
    created: list[Any] = []

    class _StubClient:
        def __init__(self):
            self.closed = False
            created.append(self)

        async def close(self):
            self.closed = True

    settings = svc.Settings(**DEFAULTS)
    app = svc.create_app(settings=settings, client_factory=lambda s: _StubClient())
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert len(created) == 1
    assert created[0].closed is True


# --- 配置装载 -------------------------------------------------------------


_REQUIRED_VARS = ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")


def test_missing_required_config_rejects_startup_without_leak(svc, monkeypatch):
    for name in _REQUIRED_VARS:
        monkeypatch.delenv(name, raising=False)
    app = svc.create_app()
    with pytest.raises(RuntimeError) as info:
        with TestClient(app):
            pass
    message = str(info.value)
    for name in _REQUIRED_VARS:
        assert name in message


@pytest.mark.parametrize(
    "bad_url",
    [
        pytest.param("http://user:pass@model.invalid/v1", id="userinfo"),
        pytest.param("https://model.invalid/v1?x=1", id="query"),
        pytest.param("https://model.invalid/v1#frag", id="fragment"),
        pytest.param("ftp://model.invalid/v1", id="non-http-scheme"),
        pytest.param("", id="empty"),
        pytest.param("not-a-url", id="not-url"),
    ],
)
def test_invalid_base_url_rejected_without_echo(svc, monkeypatch, bad_url):
    monkeypatch.setenv("MODEL_BASE_URL", bad_url)
    monkeypatch.setenv("MODEL_API_KEY", "k")
    monkeypatch.setenv("MODEL_NAME", "m")
    app = svc.create_app()
    with pytest.raises(RuntimeError) as info:
        with TestClient(app):
            pass
    message = str(info.value)
    if bad_url:
        assert bad_url not in message
    if "@" in bad_url:
        assert "user" not in message and "pass" not in message


def test_env_loads_required_variables_and_di_overrides_client(svc, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", DEFAULTS["base_url"])
    monkeypatch.setenv("MODEL_API_KEY", DEFAULTS["api_key"])
    monkeypatch.setenv("MODEL_NAME", DEFAULTS["model_name"])
    calls: dict[str, Any] = {"count": 0}
    got_settings = {}

    def factory(config, **kwargs):
        got_settings["value"] = config
        return _default_sdk_client(_recording_ok_transport(calls))

    app = svc.create_app(client_factory=factory)
    with TestClient(app) as client:
        resp = client.post("/invoke", json={"messages": BOT_MESSAGES, "customer_id": "c"})
    assert resp.status_code == 200
    assert str(calls["last_request"].url) == "https://model.invalid/v1/chat/completions"
    from_config = got_settings["value"]
    assert from_config.base_url == DEFAULTS["base_url"]
    assert from_config.api_key == DEFAULTS["api_key"]
    assert from_config.model_name == DEFAULTS["model_name"]


# --- 取消语义 -------------------------------------------------------------


def test_cancelled_call_propagates_and_not_mapped_success(svc, settings):
    class _Completions:
        async def create(self, **kwargs):
            raise asyncio.CancelledError()

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Stub:
        def __init__(self):
            self.chat = _Chat()

    app = svc.create_app(settings=settings, client_factory=lambda s: _Stub())
    app.state.client = _Stub()

    route = next(r for r in app.routes if getattr(r, "path", None) == "/invoke")
    endpoint = route.endpoint
    req = svc.InvokeRequest(messages=[{"role": "user", "content": "hi"}], customer_id="c1")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(endpoint(req))
