"""有界 Langfuse v2 只读传输；原始页仅供进程内 Normalizer 消费。"""

import asyncio
import hashlib
import ipaddress
import math
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from .codec import canonical_loads
from .schema import JsonValue, TrajectoryScope


@dataclass(frozen=True)
class ObservationPage:
    rows: tuple[dict[str, JsonValue], ...]
    cursor: str | None
    source_bytes: int


class ObservationReadError(RuntimeError):
    def __init__(self, reason_code: str, *, retryable: bool, source_bytes: int = 0):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
        self.source_bytes = source_bytes


class ObservationReader(Protocol):
    async def read_page(
        self, scope: TrajectoryScope, *, cursor: str | None, page_size: int, timeout_seconds: float, max_bytes: int
    ) -> ObservationPage: ...


def _cursor_valid(cursor: object) -> bool:
    return cursor is None or (
        type(cursor) is str and 0 < len(cursor) <= 4096 and all(33 <= ord(char) <= 126 for char in cursor)
    )


def _trusted_plaintext_host(host: str, trusted_hosts: frozenset[str]) -> bool:
    if host == "localhost" or host in trusted_hosts:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LangfuseObservationReader:
    """URL/凭据由 Runner 已授权配置注入；不接受业务响应或浏览器提供的 URL。"""

    def __init__(
        self,
        *,
        server_url: str,
        public_key: str,
        secret_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        trusted_http_hosts: frozenset[str] = frozenset(),
    ):
        if type(trusted_http_hosts) is not frozenset or any(
            type(host) is not str or not host for host in trusted_http_hosts
        ):
            raise ValueError("TRAJECTORY_READER_CONFIGURATION_INVALID")
        try:
            url = httpx.URL(server_url)
            valid = (
                url.scheme in {"http", "https"}
                and url.host
                and not (url.userinfo or url.query or url.fragment)
                and (url.scheme == "https" or _trusted_plaintext_host(url.host, trusted_http_hosts))
            )
        except (httpx.InvalidURL, TypeError):
            valid = False
        if not valid or any(type(key) is not str or not key.strip() for key in (public_key, secret_key)):
            raise ValueError("TRAJECTORY_READER_CONFIGURATION_INVALID") from None
        self._client = httpx.AsyncClient(
            base_url=str(url).rstrip("/") + "/",
            auth=(public_key, secret_key),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={"accept": "application/json", "accept-encoding": "identity"},
        )

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify_api_contract(self, approved_digests: frozenset[str], *, timeout_seconds: float) -> None:
        """只探测固定公开 API 文档；1 MiB/单请求 timeout，不解码或保留原文。"""
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 5):
            raise ValueError("TRAJECTORY_READER_LIMIT_INVALID")
        if (type(approved_digests) is not frozenset or not 1 <= len(approved_digests) <= 16
                or any(type(value) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None
                       for value in approved_digests)):
            raise ValueError("TRAJECTORY_READER_APPROVAL_CONFIGURATION_INVALID")
        size, digest = 0, hashlib.sha256()
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client.stream("GET", "api/openapi.yaml", timeout=timeout_seconds) as response:
                    self._check_status(response.status_code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ObservationReadError("TRAJECTORY_SOURCE_ENCODING_UNSUPPORTED", retryable=False)
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        if not declared.isascii() or not declared.isdigit() or len(declared) > 20:
                            raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False)
                        if int(declared) > 1024 * 1024:
                            raise ObservationReadError("TRAJECTORY_SOURCE_API_DOCUMENT_LIMIT", retryable=False)
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 1024 * 1024:
                            raise ObservationReadError("TRAJECTORY_SOURCE_API_DOCUMENT_LIMIT", retryable=False)
                        digest.update(chunk)
        except (TimeoutError, httpx.TimeoutException):
            raise ObservationReadError("TRAJECTORY_SOURCE_TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            raise ObservationReadError("TRAJECTORY_SOURCE_TRANSPORT_ERROR", retryable=True) from None
        if "sha256:" + digest.hexdigest() not in approved_digests:
            raise ObservationReadError("TRAJECTORY_SOURCE_API_CONTRACT_MISMATCH", retryable=False)

    async def read_page(
        self, scope: TrajectoryScope, *, cursor: str | None, page_size: int, timeout_seconds: float, max_bytes: int
    ) -> ObservationPage:
        if (
            type(page_size) is not int
            or not 1 <= page_size <= 100
            or not _cursor_valid(cursor)
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 8 * 1024 * 1024
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 5
        ):
            raise ValueError("TRAJECTORY_READER_LIMIT_INVALID")
        scope = TrajectoryScope.model_validate(scope)
        params = {
            "traceId": scope.trace_id,
            "limit": page_size,
            "fields": "core,basic,time,io,metadata",
            "expandMetadata": "attributes.workflow.event_ids,attributes.workflow.predecessor_event_ids",
        }
        if cursor is not None:
            params["cursor"] = cursor
        body = bytearray()
        try:
            # HTTPX timeout 限制各次 I/O；外层同时限制整次请求/流，防慢速分块无限续命。
            async with asyncio.timeout(timeout_seconds):
                async with self._client.stream(
                    "GET", "api/public/v2/observations", params=params, timeout=timeout_seconds
                ) as response:
                    self._check_status(response.status_code)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ObservationReadError("TRAJECTORY_SOURCE_ENCODING_UNSUPPORTED", retryable=False)
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        if not content_length.isascii() or not content_length.isdigit() or len(content_length) > 20:
                            raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False)
                        if int(content_length) > max_bytes:
                            raise ObservationReadError("TRAJECTORY_SOURCE_BYTE_LIMIT", retryable=False)
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > max_bytes:
                            raise ObservationReadError(
                                "TRAJECTORY_SOURCE_BYTE_LIMIT", retryable=False, source_bytes=len(body) + len(chunk)
                            )
                        body.extend(chunk)
        except (TimeoutError, httpx.TimeoutException):
            raise ObservationReadError("TRAJECTORY_SOURCE_TIMEOUT", retryable=True, source_bytes=len(body)) from None
        except httpx.HTTPError:
            raise ObservationReadError(
                "TRAJECTORY_SOURCE_TRANSPORT_ERROR", retryable=True, source_bytes=len(body)
            ) from None
        # CancelledError 不捕获；stream/客户端由 async context manager 释放。
        try:
            payload = canonical_loads(body.decode("utf-8"), max_bytes=max_bytes)
        except (UnicodeError, ValueError) as error:
            code = str(error)
            reason = {
                "TRAJECTORY_JSON_DEPTH_LIMIT": "TRAJECTORY_SOURCE_DEPTH_LIMIT",
                "TRAJECTORY_JSON_STRING_LIMIT": "TRAJECTORY_SOURCE_STRING_LIMIT",
                "TRAJECTORY_JSON_NUMBER_LIMIT": "TRAJECTORY_SOURCE_NUMBER_LIMIT",
            }.get(code, "TRAJECTORY_SOURCE_JSON_INVALID")
            raise ObservationReadError(reason, retryable=False, source_bytes=len(body)) from None
        if (
            type(payload) is not dict
            or type(payload.get("data")) is not list
            or type(payload.get("meta")) is not dict
            or len(payload["data"]) > page_size
            or any(type(row) is not dict for row in payload["data"])
            or not _cursor_valid(payload["meta"].get("cursor"))
        ):
            raise ObservationReadError("TRAJECTORY_SOURCE_CONTRACT_ERROR", retryable=False, source_bytes=len(body))
        return ObservationPage(tuple(payload["data"]), payload["meta"].get("cursor"), len(body))

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 200:
            return
        if status in {401, 403}:
            code, retryable = "TRAJECTORY_SOURCE_AUTH_FAILED", False
        elif status == 408:
            code, retryable = "TRAJECTORY_SOURCE_TIMEOUT", True
        elif status == 429:
            code, retryable = "TRAJECTORY_SOURCE_RATE_LIMITED", True
        elif 500 <= status < 600:
            code, retryable = "TRAJECTORY_SOURCE_TEMPORARY_ERROR", True
        elif 300 <= status < 400:
            code, retryable = "TRAJECTORY_SOURCE_REDIRECT_REJECTED", False
        else:
            code, retryable = "TRAJECTORY_SOURCE_CONTRACT_ERROR", False
        raise ObservationReadError(code, retryable=retryable)
