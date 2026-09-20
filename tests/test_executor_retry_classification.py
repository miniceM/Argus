from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.executor import ErrorClassification, RemoteAgentExecutor  # noqa: E402
from app.registry import AgentVersionSpec  # noqa: E402


def make_spec(is_idempotent: bool = False, max_retries: int = 2) -> AgentVersionSpec:
    return AgentVersionSpec(
        agent_id="test-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
        method="POST",
        timeout_seconds=5.0,
        max_retries=max_retries,
        rate_limit_per_minute=600,
        request_mapping={},
        is_idempotent=is_idempotent,
    )


def test_non_retryable_http_4xx():
    async def _run():
        spec = make_spec(max_retries=2)
        executor = RemoteAgentExecutor(spec)

        attempts_started: list[int] = []
        attempts_ended: list[tuple[int, str | None]] = []

        def on_start(attempt_no: int):
            attempts_started.append(attempt_no)
            return f"att-{attempt_no}"

        def on_end(att_id: str, status: int | None, err_type: str | None, err_msg: str | None, latency: int, trace: bool):
            attempts_ended.append((status, err_type))

        executor.set_attempt_hooks(on_start, on_end)

        mock_resp = httpx.Response(400, json={"error": "bad request"})
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_resp)):
            with pytest.raises(RuntimeError, match="HTTP_4XX"):
                await executor.invoke({"msg": "hello"}, {})

        # Must only attempt ONCE (no retry on 4xx)
        assert len(attempts_started) == 1
        assert len(attempts_ended) == 1
        assert attempts_ended[0][0] == 400
        assert attempts_ended[0][1] == ErrorClassification.HTTP_4XX

    asyncio.run(_run())


def test_non_retryable_invalid_json_format():
    async def _run():
        spec = make_spec(max_retries=2)
        executor = RemoteAgentExecutor(spec)

        attempts_started: list[int] = []
        attempts_ended: list[tuple[int, str | None]] = []

        executor.set_attempt_hooks(
            lambda no: attempts_started.append(no) or f"att-{no}",
            lambda a_id, st, err_type, msg, lat, tr: attempts_ended.append((st, err_type)),
        )

        mock_resp = httpx.Response(200, text="not valid json")
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=mock_resp)):
            with pytest.raises(RuntimeError, match="INVALID_RESPONSE"):
                await executor.invoke({"msg": "hello"}, {})

        assert len(attempts_started) == 1
        assert attempts_ended[0][1] == ErrorClassification.INVALID_RESPONSE

    asyncio.run(_run())


def test_read_timeout_non_idempotent_agent_does_not_retry():
    async def _run():
        # Side-effect protection: non-idempotent agent reading timeout must NOT be retried
        spec = make_spec(is_idempotent=False, max_retries=2)
        executor = RemoteAgentExecutor(spec)

        attempts = 0

        async def mock_post(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("timed out reading response")

        with patch.object(httpx.AsyncClient, "post", mock_post):
            with pytest.raises(RuntimeError, match="READ_TIMEOUT"):
                await executor.invoke({"msg": "transfer money"}, {})

        # Only 1 attempt allowed for non-idempotent read timeout!
        assert attempts == 1

    asyncio.run(_run())


def test_read_timeout_idempotent_agent_retries():
    async def _run():
        # When agent is explicitly idempotent, read timeout may be retried
        spec = make_spec(is_idempotent=True, max_retries=2)
        executor = RemoteAgentExecutor(spec)

        attempts = 0

        async def mock_post(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("timed out reading response")

        with patch.object(httpx.AsyncClient, "post", mock_post):
            with pytest.raises(RuntimeError, match="READ_TIMEOUT"):
                await executor.invoke({"msg": "read query"}, {})

        # Initial attempt + 2 retries = 3 attempts
        assert attempts == 3

    asyncio.run(_run())


def test_connect_error_and_503_retries():
    async def _run():
        spec = make_spec(max_retries=2)
        executor = RemoteAgentExecutor(spec)

        attempts = 0

        async def mock_post(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, json={"error": "service unavailable"})

        with patch.object(httpx.AsyncClient, "post", mock_post):
            with pytest.raises(RuntimeError, match="HTTP_5XX"):
                await executor.invoke({"msg": "query"}, {})

        assert attempts == 3

    asyncio.run(_run())
