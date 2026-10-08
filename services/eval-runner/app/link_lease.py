"""Cross-instance coordination for the Langfuse link backfill loop.

Production supports several Runner replicas — AGENTS.md §5.3 requires Redis precisely
so that multi-instance execution stays coordinated. Every replica nevertheless started
the link loop unconditionally, and the candidate query carries no claim of its own, so
a launch missing its link was resolved by every replica at the same time. The
compare-and-set write prevented duplicate rows, but not the duplicate remote
Project/Dataset/Experiment calls behind it, and the per-process backoff state gave no
relief either: that is a rate-limit and latency problem against Langfuse rather than a
correctness one.

A short Redis lease elects one replica per window. Without Redis — single-instance dev
and test — the loop stays unfenced, which is the previous behaviour.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from typing import Any

logger = logging.getLogger("argus.link_lease")

LINK_LEASE_KEY = "argus:langfuse-link-backfill"
# One pass is bounded by reconcile_langfuse_links(budget_seconds=10.0) and the loop
# ticks every 5s, so the TTL has to comfortably outlast a single pass. Otherwise a
# second replica could start a competing pass while the first one is still resolving.
LINK_LEASE_MIN_TTL_SECONDS = 30.0


class UnconditionalLinkLoopLease:
    """Single-instance fallback: without Redis there is nobody to fence against."""

    def try_acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


class RedisLinkLoopLease:
    """Elects a single holder per TTL window with a compare-and-set lease."""

    def __init__(self, client: Any, *, owner: str, ttl_seconds: float = LINK_LEASE_MIN_TTL_SECONDS):
        self._client = client
        self._owner = owner
        self._ttl = max(float(ttl_seconds), LINK_LEASE_MIN_TTL_SECONDS)
        self._key = LINK_LEASE_KEY

    def _current_owner(self) -> str | None:
        current = self._client.get(self._key)
        if isinstance(current, bytes):
            current = current.decode("utf-8", "replace")
        return current

    def try_acquire(self) -> bool:
        try:
            if self._client.set(self._key, self._owner, nx=True, ex=self._ttl):
                return True
            current = self._current_owner()
        except Exception as exc:
            # Fail open on purpose. The link is a display value and the write stays a
            # compare-and-set, so a transient Redis fault must not silently stop the
            # backfill; it only costs duplicate remote lookups while Redis is down.
            logger.warning("Langfuse link lease unavailable, running unfenced: %s", exc)
            return True

        if current != self._owner:
            return False

        try:
            # Already ours: extend so a slow pass is not cut short mid-flight.
            self._client.expire(self._key, self._ttl)
        except Exception as exc:
            logger.warning("Could not renew the Langfuse link lease: %s", exc)
        return True

    def release(self) -> None:
        try:
            if self._current_owner() == self._owner:
                self._client.delete(self._key)
        except Exception as exc:
            logger.warning("Could not release the Langfuse link lease: %s", exc)


def default_owner() -> str:
    """Identifies one replica process; the random suffix avoids host/pid reuse."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def build_link_loop_lease(
    client: Any | None, *, ttl_seconds: float = LINK_LEASE_MIN_TTL_SECONDS
) -> UnconditionalLinkLoopLease | RedisLinkLoopLease:
    """Redis lease when a client is available, otherwise an unfenced loop."""
    if client is None:
        return UnconditionalLinkLoopLease()
    return RedisLinkLoopLease(client, owner=default_owner(), ttl_seconds=ttl_seconds)
