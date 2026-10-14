"""单次有界 OpenAI-compatible Judge 传输；身份声明不替代真实模型/能力准入。"""

import asyncio
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Protocol

import httpx

from .codec import MAX_JSON_DEPTH, canonical_dumps, validate_json_depth
from .judge import Digest, FrozenJudgeDefinition, ParsedJudgeResult, judge_output_schema, parse_judge_output
from .judge_input import PreparedJudgeRequest, validate_prepared_judge_request
from .schema import Identity, _ContractModel

MAX_RESPONSE_BYTES = 128 * 1024


class JudgeProviderIdentity(_ContractModel):
    provider_id: Identity
    provider_api_version: Identity
    resolved_model_id: Identity
    resolved_model_artifact_digest: Digest


@dataclass(frozen=True)
class JudgeUsage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class JudgeProviderOutcome:
    result: ParsedJudgeResult
    usage: JudgeUsage | None
    input_digest: str | None
    definition_digest: str | None
    duration_ms: int


class JudgeProvider(Protocol):
    async def evaluate(self, request: PreparedJudgeRequest, definition: FrozenJudgeDefinition) -> JudgeProviderOutcome: ...


class _ProviderError(ValueError):
    pass


def _failure(code):
    raise _ProviderError(code) from None


def _safe_digest(request, field):
    value = getattr(request, field) if type(request) is PreparedJudgeRequest else None
    return value if type(value) is str and re.fullmatch(r"sha256:[a-f0-9]{64}", value) else None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _failure("JUDGE_PROVIDER_RESPONSE_INVALID")
        result[key] = value
    return result


def _envelope(raw):
    # content 是整个 Judge 输出文档，不能用来源标量的 4KiB 限额误拒。
    # 在解码前限制深度，避免递归/巨数与重复键；整个响应始终受字节上限约束。
    try:
        text = raw.decode("utf-8")
        depth = 0
        in_string = escaped = False
        for char in text:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "[{":
                depth += 1
                if depth > MAX_JSON_DEPTH + 1:
                    raise ValueError
            elif char in "]}":
                depth -= 1
        result = json.loads(text, object_pairs_hook=_pairs, parse_float=Decimal,
            parse_constant=lambda _: _failure("JUDGE_PROVIDER_RESPONSE_INVALID"))
        validate_json_depth(result)
        if type(result) is not dict:
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError, DecimalException):
        _failure("JUDGE_PROVIDER_RESPONSE_INVALID")


def _usage(body, definition):
    usage = body.get("usage")
    if usage is None:
        return None  # 下游账本保留最坏预留；缺用量不伪造 0。
    if type(usage) is not dict:
        _failure("JUDGE_PROVIDER_USAGE_INVALID")
    values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    if (any(type(value) is not int or value < 0 for value in values)
        or values[0] > definition.max_total_input_tokens or values[1] > definition.max_output_tokens):
        _failure("JUDGE_PROVIDER_USAGE_INVALID")
    if "total_tokens" in usage and (type(usage["total_tokens"]) is not int or usage["total_tokens"] != sum(values)):
        _failure("JUDGE_PROVIDER_USAGE_INVALID")
    return JudgeUsage(*values)


