"""Cross-instance coordination for the Langfuse link backfill loop (Issue #44).

Production runs several Runner replicas, so every replica starting the link loop means
every replica resolves the same missing links and issues identical remote calls. The
compare-and-set write stops duplicate rows; only a lease stops duplicate lookups.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "services" / "eval-runner")]

from app.link_lease import (  # noqa: E402
    LINK_LEASE_KEY,
    LINK_LEASE_MIN_TTL_SECONDS,
    RedisLinkLoopLease,
    UnconditionalLinkLoopLease,
    build_link_loop_lease,
)


class FakeRedis:
    """The subset of redis-py used by the lease: SET NX EX / GET / EXPIRE / DELETE."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value.encode()
        self.ttls[key] = int(ex or 0)
        return True

    def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    def expire(self, key: str, seconds: int) -> bool:
        if key not in self.store:
            return False
        self.ttls[key] = int(seconds)
        return True

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


class BrokenRedis:
    def set(self, *_args: Any, **_kwargs: Any) -> bool:
        raise ConnectionError("redis down")

    def get(self, *_args: Any, **_kwargs: Any) -> bytes:
        raise ConnectionError("redis down")


def test_only_one_replica_holds_the_lease_at_a_time():
    redis = FakeRedis()
    first = RedisLinkLoopLease(redis, owner="runner-a")
    second = RedisLinkLoopLease(redis, owner="runner-b")

    assert first.try_acquire() is True
    # The second replica must stand down instead of duplicating the remote lookups.
    assert second.try_acquire() is False
    assert first.try_acquire() is True  # the holder renews rather than re-electing


def test_lease_is_released_on_shutdown():
    redis = FakeRedis()
    lease = RedisLinkLoopLease(redis, owner="runner-a")
    assert lease.try_acquire() is True

    lease.release()
    assert LINK_LEASE_KEY not in redis.store
    # Another replica can take over immediately instead of waiting out the TTL.
    assert RedisLinkLoopLease(redis, owner="runner-b").try_acquire() is True


def test_release_does_not_steal_another_replicas_lease():
    redis = FakeRedis()
    holder = RedisLinkLoopLease(redis, owner="runner-a")
    assert holder.try_acquire() is True

    RedisLinkLoopLease(redis, owner="runner-b").release()
    assert redis.store[LINK_LEASE_KEY] == b"runner-a"
    assert holder.try_acquire() is True


def test_lease_ttl_outlasts_a_single_reconciliation_pass():
    """A pass is bounded by a 10s budget; a shorter TTL would let a replica
    start a competing pass while the previous one is still resolving."""
    redis = FakeRedis()
    RedisLinkLoopLease(redis, owner="runner-a", ttl_seconds=1.0).try_acquire()
    assert redis.ttls[LINK_LEASE_KEY] >= LINK_LEASE_MIN_TTL_SECONDS


def test_redis_fault_fails_open_instead_of_stopping_the_backfill():
    """The link is a display value written under a compare-and-set: a Redis outage
    must not silently disable the feature, it only costs duplicate lookups."""
    lease = RedisLinkLoopLease(BrokenRedis(), owner="runner-a")
    assert lease.try_acquire() is True
    lease.release()  # must not raise


def test_no_redis_means_an_unfenced_loop():
    assert isinstance(build_link_loop_lease(None), UnconditionalLinkLoopLease)
    assert build_link_loop_lease(None).try_acquire() is True
    assert isinstance(build_link_loop_lease(FakeRedis()), RedisLinkLoopLease)
