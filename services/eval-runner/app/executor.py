from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

from .registry import AgentVersionSpec


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

    async def invoke(self, payload: dict[str, Any], headers: dict[str, str]) -> RemoteCallResult:
        if self.spec.method != "POST":
            raise ValueError("PoC executor currently supports POST only")

        attempts = 0
        started = time.monotonic()
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=self.spec.timeout_seconds) as client:
            for attempt in range(self.spec.max_retries + 1):
                attempts = attempt + 1
                await self._limiter.acquire()
                try:
                    response = await client.post(self.spec.endpoint, json=payload, headers=headers)
                    retryable = response.status_code == 429 or response.status_code >= 500
                    if retryable and attempt < self.spec.max_retries:
                        retry_after = response.headers.get("retry-after")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 5)
                        await asyncio.sleep(delay)
                        continue
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError("Remote agent response must be a JSON object")
                    return RemoteCallResult(
                        status_code=response.status_code,
                        body=body,
                        attempts=attempts,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        trace_context_received=(
                            response.headers.get("x-demo-traceparent-received", "").lower() == "true"
                        ),
                    )
                except (httpx.TransportError, httpx.TimeoutException, httpx.HTTPStatusError, ValueError) as exc:
                    last_error = exc
                    if attempt >= self.spec.max_retries:
                        break
                    await asyncio.sleep(min(2**attempt, 5))

        raise RuntimeError(f"Remote agent failed after {attempts} attempt(s): {last_error}") from last_error
