from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .limiter import DistributedAgentLimiter
from .queue import QueueAdapter
from .state_machine import aggregate_launch_status_from_items


class ExecutionReconciler:
    """Scans and recovers state: delays to queued, expired leases, Redis backlog loss, and launch completion."""

    def __init__(
        self,
        db_mgr: DatabaseManager,
        queue: QueueAdapter,
        limiter: DistributedAgentLimiter,
    ):
        self.db_mgr = db_mgr
        self.queue = queue
        self.limiter = limiter

    def reconcile_retry_waits(self) -> int:
        """Transitions RETRY_WAIT items whose available_at <= now() back to QUEUED and reenqueues."""
        now = datetime.now(UTC)
        reenqueued = []

        with self.db_mgr.get_session() as session:
            items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.execution_status == "retry_wait",
                    ExperimentItemExecutionRecord.available_at <= now,
                )
            ).all()

            for it in items:
                it.execution_status = "queued"
                it.queued_at = now
                it.available_at = None
                it.lease_owner = None
                it.lease_token = None
                it.lease_expires_at = None
                it.updated_at = now
                reenqueued.append((it.id, it.dispatch_generation))

            session.commit()

        if reenqueued:
            self.queue.enqueue_items(reenqueued)

        return len(reenqueued)

    def reconcile_expired_leases(self) -> int:
        """Reclaims RUNNING items whose lease expired without completion. Enforces non-idempotent crash protection."""
        now = datetime.now(UTC)
        recovered_count = 0
        reenqueue_items = []

        with self.db_mgr.get_session() as session:
            expired_items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.execution_status == "running",
                    ExperimentItemExecutionRecord.lease_expires_at < now,
                )
            ).all()

            for it in expired_items:
                recovered_count += 1
                launch = session.get(ExperimentLaunchRecord, it.launch_id)
                manifest = launch.manifest if launch else {}
                agent_dict = manifest.get("agent", {})
                is_idempotent = bool(agent_dict.get("is_idempotent", False))
                max_retries = manifest.get("execution_policy", {}).get("max_retries", 2)

                # Find latest attempt
                latest_att = session.scalars(
                    select(ExecutionAttemptRecord)
                    .where(ExecutionAttemptRecord.item_execution_id == it.id)
                    .order_by(ExecutionAttemptRecord.attempt_no.desc())
                ).first()

                # Non-idempotent crash window: request may have been sent before worker crashed
                if latest_att and latest_att.request_phase in ("MAY_HAVE_BEEN_SENT", "RESPONSE_RECEIVED") and not is_idempotent:
                    latest_att.status = "FAILED"
                    latest_att.error_type = "AMBIGUOUS_OUTCOME"
                    latest_att.error_message = (
                        "Worker lease expired after request was dispatched. Non-idempotent agent outcome is ambiguous. Automatic retry prohibited."
                    )
                    latest_att.completed_at = now

                    it.execution_status = "failed"
                    it.eval_status = "skipped"
                    it.quality_conclusion = "fail"
                    it.final_attempt_id = latest_att.id
                    it.execution_error = latest_att.error_message
                    it.lease_owner = None
                    it.lease_token = None
                    it.lease_expires_at = None
                    it.completed_at = now
                    it.updated_at = now
                else:
                    # Retryable or idempotent
                    attempt_count = session.scalar(
                        select(func.count(ExecutionAttemptRecord.id)).where(
                            ExecutionAttemptRecord.item_execution_id == it.id
                        )
                    ) or 0

                    if attempt_count <= max_retries:
                        it.execution_status = "queued"
                        it.queued_at = now
                        it.available_at = None
                        it.lease_owner = None
                        it.lease_token = None
                        it.lease_expires_at = None
                        it.updated_at = now
                        reenqueue_items.append((it.id, it.dispatch_generation))
                    else:
                        it.execution_status = "timed_out"
                        it.eval_status = "skipped"
                        it.quality_conclusion = "fail"
                        it.lease_owner = None
                        it.lease_token = None
                        it.lease_expires_at = None
                        it.completed_at = now
                        it.updated_at = now

            session.commit()

        if reenqueue_items:
            self.queue.enqueue_items(reenqueue_items)

        return recovered_count

    def reconcile_backlog(self, max_items: int = 100) -> int:
        """Re-enqueues QUEUED items that may have been lost from Redis stream after restart."""
        reenqueued = []

        with self.db_mgr.get_session() as session:
            queued_items = session.scalars(
                select(ExperimentItemExecutionRecord)
                .where(ExperimentItemExecutionRecord.execution_status == "queued")
                .limit(max_items)
            ).all()

            for it in queued_items:
                reenqueued.append((it.id, it.dispatch_generation))

        if reenqueued:
            self.queue.enqueue_items(reenqueued)

        return len(reenqueued)

    def reconcile_launch_states(self) -> int:
        """Checks RUNNING or CANCELLING launches; transitions to terminal status when all items finish."""
        now = datetime.now(UTC)
        updated_count = 0

        with self.db_mgr.get_session() as session:
            active_launches = session.scalars(
                select(ExperimentLaunchRecord).where(
                    ExperimentLaunchRecord.status.in_(["RUNNING", "CANCELLING", "QUEUED"])
                )
            ).all()

            for launch in active_launches:
                counts_res = session.execute(
                    select(
                        ExperimentItemExecutionRecord.execution_status,
                        func.count(ExperimentItemExecutionRecord.id),
                    )
                    .where(ExperimentItemExecutionRecord.launch_id == launch.id)
                    .group_by(ExperimentItemExecutionRecord.execution_status)
                ).all()

                counts = {row[0].lower(): row[1] for row in counts_res}
                active_items = counts.get("pending", 0) + counts.get("queued", 0) + counts.get("running", 0) + counts.get("retry_wait", 0)

                if active_items == 0 and sum(counts.values()) > 0:
                    term_status, term_quality = aggregate_launch_status_from_items(counts)
                    launch.status = term_status
                    launch.quality_conclusion = term_quality
                    launch.completed_at = now
                    launch.updated_at = now
                    updated_count += 1
                elif launch.cancel_requested_at and counts.get("running", 0) == 0 and counts.get("retry_wait", 0) == 0:
                    # Quiescence reached for cancelling launch
                    term_status, term_quality = aggregate_launch_status_from_items(counts)
                    launch.status = "CANCELLED" if term_status != "COMPLETED" else "COMPLETED"
                    launch.completed_at = now
                    launch.updated_at = now
                    updated_count += 1

            session.commit()

        return updated_count
