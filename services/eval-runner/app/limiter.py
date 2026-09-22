from __future__ import annotations

import abc
import time
import uuid
from typing import Any


class DistributedAgentLimiter(abc.ABC):
    """Abstract interface for distributed AgentVersion concurrency and rate limiting."""

    @abc.abstractmethod
    def acquire_concurrency_permit(
        self,
        agent_version_id: str,
        max_concurrency: int,
        timeout_sec: float = 30.0,
        owner_id: str = "",
    ) -> str | None:
        """Attempt to acquire a concurrency slot. Returns permit_id if acquired, None if full."""
        ...

    @abc.abstractmethod
    def release_concurrency_permit(self, agent_version_id: str, permit_id: str) -> None:
        """Release the acquired concurrency slot."""
        ...

    @abc.abstractmethod
    def renew_concurrency_permit(
        self, agent_version_id: str, permit_id: str, extra_sec: float = 30.0
    ) -> bool:
        """Heartbeat extension for an active permit. Returns False if expired or lost."""
        ...

    @abc.abstractmethod
    def acquire_rate_permit(self, agent_version_id: str, rpm: int) -> bool:
        """Attempt to acquire a rate limit slot within the 1-minute window."""
        ...


class MemoryAgentLimiter(DistributedAgentLimiter):
    """In-memory limiter for local testing."""

    def __init__(self):
        # agent_version_id -> {permit_id: expires_at}
        self._permits: dict[str, dict[str, float]] = {}
        # agent_version_id -> [timestamps]
        self._rate_windows: dict[str, list[float]] = {}

    def _clean_expired(self, agent_version_id: str) -> None:
        now = time.time()
        active = self._permits.get(agent_version_id, {})
        valid = {pid: exp for pid, exp in active.items() if exp > now}
        self._permits[agent_version_id] = valid

    def acquire_concurrency_permit(
        self,
        agent_version_id: str,
        max_concurrency: int,
        timeout_sec: float = 30.0,
        owner_id: str = "",
    ) -> str | None:
        self._clean_expired(agent_version_id)
        active = self._permits.setdefault(agent_version_id, {})
        if len(active) < max(1, max_concurrency):
            permit_id = f"permit-{uuid.uuid4().hex[:8]}"
            active[permit_id] = time.time() + timeout_sec
            return permit_id
        return None

    def release_concurrency_permit(self, agent_version_id: str, permit_id: str) -> None:
        active = self._permits.get(agent_version_id, {})
        active.pop(permit_id, None)

    def renew_concurrency_permit(
        self, agent_version_id: str, permit_id: str, extra_sec: float = 30.0
    ) -> bool:
        now = time.time()
        active = self._permits.get(agent_version_id, {})
        if permit_id in active and active[permit_id] > now:
            active[permit_id] = now + extra_sec
            return True
        return False

    def acquire_rate_permit(self, agent_version_id: str, rpm: int) -> bool:
        now = time.time()
        window = self._rate_windows.setdefault(agent_version_id, [])
        # Prune events older than 60s
        self._rate_windows[agent_version_id] = [t for t in window if now - t < 60.0]
        if len(self._rate_windows[agent_version_id]) < max(1, rpm):
            self._rate_windows[agent_version_id].append(now)
            return True
        return False


class RedisDistributedLimiter(DistributedAgentLimiter):
    """Production Redis Distributed Limiter with owner-tracking and fail-closed behavior."""

    def __init__(self, redis_client: Any, key_prefix: str = "argus:limiter"):
        self.client = redis_client
        self.prefix = key_prefix

    def acquire_concurrency_permit(
        self,
        agent_version_id: str,
        max_concurrency: int,
        timeout_sec: float = 30.0,
        owner_id: str = "",
    ) -> str | None:
        now = time.time()
        key = f"{self.prefix}:conc:{agent_version_id}"
        # Lua script to clean expired entries and conditionally add permit if size < max_concurrency
        lua = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local limit = tonumber(ARGV[2])
        local permit_id = ARGV[3]
        local expires_at = tonumber(ARGV[4])
        local timeout_sec = tonumber(ARGV[5])

        -- Remove expired permits
        redis.call('ZREMRANGEBYSCORE', key, '-inf', now)

        local current_count = redis.call('ZCARD', key)
        if current_count < limit then
            redis.call('ZADD', key, expires_at, permit_id)
            redis.call('EXPIRE', key, math.ceil(timeout_sec * 2))
            return permit_id
        else
            return nil
        end
        """
        permit_id = f"{owner_id}:{uuid.uuid4().hex[:8]}" if owner_id else uuid.uuid4().hex[:12]
        expires_at = now + timeout_sec
        try:
            res = self.client.eval(lua, 1, key, now, max_concurrency, permit_id, expires_at, timeout_sec)
            return res.decode() if isinstance(res, bytes) else res
        except Exception:
            raise

    def release_concurrency_permit(self, agent_version_id: str, permit_id: str) -> None:
        key = f"{self.prefix}:conc:{agent_version_id}"
        try:
            self.client.zrem(key, permit_id)
        except Exception:
            pass

    def renew_concurrency_permit(
        self, agent_version_id: str, permit_id: str, extra_sec: float = 30.0
    ) -> bool:
        now = time.time()
        key = f"{self.prefix}:conc:{agent_version_id}"
        new_expires = now + extra_sec
        # Update score only if member already exists
        lua = """
        local key = KEYS[1]
        local permit_id = ARGV[1]
        local new_expires = tonumber(ARGV[2])
        local score = redis.call('ZSCORE', key, permit_id)
        if score then
            redis.call('ZADD', key, new_expires, permit_id)
            return 1
        else
            return 0
        end
        """
        try:
            res = self.client.eval(lua, 1, key, permit_id, new_expires)
            return bool(res == 1)
        except Exception:
            return False

    def acquire_rate_permit(self, agent_version_id: str, rpm: int) -> bool:
        now = time.time()
        key = f"{self.prefix}:rate:{agent_version_id}"
        event_id = f"{now}-{uuid.uuid4().hex[:6]}"
        lua = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local limit = tonumber(ARGV[2])
        local event_id = ARGV[3]

        redis.call('ZREMRANGEBYSCORE', key, '-inf', now - 60.0)
        local count = redis.call('ZCARD', key)
        if count < limit then
            redis.call('ZADD', key, now, event_id)
            redis.call('EXPIRE', key, 120)
            return 1
        else
            return 0
        end
        """
        try:
            res = self.client.eval(lua, 1, key, now, rpm, event_id)
            return bool(res == 1)
        except Exception:
            raise
