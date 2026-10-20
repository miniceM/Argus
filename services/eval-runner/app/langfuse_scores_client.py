"""官方 Scores v3 / Observations v2 的有界只读入口，不执行 Evaluator。"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import sanitize_langfuse_input, validate_langfuse_dashboard_url


class ScoresReadError(RuntimeError):
    def __init__(self, code: str, *, retry_after: str | None = None):
        self.code = code
        self.retry_after = retry_after
        # 不回显上游响应、请求头、凭据或异常原文。
        super().__init__(code)


class ScoresV3Client:
    """一次收集不做隐藏重试；429/超时由调用方显式重试，无部分页采纳。"""

    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        *,
        transport=None,
        page_size: int = 100,
        max_pages: int = 100,
    ):
        try:
            base_url = validate_langfuse_dashboard_url(sanitize_langfuse_input(base_url))
        except ValueError:
            raise ScoresReadError("CLIENT_CONFIG_INVALID") from None
        if not base_url or not public_key or not secret_key:
            raise ScoresReadError("CLIENT_NOT_CONFIGURED")
        parsed = urlsplit(base_url)
        if parsed.path not in ("", "/") or not 1 <= page_size <= 1000 or not 1 <= max_pages <= 1000:
            raise ScoresReadError("CLIENT_CONFIG_INVALID")
        self.base_url = base_url.rstrip("/")
        self.page_size, self.max_pages = page_size, max_pages
        self.requests: list[dict[str, Any]] = []
        self.http = httpx.Client(
            base_url=self.base_url,
            auth=(public_key, secret_key),
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
            transport=transport,
        )

    @classmethod
    def from_env(cls):
        values = [
            sanitize_langfuse_input(os.getenv(k))
            for k in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
        ]
        if not all(values):
            raise ScoresReadError("CLIENT_NOT_CONFIGURED")
        return cls(*values)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def _pages(self, path: str, params: dict) -> list[dict]:
        rows, seen = [], set()
        params = {**params, "limit": self.page_size}
        for _ in range(self.max_pages):
            try:
                response = self.http.get(path, params=params)
            except httpx.TimeoutException:
                raise ScoresReadError("TIMEOUT") from None
            except httpx.HTTPError:
                raise ScoresReadError("NETWORK_UNAVAILABLE") from None
            self.requests.append({"method": "GET", "path": path, "query": dict(params), "status": response.status_code})
            if response.status_code != 200:
                code = {401: "AUTHENTICATION_FAILED", 403: "AUTHORIZATION_FAILED", 429: "RATE_LIMITED"}.get(
                    response.status_code, "UPSTREAM_UNAVAILABLE"
                )
                retry = response.headers.get("Retry-After", "")
                raise ScoresReadError(code, retry_after=retry if retry.isdigit() and len(retry) < 8 else None)
            if len(response.content) > 8 * 1024 * 1024:
                raise ScoresReadError("RESPONSE_TOO_LARGE")
            try:
                payload = response.json()
            except ValueError:
                raise ScoresReadError("RESPONSE_INVALID") from None
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("data"), list)
                or not all(isinstance(row, dict) for row in payload["data"])
                or not isinstance(payload.get("meta"), dict)
                or "cursor" not in payload["meta"]
            ):
                raise ScoresReadError("RESPONSE_INVALID")
            rows.extend(payload["data"])
            cursor = payload["meta"]["cursor"]
            if cursor is None:
                return rows
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ScoresReadError("PAGINATION_INVALID")
            seen.add(cursor)
            params["cursor"] = cursor
        raise ScoresReadError("PAGINATION_LIMIT")

    def scores_for(self, trace_id: str, observation_id: str, name: str) -> list[dict]:
        return self._pages(
            "/api/public/v3/scores",
            {
                "traceId": trace_id,
                "observationId": observation_id,
                "name": name,
                "source": "EVAL",
                "fields": "details,subject",
            },
        )

    def observations(self, trace_id: str) -> list[dict]:
        return self._pages(
            "/api/public/v2/observations", {"traceId": trace_id, "fields": "core,basic,io,metadata,trace_context"}
        )
