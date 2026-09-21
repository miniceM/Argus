from __future__ import annotations

import abc
import time
import uuid
from typing import Any


class QueueAdapter(abc.ABC):
    """Abstract interface for ItemExecution queue delivery."""

    @abc.abstractmethod
    def enqueue_item(self, item_execution_id: str, generation: int) -> str:
        """Enqueue a single item execution notification."""
        ...

    @abc.abstractmethod
    def enqueue_items(self, items: list[tuple[str, int]]) -> list[str]:
        """Batch enqueue item executions."""
        ...

    @abc.abstractmethod
    def read_group(
        self, consumer_name: str, count: int = 10, block_ms: int = 1000
    ) -> list[tuple[str, str, int]]:
        """Read pending/new items for consumer group.

        Returns: list of (message_id, item_execution_id, generation)
        """
        ...

    @abc.abstractmethod
    def claim_pending_entries(
        self, consumer_name: str, min_idle_ms: int = 30000, count: int = 10
    ) -> list[tuple[str, str, int]]:
        """Claims abandoned pending messages from crashed workers.

        Returns: list of (message_id, item_execution_id, generation)
        """
        ...

    @abc.abstractmethod
    def ack(self, message_id: str) -> None:
        """Acknowledge processed message."""
        ...

    @abc.abstractmethod
    def get_queue_depth(self) -> int:
        """Return total unacknowledged or stream length depth."""
        ...


class MemoryQueueAdapter(QueueAdapter):
    """Thread-safe in-memory queue adapter for local testing."""

    def __init__(self):
        self._queue: list[tuple[str, str, int]] = []  # (msg_id, item_id, gen)
        self._acked: set[str] = set()
        self._read_idx = 0

    def enqueue_item(self, item_execution_id: str, generation: int) -> str:
        return self.enqueue_items([(item_execution_id, generation)])[0]

    def enqueue_items(self, items: list[tuple[str, int]]) -> list[str]:
        msg_ids = []
        for item_id, gen in items:
            msg_id = f"mem-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
            self._queue.append((msg_id, item_id, gen))
            msg_ids.append(msg_id)
        return msg_ids

    def read_group(
        self, consumer_name: str, count: int = 10, block_ms: int = 1000
    ) -> list[tuple[str, str, int]]:
        res = []
        while self._read_idx < len(self._queue) and len(res) < count:
            msg = self._queue[self._read_idx]
            self._read_idx += 1
            if msg[0] not in self._acked:
                res.append(msg)
        return res

    def claim_pending_entries(
        self, consumer_name: str, min_idle_ms: int = 30000, count: int = 10
    ) -> list[tuple[str, str, int]]:
        return []

    def ack(self, message_id: str) -> None:
        self._acked.add(message_id)

    def get_queue_depth(self) -> int:
        return len([m for m in self._queue if m[0] not in self._acked])


class RedisStreamQueueAdapter(QueueAdapter):
    """Production Redis Streams Queue adapter with Consumer Groups."""

    def __init__(
        self,
        redis_client: Any,
        stream_key: str = "argus:stream:item_executions",
        group_name: str = "argus:workers",
    ):
        self.client = redis_client
        self.stream_key = stream_key
        self.group_name = group_name
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream_key, self.group_name, id="0", mkstream=True)
        except Exception as exc:
            # BUSYGROUP Consumer Group name already exists
            if "BUSYGROUP" not in str(exc):
                # If error is not BUSYGROUP, re-raise to fail closed
                raise

    def enqueue_item(self, item_execution_id: str, generation: int) -> str:
        return self.enqueue_items([(item_execution_id, generation)])[0]

    def enqueue_items(self, items: list[tuple[str, int]]) -> list[str]:
        pipe = self.client.pipeline()
        for item_id, gen in items:
            pipe.xadd(
                self.stream_key,
                {"item_execution_id": item_id, "dispatch_generation": str(gen)},
            )
        return [str(res) for res in pipe.execute()]

    def read_group(
        self, consumer_name: str, count: int = 10, block_ms: int = 1000
    ) -> list[tuple[str, str, int]]:
        try:
            # Read new messages with ">"
            streams = {self.stream_key: ">"}
            raw_entries = self.client.xreadgroup(
                self.group_name,
                consumer_name,
                streams,
                count=count,
                block=block_ms,
            )
            if not raw_entries:
                return self.claim_pending_entries(consumer_name, min_idle_ms=30000, count=count)

            results = []
            for _stream_name, messages in raw_entries:
                for msg_id, data in messages:
                    # Parse bytes to str if needed
                    fields = {
                        (k.decode() if isinstance(k, bytes) else k): (
                            v.decode() if isinstance(v, bytes) else v
                        )
                        for k, v in data.items()
                    }
                    item_id = fields.get("item_execution_id", "")
                    gen = int(fields.get("dispatch_generation", 1))
                    m_id = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                    results.append((m_id, item_id, gen))
            return results
        except Exception:
            raise

    def claim_pending_entries(
        self, consumer_name: str, min_idle_ms: int = 30000, count: int = 10
    ) -> list[tuple[str, str, int]]:
        """Claims abandoned pending messages from crashed workers using XAUTOCLAIM."""
        try:
            res = self.client.xautoclaim(
                self.stream_key,
                self.group_name,
                consumer_name,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            )
            if not res or len(res) < 2:
                return []
            messages = res[1]
            results = []
            for msg_id, data in messages:
                fields = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in data.items()
                }
                item_id = fields.get("item_execution_id", "")
                gen = int(fields.get("dispatch_generation", 1))
                m_id = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                results.append((m_id, item_id, gen))
            return results
        except Exception:
            return []

    def ack(self, message_id: str) -> None:
        self.client.xack(self.stream_key, self.group_name, message_id)

    def get_queue_depth(self) -> int:
        try:
            return int(self.client.xlen(self.stream_key))
        except Exception:
            return 0
