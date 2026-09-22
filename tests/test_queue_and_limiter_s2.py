from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.limiter import MemoryAgentLimiter  # noqa: E402
from app.queue import MemoryQueueAdapter  # noqa: E402


def test_memory_queue_adapter_roundtrip():
    queue = MemoryQueueAdapter()
    assert queue.get_queue_depth() == 0

    queue.enqueue_items([("item-1", 1), ("item-2", 1), ("item-3", 2)])
    assert queue.get_queue_depth() == 3

    msgs = queue.read_group("worker-1", count=2, block_ms=0)
    assert len(msgs) == 2
    msg_id1, item_id1, gen1 = msgs[0]
    assert item_id1 == "item-1"
    assert gen1 == 1

    queue.ack(msg_id1)
    # Remaining 1 unread
    msgs2 = queue.read_group("worker-1", count=2, block_ms=0)
    assert len(msgs2) == 1
    assert msgs2[0][1] == "item-3"


def test_distributed_limiter_concurrency_and_rate():
    limiter = MemoryAgentLimiter()
    v_id = "test-agent-v1"

    # Concurrency limit = 2
    p1 = limiter.acquire_concurrency_permit(v_id, max_concurrency=2, owner_id="w1")
    assert p1 is not None
    p2 = limiter.acquire_concurrency_permit(v_id, max_concurrency=2, owner_id="w2")
    assert p2 is not None

    # Third attempt should be rejected
    p3 = limiter.acquire_concurrency_permit(v_id, max_concurrency=2, owner_id="w3")
    assert p3 is None

    # Release p1
    limiter.release_concurrency_permit(v_id, p1)
    p4 = limiter.acquire_concurrency_permit(v_id, max_concurrency=2, owner_id="w3")
    assert p4 is not None

    # Test rate limit
    assert limiter.acquire_rate_permit(v_id, rpm=2) is True
    assert limiter.acquire_rate_permit(v_id, rpm=2) is True
    assert limiter.acquire_rate_permit(v_id, rpm=2) is False
