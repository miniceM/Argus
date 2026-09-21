from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.executor import RemoteAgentExecutor, SingleInvocationResult  # noqa: E402
from app.registry import AgentVersionSpec  # noqa: E402


@pytest.fixture
def agent_spec():
    return AgentVersionSpec(
        agent_id="test-agent",
        version="v1",
        endpoint="http://localhost:8080/invoke",
        method="POST",
        timeout_seconds=2,
        max_retries=2,
        rate_limit_per_minute=60,
        request_mapping={},
        max_concurrency=5,
        is_idempotent=False,
    )


def test_executor_invoke_once_success(agent_spec):
    async def _run():
        executor = RemoteAgentExecutor(agent_spec)

        async def mock_post(*args, **kwargs):
            return httpx.Response(
                status_code=200,
                json={"message": "hello"},
                headers={"Content-Type": "application/json", "x-demo-traceparent-received": "true"},
            )

        executor._client.post = mock_post
        res: SingleInvocationResult = await executor.invoke_once({"input": "test"}, {})
        assert res.status_code == 200
        assert res.body == {"message": "hello"}
        assert res.error_category is None
        assert res.trace_context_received is True

    asyncio.run(_run())


def test_executor_invoke_once_429_with_retry_after(agent_spec, monkeypatch):
    async def _run():
        # Ensure no sleep is called inside invoke_once!
        slept = False

        async def mock_sleep(seconds):
            nonlocal slept
            slept = True

        monkeypatch.setattr("asyncio.sleep", mock_sleep)

        executor = RemoteAgentExecutor(agent_spec)

        async def mock_post(*args, **kwargs):
            return httpx.Response(
                status_code=429,
                text="Too many requests",
                headers={"Retry-After": "45"},
            )

        executor._client.post = mock_post
        res = await executor.invoke_once({"input": "test"}, {})
        assert res.status_code == 429
        assert res.error_category == "HTTP_429"
        assert res.retry_after_seconds == 45
        assert res.is_retryable is True
        assert not slept, "invoke_once must not sleep internally!"

    asyncio.run(_run())


def test_executor_invoke_once_read_timeout_distinguishes_side_effects(agent_spec):
    async def _run():
        executor = RemoteAgentExecutor(agent_spec)

        async def mock_post(*args, **kwargs):
            raise httpx.ReadTimeout("Read timed out")

        executor._client.post = mock_post
        res = await executor.invoke_once({"input": "test"}, {})
        assert res.status_code is None
        assert res.error_category == "READ_TIMEOUT"
        # For non-idempotent agent, read timeout means request may have been processed remotely
        assert res.may_have_side_effects is True
        assert res.is_retryable is False  # Non-idempotent agent cannot safely auto-retry read timeout

    asyncio.run(_run())


def test_executor_invoke_once_connect_timeout_has_no_side_effects(agent_spec):
    async def _run():
        executor = RemoteAgentExecutor(agent_spec)

        async def mock_post(*args, **kwargs):
            raise httpx.ConnectTimeout("Connection refused")

        executor._client.post = mock_post
        res = await executor.invoke_once({"input": "test"}, {})
        assert res.error_category == "CONNECT_ERROR"
        assert res.may_have_side_effects is False
        assert res.is_retryable is True

    asyncio.run(_run())
