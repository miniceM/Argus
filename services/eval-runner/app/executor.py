from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from .registry import AgentVersionSpec
from .security import resolve_credential


class ErrorClassification(StrEnum):
    CONNECT_ERROR = "CONNECT_ERROR"
    READ_TIMEOUT = "READ_TIMEOUT"
    HTTP_4XX = "HTTP_4XX"
    HTTP_5XX = "HTTP_5XX"
    HTTP_429 = "HTTP_429"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SSL_ERROR = "SSL_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass
class RemoteCallResult:
    status_code: int
    body: dict[str, Any]
    attempts: int
    duration_ms: int
    trace_context_received: bool


class SlidingWindowRateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = max(1, limit_per_minute)
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0] >= 60:
                    self._events.popleft()
                if len(self._events) < self.limit:
                    self._events.append(now)
                    return
                wait_for = max(0.01, 60 - (now - self._events[0]))
            await asyncio.sleep(wait_for)


class RemoteAgentExecutor:
    def __init__(self, spec: AgentVersionSpec):
        self.spec = spec
        self._limiter = SlidingWindowRateLimiter(spec.rate_limit_per_minute)
        self._on_attempt_start: Callable[[int], str | None] | None = None
        self._on_attempt_end: (
            Callable[[str | None, int | None, str | None, str | None, int, bool], None] | None
        ) = None

    def set_attempt_hooks(
        self,
        on_start: Callable[[int], str | None],
        on_end: Callable[[str | None, int | None, str | None, str | None, int, bool], None],
    ) -> None:
        self._on_attempt_start = on_start
        self._on_attempt_end = on_end

    async def invoke(self, payload: dict[str, Any], headers: dict[str, str]) -> RemoteCallResult:
        if self.spec.method != "POST":
            raise ValueError(f"Executor currently supports POST only, got: {self.spec.method}")

        total_started = time.monotonic()
        total_attempts = 0
        call_headers = dict(headers)

        # Inject resolved credential if present
        if self.spec.credential_ref:
            token = resolve_credential(self.spec.credential_ref)
            if token:
                call_headers["Authorization"] = f"Bearer {token}"

        timeout = httpx.Timeout(
            self.spec.timeout_seconds,
            connect=min(5.0, self.spec.timeout_seconds),
            read=self.spec.timeout_seconds,
            write=self.spec.timeout_seconds,
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(self.spec.max_retries + 1):
                total_attempts = attempt + 1
                attempt_id: str | None = None
                if self._on_attempt_start:
                    attempt_id = self._on_attempt_start(total_attempts)

                await self._limiter.acquire()
                attempt_started = time.monotonic()

                try:
                    response = await client.post(self.spec.endpoint, json=payload, headers=call_headers)
                    duration_ms = int((time.monotonic() - attempt_started) * 1000)
                    trace_received = response.headers.get("x-demo-traceparent-received", "").lower() == "true"

                    # 1. Check HTTP 429
                    if response.status_code == 429:
                        if attempt < self.spec.max_retries:
                            if self._on_attempt_end:
                                self._on_attempt_end(
                                    attempt_id,
                                    429,
                                    ErrorClassification.HTTP_429,
                                    "Rate limited",
                                    duration_ms,
                                    trace_received,
                                )
                            retry_after = response.headers.get("retry-after")
                            delay = (
                                float(retry_after)
                                if retry_after and retry_after.isdigit()
                                else min(2**attempt, 5)
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            if self._on_attempt_end:
                                self._on_attempt_end(
                                    attempt_id,
                                    429,
                                    ErrorClassification.HTTP_429,
                                    "Rate limit exceeded",
                                    duration_ms,
                                    trace_received,
                                )
                            raise RuntimeError(f"Remote agent failed with HTTP_429 after {total_attempts} attempts")

                    # 2. Check HTTP 5xx
                    if response.status_code >= 500:
                        if attempt < self.spec.max_retries:
                            if self._on_attempt_end:
                                self._on_attempt_end(
                                    attempt_id,
                                    response.status_code,
                                    ErrorClassification.HTTP_5XX,
                                    f"Server error {response.status_code}",
                                    duration_ms,
                                    trace_received,
                                )
                            await asyncio.sleep(min(2**attempt, 5))
                            continue
                        else:
                            if self._on_attempt_end:
                                self._on_attempt_end(
                                    attempt_id,
                                    response.status_code,
                                    ErrorClassification.HTTP_5XX,
                                    f"Server error {response.status_code}",
                                    duration_ms,
                                    trace_received,
                                )
                            raise RuntimeError(
                                f"Remote agent failed with HTTP_5XX ({response.status_code}) after {total_attempts} attempts"
                            )

                    # 3. Check HTTP 4xx (Non-retryable)
                    if 400 <= response.status_code < 500:
                        if self._on_attempt_end:
                            self._on_attempt_end(
                                attempt_id,
                                response.status_code,
                                ErrorClassification.HTTP_4XX,
                                f"Client error {response.status_code}",
                                duration_ms,
                                trace_received,
                            )
                        raise RuntimeError(
                            f"Remote agent failed with HTTP_4XX ({response.status_code}) non-retryable error"
                        )

                    # 4. Check JSON format
                    try:
                        body = response.json()
                        if not isinstance(body, dict):
                            raise ValueError("Remote agent response must be a JSON object")
                    except Exception as exc:
                        if self._on_attempt_end:
                            self._on_attempt_end(
                                attempt_id,
                                response.status_code,
                                ErrorClassification.INVALID_RESPONSE,
                                str(exc),
                                duration_ms,
                                trace_received,
                            )
                        raise RuntimeError(
                            f"Remote agent failed with INVALID_RESPONSE: {exc}"
                        ) from exc

                    # Success!
                    if self._on_attempt_end:
                        self._on_attempt_end(attempt_id, response.status_code, None, None, duration_ms, trace_received)

                    return RemoteCallResult(
                        status_code=response.status_code,
                        body=body,
                        attempts=total_attempts,
                        duration_ms=int((time.monotonic() - total_started) * 1000),
                        trace_context_received=trace_received,
                    )

                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    duration_ms = int((time.monotonic() - attempt_started) * 1000)
                    if attempt < self.spec.max_retries:
                        if self._on_attempt_end:
                            self._on_attempt_end(
                                attempt_id,
                                None,
                                ErrorClassification.CONNECT_ERROR,
                                str(exc),
                                duration_ms,
                                False,
                            )
                        await asyncio.sleep(min(2**attempt, 5))
                        continue
                    else:
                        if self._on_attempt_end:
                            self._on_attempt_end(
                                attempt_id,
                                None,
                                ErrorClassification.CONNECT_ERROR,
                                str(exc),
                                duration_ms,
                                False,
                            )
                        raise RuntimeError(
                            f"Remote agent failed with CONNECT_ERROR after {total_attempts} attempts: {exc}"
                        ) from exc

                except httpx.ReadTimeout as exc:
                    duration_ms = int((time.monotonic() - attempt_started) * 1000)
                    # Side-effect protection: only retry if spec explicitly says is_idempotent=True!
                    if self.spec.is_idempotent and attempt < self.spec.max_retries:
                        if self._on_attempt_end:
                            self._on_attempt_end(
                                attempt_id,
                                None,
                                ErrorClassification.READ_TIMEOUT,
                                str(exc),
                                duration_ms,
                                False,
                            )
                        await asyncio.sleep(min(2**attempt, 5))
                        continue
                    else:
                        if self._on_attempt_end:
                            self._on_attempt_end(
                                attempt_id,
                                None,
                                ErrorClassification.READ_TIMEOUT,
                                str(exc),
                                duration_ms,
                                False,
                            )
                        raise RuntimeError(
                            f"Remote agent failed with READ_TIMEOUT: {exc}"
                        ) from exc

                except (RuntimeError, ValueError):
                    # Already handled and classified above, re-raise directly
                    raise

                except Exception as exc:
                    duration_ms = int((time.monotonic() - attempt_started) * 1000)
                    if self._on_attempt_end:
                        self._on_attempt_end(
                            attempt_id,
                            None,
                            ErrorClassification.UNKNOWN_ERROR,
                            str(exc),
                            duration_ms,
                            False,
                        )
                    raise RuntimeError(f"Remote agent call failed: {exc}") from exc

        raise RuntimeError(f"Remote agent failed after {total_attempts} attempt(s)")


def aggregate_launch_status(items: list[dict[str, Any]]) -> tuple[str, str]:
    """Aggregate individual item execution states into overall launch status and quality conclusion.

    Returns:
        (launch_status, quality_conclusion)
        launch_status: 'SUCCEEDED', 'FAILED', 'CANCELLED'
        quality_conclusion: 'pass', 'fail', 'unknown'
    """
    if not items:
        return "SUCCEEDED", "unknown"

    # Check for cancellation
    if any(item.get("execution_status") == "cancelled" for item in items):
        return "CANCELLED", "unknown"

    # Check for execution failure (e.g. timeout, 5xx, connect error)
    if any(item.get("execution_status") == "failed" for item in items):
        return "FAILED", "fail"

    # Check for evaluation failure (e.g. evaluator raised an unhandled exception)
    if any(item.get("eval_status") == "failed" for item in items):
        return "FAILED", "unknown"

    # All items succeeded in execution and evaluation!
    # Now check quality conclusions:
    conclusions = [item.get("quality_conclusion") for item in items]
    if all(c == "pass" for c in conclusions):
        return "SUCCEEDED", "pass"
    elif any(c == "fail" for c in conclusions):
        # Technical execution succeeded, but business quality failed!
        return "SUCCEEDED", "fail"
    else:
        return "SUCCEEDED", "unknown"
