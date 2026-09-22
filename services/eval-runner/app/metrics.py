from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Response

metrics_router = APIRouter(tags=["Metrics"])
logger = logging.getLogger("argus.runtime")


class RuntimeMetrics:
    def __init__(self):
        self.queue_depth = 0
        self.item_executions_total: dict[str, int] = {}
        self.attempts_total: dict[str, int] = {}
        self.retries_total = 0
        self.lease_expiries_total = 0

    def record_item_status(self, status: str) -> None:
        st = status.upper()
        self.item_executions_total[st] = self.item_executions_total.get(st, 0) + 1

    def record_attempt(self, outcome: str) -> None:
        self.attempts_total[outcome] = self.attempts_total.get(outcome, 0) + 1

    def record_retry(self) -> None:
        self.retries_total += 1

    def record_lease_expiry(self) -> None:
        self.lease_expiries_total += 1

    def to_prometheus_format(self) -> str:
        lines = [
            "# HELP argus_queue_depth Current item executions in queue",
            "# TYPE argus_queue_depth gauge",
            f"argus_queue_depth {self.queue_depth}",
            "",
            "# HELP argus_retries_total Total attempt retries executed",
            "# TYPE argus_retries_total counter",
            f"argus_retries_total {self.retries_total}",
            "",
            "# HELP argus_lease_expiries_total Total expired leases recovered",
            "# TYPE argus_lease_expiries_total counter",
            f"argus_lease_expiries_total {self.lease_expiries_total}",
        ]
        for st, count in self.item_executions_total.items():
            lines.append(f'argus_item_executions_total{{status="{st}"}} {count}')
        for out, count in self.attempts_total.items():
            lines.append(f'argus_attempts_total{{outcome="{out}"}} {count}')
        return "\n".join(lines) + "\n"


runtime_metrics = RuntimeMetrics()


@metrics_router.get("/metrics", summary="Prometheus Metrics")
def get_metrics() -> Response:
    return Response(content=runtime_metrics.to_prometheus_format(), media_type="text/plain; version=0.0.4")


def log_execution_event(
    launch_id: str,
    item_execution_id: str,
    attempt_id: str | None = None,
    attempt_no: int | None = None,
    worker_id: str | None = None,
    agent_version_id: str | None = None,
    lease_token: str | None = None,
    trace_id: str | None = None,
    error_type: str | None = None,
    event: str = "EXECUTION_EVENT",
    **extra: Any,
) -> None:
    """Emits structured JSON execution log."""
    record = {
        "event": event,
        "launch_id": launch_id,
        "item_execution_id": item_execution_id,
        "attempt_id": attempt_id,
        "attempt_no": attempt_no,
        "worker_id": worker_id,
        "agent_version_id": agent_version_id,
        "lease_token": lease_token,
        "trace_id": trace_id,
        "error_type": error_type,
        **extra,
    }
    logger.info(json.dumps(record, ensure_ascii=False))
