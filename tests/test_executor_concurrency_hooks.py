from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.executor import RemoteAgentExecutor  # noqa: E402
from app.registry import AgentVersionSpec  # noqa: E402


def test_concurrent_invoke_attempt_hooks_isolation():
    async def _run():
        spec = AgentVersionSpec(
            agent_id="test-agent",
            version="v1",
            endpoint="http://mock-agent.internal/invoke",
            method="POST",
            timeout_seconds=5.0,
            max_retries=2,
            rate_limit_per_minute=6000,
            request_mapping={},
            max_concurrency=10,
            is_idempotent=True,
        )
        executor = RemoteAgentExecutor(spec)

        # Assert deprecated mutable hook methods are removed
        assert not hasattr(executor, "set_attempt_hooks")
        assert not hasattr(executor, "_on_attempt_start")
        assert not hasattr(executor, "_on_attempt_end")

        call_counts: dict[str, int] = {}
        item_attempts_recorded: dict[str, list[dict]] = {f"item-{i}": [] for i in range(5)}

        def mock_handler(request: httpx.Request) -> httpx.Response:
            import json
            body = json.loads(request.content.decode("utf-8"))
            item_id = body.get("item_id")
            call_counts[item_id] = call_counts.get(item_id, 0) + 1

            if item_id in ("item-0", "item-1") and call_counts[item_id] == 1:
                return httpx.Response(500, json={"error": "transient failure"})
            return httpx.Response(200, json={"result": f"ok-{item_id}"})

        transport = httpx.MockTransport(mock_handler)

        async def _invoke_for_item(item_id: str):
            def on_attempt_start(attempt_no: int) -> str:
                att_id = f"{item_id}-att-{attempt_no}"
                item_attempts_recorded[item_id].append({"id": att_id, "attempt_no": attempt_no, "status": "started"})
                return att_id

            def on_attempt_end(att_id, http_status, error_type, error_message, latency_ms, trace_received):
                for a in item_attempts_recorded[item_id]:
                    if a["id"] == att_id:
                        a["status"] = "completed" if error_type is None else "failed"
                        a["http_status"] = http_status
                        a["error_type"] = error_type

            res = await executor.invoke(
                payload={"item_id": item_id},
                headers={"X-Item-Id": item_id},
                client_transport=transport,
                on_attempt_start=on_attempt_start,
                on_attempt_end=on_attempt_end,
            )
            return res

        tasks = [_invoke_for_item(f"item-{i}") for i in range(5)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 5

        # Check isolation:
        # Item 0 and 1 must have exactly 2 attempts
        assert len(item_attempts_recorded["item-0"]) == 2
        assert item_attempts_recorded["item-0"][0]["id"] == "item-0-att-1"
        assert item_attempts_recorded["item-0"][0]["status"] == "failed"
        assert item_attempts_recorded["item-0"][1]["id"] == "item-0-att-2"
        assert item_attempts_recorded["item-0"][1]["status"] == "completed"

        assert len(item_attempts_recorded["item-1"]) == 2
        assert item_attempts_recorded["item-1"][0]["id"] == "item-1-att-1"
        assert item_attempts_recorded["item-1"][0]["status"] == "failed"
        assert item_attempts_recorded["item-1"][1]["id"] == "item-1-att-2"
        assert item_attempts_recorded["item-1"][1]["status"] == "completed"

        # Items 2, 3, 4 must have exactly 1 attempt each
        for i in (2, 3, 4):
            item_key = f"item-{i}"
            assert len(item_attempts_recorded[item_key]) == 1
            assert item_attempts_recorded[item_key][0]["id"] == f"{item_key}-att-1"
            assert item_attempts_recorded[item_key][0]["status"] == "completed"

    asyncio.run(_run())