class OpenAICompatibleJudgeProvider:
    """仅接受 Runner 配置的固定端点/身份；首次真实调用前仍需 Provider preflight。"""

    def __init__(self, *, base_url: str, api_key: str, identity: JudgeProviderIdentity,
        transport: httpx.AsyncBaseTransport | None = None):
        try:
            url = httpx.URL(base_url)
            loopback = url.host == "localhost"
            if not loopback:
                try:
                    loopback = ipaddress.ip_address(url.host).is_loopback
                except ValueError:
                    pass
            if (not url.host or url.scheme not in {"https", "http"} or (url.scheme == "http" and not loopback)
                or url.userinfo or url.query or url.fragment or type(api_key) is not str or not api_key.strip()
                or any(char in api_key for char in "\r\n")):
                raise ValueError
            self._identity = JudgeProviderIdentity.model_validate(identity.model_dump())
        except (ValueError, TypeError, AttributeError, httpx.InvalidURL):
            raise ValueError("JUDGE_PROVIDER_CONFIGURATION_INVALID") from None
        self._semaphore = asyncio.Semaphore(2)
        self._client = httpx.AsyncClient(base_url=str(url).rstrip("/") + "/", transport=transport,
            trust_env=False, follow_redirects=False, headers={"authorization": "Bearer " + api_key,
                "accept": "application/json", "accept-encoding": "identity"},
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))

    @property
    def is_closed(self):
        return self._client.is_closed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def evaluate(self, request: PreparedJudgeRequest, definition: FrozenJudgeDefinition) -> JudgeProviderOutcome:
        started = time.monotonic_ns()
        usage = None
        try:
            definition = FrozenJudgeDefinition.model_validate(definition.model_dump())
            expected = JudgeProviderIdentity.model_validate({key: getattr(definition, key) for key in JudgeProviderIdentity.model_fields})
            if self._identity != expected:
                _failure("JUDGE_PROVIDER_IDENTITY_MISMATCH")
            timeout = float(definition.request_timeout_seconds)
            encoded = encode_judge_request(request, definition)
            async with asyncio.timeout(timeout):
                async with self._semaphore:
                    async with self._client.stream("POST", "chat/completions", content=encoded,
                        headers={"content-type": "application/json"}, timeout=httpx.Timeout(timeout)) as response:
                        if response.status_code != 200:
                            _failure("JUDGE_PROVIDER_HTTP_ERROR")
                        if response.headers.get("warning"):
                            _failure("JUDGE_PROVIDER_CAPABILITY_UNAVAILABLE")
                        if (response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}
                            or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"):
                            _failure("JUDGE_PROVIDER_RESPONSE_INVALID")
                        raw = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=16384):
                            if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                                _failure("JUDGE_PROVIDER_RESPONSE_TOO_LARGE")
                            raw.extend(chunk)
                        body = _envelope(raw)
            if body.get("model") != definition.resolved_model_id:
                _failure("JUDGE_MODEL_IDENTITY_MISMATCH")
            choices = body.get("choices")
            if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
                _failure("JUDGE_PROVIDER_RESPONSE_INVALID")
            choice = choices[0]
            message = choice.get("message")
            if (choice.get("finish_reason") != "stop" or type(choice.get("index")) is not int or choice["index"] != 0
                or type(message) is not dict or message.get("role") != "assistant" or message.get("refusal")
                or type(message.get("content")) is not str):
                _failure("JUDGE_PROVIDER_RESPONSE_INVALID")
            usage = _usage(body, definition)
            result = parse_judge_output(message["content"], policy=definition.output_policy, local_step_ids=request.local_step_ids)
        except asyncio.CancelledError:
            raise  # response context 已关闭，semaphore 已释放；取消不是伪造结果。
        except (TimeoutError, httpx.TimeoutException):
            result = ParsedJudgeResult("failed", None, "JUDGE_TIMEOUT")
        except httpx.HTTPError:
            result = ParsedJudgeResult("failed", None, "JUDGE_PROVIDER_NETWORK_ERROR")
        except _ProviderError as error:
            result = ParsedJudgeResult("failed", None, str(error))
        except (ValueError, TypeError, AttributeError):
            result = ParsedJudgeResult("failed", None, "JUDGE_PREPARED_REQUEST_INVALID")
        return JudgeProviderOutcome(result, usage, _safe_digest(request, "input_digest"), _safe_digest(request, "definition_digest"),
            max(0, (time.monotonic_ns() - started) // 1000000))


def encode_judge_request(request, definition):
    """验证准备入口并构造实际 HTTP 使用的精确 body，不插入额外输入或隐式模板选项。"""
    definition = FrozenJudgeDefinition.model_validate(definition.model_dump())
    validate_prepared_judge_request(request, definition)
    payload = {"model": definition.resolved_model_id, "temperature": definition.temperature,
        "top_p": definition.top_p, "max_tokens": definition.max_output_tokens, "stream": False,
        "response_format": {"type": "json_schema", "json_schema": {"name": "judge_output", "strict": True,
            "schema": judge_output_schema(definition.output_policy)}}}
    options = definition.provider_options
    if options is not None:
        payload["seed"] = options.seed
        if options.profile == "omlx-0.7-v1":
            payload["enable_thinking"] = options.enable_thinking
            payload["chat_template_kwargs"] = {"enable_thinking": options.enable_thinking}
    # Decimal 原样编码；messages 已经经过来源 seal 和结构校验。
    return (canonical_dumps(payload)[:-1] + ',"messages":' + request.messages_json + "}").encode("utf-8")
