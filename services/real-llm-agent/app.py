"""Issue #198: 真实 LLM 本地轻量 Agent。

独立 FastAPI 服务：POST /invoke -> OpenAI 兼容 Chat Completions -> answer。
不引入评测 SDK、Langfuse 或 Agent 框架；不透传业务模型字段；
唯一必需配置为 MODEL_BASE_URL / MODEL_API_KEY / MODEL_NAME（环境注入，不能写死）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx
import openai
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 50.0
DEFAULT_WRITE_TIMEOUT = 10.0
DEFAULT_POOL_TIMEOUT = 5.0
DEFAULT_DEADLINE_SECONDS = 60.0

_LOG = logging.getLogger("real_llm_agent")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_INVALID_BASE_URL_MESSAGE = (
    "MODEL_BASE_URL 必须是合法的 http/https URL（不含用户名、密码、query 或 fragment）"
)

_ERROR_MESSAGES: dict[str, str] = {
    "MODEL_TIMEOUT": "模型请求超时",
    "MODEL_UNREACHABLE": "无法连接模型服务",
    "MODEL_AUTH_FAILED": "模型服务鉴权失败",
    "MODEL_PERMISSION_DENIED": "模型服务拒绝访问",
    "MODEL_RATE_LIMITED": "模型服务限流",
    "MODEL_UPSTREAM_ERROR": "模型服务返回错误",
    "MODEL_INVALID_RESPONSE": "模型返回内容无效",
    "MODEL_CLIENT_UNAVAILABLE": "模型客户端未就绪",
    "INVALID_REQUEST": "请求体无效",
}


@dataclass(frozen=True)
class Settings:
    """模型网关连接配置；生产实例由环境变量注入。"""

    base_url: str
    api_key: str
    model_name: str
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    write_timeout: float = DEFAULT_WRITE_TIMEOUT
    pool_timeout: float = DEFAULT_POOL_TIMEOUT
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            base_url=os.environ.get("MODEL_BASE_URL", ""),
            api_key=os.environ.get("MODEL_API_KEY", ""),
            model_name=os.environ.get("MODEL_NAME", ""),
        )


class AgentConfigError(RuntimeError):
    """启动配置非法；错误信息必须不回显敏感值。"""


class AgentError(Exception):
    """稳定的 Agent 错误；body 仅包含受控 code/message，不透传上游原文。"""

    def __init__(self, status_code: int, code: str, *, duration_ms: int | None = None, request_id: str | None = None):
        super().__init__(_ERROR_MESSAGES.get(code, code))
        self.status_code = status_code
        self.code = code
        self.message = _ERROR_MESSAGES.get(code, code)
        self.duration_ms = duration_ms
        self.request_id = request_id


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content 不能为空白")
        return value


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageIn] = Field(min_length=1)
    customer_id: str | None = None


class UsageOut(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class InvokeResponse(BaseModel):
    """成功白名单输出；不透传 SDK 完整对象、密钥或任意 Provider extra 字段。"""

    answer: str
    model: str | None = None
    usage: UsageOut | None = None
    request_id: str | None = None


def validate_base_url(url: str) -> None:
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        raise AgentConfigError(_INVALID_BASE_URL_MESSAGE) from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise AgentConfigError(_INVALID_BASE_URL_MESSAGE)
    if not parts.hostname:
        raise AgentConfigError(_INVALID_BASE_URL_MESSAGE)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise AgentConfigError(_INVALID_BASE_URL_MESSAGE)


def validate_settings(settings: Settings) -> None:
    missing = []
    for attribute, env_name in (
        ("base_url", "MODEL_BASE_URL"),
        ("api_key", "MODEL_API_KEY"),
        ("model_name", "MODEL_NAME"),
    ):
        value = getattr(settings, attribute)
        if value is None or not str(value).strip():
            missing.append(env_name)
    if missing:
        raise AgentConfigError("缺少必需配置: " + ", ".join(missing))
    validate_base_url(settings.base_url)


def default_client_factory(settings: Settings) -> Any:
    timeout = httpx.Timeout(
        settings.read_timeout,
        connect=settings.connect_timeout,
        write=settings.write_timeout,
        pool=settings.pool_timeout,
    )
    return openai.AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        max_retries=0,
        timeout=timeout,
    )


def _as_valid_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _usage_from_raw(raw: Any) -> UsageOut | None:
    if not isinstance(raw, dict):
        return None

    def pick(*keys: str) -> int | None:
        for key in keys:
            tokens = _as_valid_int(raw.get(key))
            if tokens is not None:
                return tokens
        return None

    return UsageOut(
        input_tokens=pick("prompt_tokens", "input_tokens"),
        output_tokens=pick("completion_tokens", "output_tokens"),
        total_tokens=pick("total_tokens", "total"),
    )


def _sanitize_request_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if _REQUEST_ID_PATTERN.match(value):
        return value
    return None


def _parse_completion(completion: Any) -> InvokeResponse:
    data = completion if isinstance(completion, dict) else getattr(completion, "model_dump", lambda: None)()
    if not isinstance(data, dict):
        raise AgentError(502, "MODEL_INVALID_RESPONSE")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError(502, "MODEL_INVALID_RESPONSE")
    if len(choices) != 1 or not isinstance(choices[0], dict):
        raise AgentError(502, "MODEL_INVALID_RESPONSE")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise AgentError(502, "MODEL_INVALID_RESPONSE")
    model_value = data.get("model")
    model: str | None = None
    if isinstance(model_value, str) and model_value.strip():
        model = model_value
    request_id = _sanitize_request_id(data.get("id"))
    usage = _usage_from_raw(data.get("usage"))
    return InvokeResponse(answer=content, model=model, usage=usage, request_id=request_id)


async def _call_model(
    client: Any,
    settings: Settings,
    messages: list[dict[str, str]],
    session_id: str,
) -> Any:
    # x-opencode-session 是 OpenCode Go 等网关的路由/缓存提示；不使用业务数据生成。
    return await asyncio.wait_for(
        client.chat.completions.create(
            model=settings.model_name,
            messages=messages,
            stream=False,
            extra_headers={"x-opencode-session": session_id},
        ),
        timeout=settings.deadline_seconds,
    )


_TRACEPARENT_TRACE_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


def _session_id_from_request(request: Request) -> str:
    """从 W3C traceparent 取 trace-id 作为会话标识；缺失时生成一次性会话。

    会话标识不是业务字段：既有 trace 优先，缺省用随机 hex；不使用 customer_id
    或消息内容，避免把业务数据外泄到模型网关的路由/缓存提示上。
    """
    traceparent = request.headers.get("traceparent")
    if traceparent:
        parts = traceparent.split("-")
        if len(parts) >= 3 and _TRACEPARENT_TRACE_ID_PATTERN.match(parts[1]):
            return parts[1]
    return secrets.token_hex(16)


async def invoke_model(
    client: Any,
    settings: Settings,
    messages: list[dict[str, str]],
    session_id: str,
) -> InvokeResponse:
    """调用上游模型并把 SDK 异常映射为稳定的 Agent 错误。不吞掉 CancelledError。"""
    started = time.monotonic()
    try:
        completion = await _call_model(client, settings, messages, session_id)
    except TimeoutError:
        raise AgentError(504, "MODEL_TIMEOUT") from None
    except openai.APITimeoutError:
        raise AgentError(504, "MODEL_TIMEOUT") from None
    except openai.APIConnectionError:
        raise AgentError(502, "MODEL_UNREACHABLE") from None
    except openai.RateLimitError:
        raise AgentError(429, "MODEL_RATE_LIMITED") from None
    except openai.AuthenticationError:
        raise AgentError(401, "MODEL_AUTH_FAILED") from None
    except openai.PermissionDeniedError:
        raise AgentError(403, "MODEL_PERMISSION_DENIED") from None
    except Exception:
        raise AgentError(502, "MODEL_UPSTREAM_ERROR") from None
    duration_ms = int((time.monotonic() - started) * 1000)
    try:
        response = _parse_completion(completion)
    except AgentError as exc:
        exc.duration_ms = duration_ms
        raise
    _LOG.info(
        "model_call_succeeded duration_ms=%s request_id=%s",
        duration_ms,
        response.request_id,
    )
    return response


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[Settings], Any] | None = None,
    deadline_seconds: float | None = None,
) -> FastAPI:
    """构造 Agent 应用；生产配置在 lifespan 启动时校验并构造 client。"""
    resolved = settings if settings is not None else Settings.from_env()
    if deadline_seconds is not None:
        resolved = replace(resolved, deadline_seconds=deadline_seconds)
    factory: Callable[[Settings], Any] = client_factory or default_client_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_settings(resolved)
        if getattr(app.state, "client", None) is None:
            app.state.client = factory(resolved)
        try:
            yield
        finally:
            client = getattr(app.state, "client", None)
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close_result = close()
                    if inspect.isawaitable(close_result):
                        await close_result
                app.state.client = None

    app = FastAPI(
        title="Argus Real LLM Agent",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved
    app.state.client = None

    @app.exception_handler(AgentError)
    async def agent_error_handler(_: Request, exc: AgentError) -> JSONResponse:
        _LOG.warning(
            "model_call_failed code=%s http_status=%s duration_ms=%s request_id=%s",
            exc.code,
            exc.status_code,
            exc.duration_ms,
            exc.request_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": _ERROR_MESSAGES["INVALID_REQUEST"],
                    "details": details,
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        # health 仅证明本服务进程存活；不探测上游模型服务、不发任何远程请求。
        return {"status": "ok"}

    @app.post("/invoke", response_model=InvokeResponse)
    async def invoke(request: InvokeRequest, http_request: Request) -> InvokeResponse:
        client = getattr(app.state, "client", None)
        if client is None:
            raise AgentError(503, "MODEL_CLIENT_UNAVAILABLE")
        # customer_id 仅允许接收，不外传给模型；模型调用只包含 messages 与冻结的 model。
        messages = [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
        session_id = _session_id_from_request(http_request)
        return await invoke_model(client, app.state.settings, messages, session_id)

    return app


# uvicorn 入口：python -m uvicorn --host 0.0.0.0 --port 8080 app:app
# 导入时仅读取环境（不发远程请求）；配置校验发生在 lifespan 启动。
app = create_app()
