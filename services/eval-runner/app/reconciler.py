from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .langfuse_sync import aggregate_launch_sync_status
from .limiter import DistributedAgentLimiter
from .metrics import runtime_metrics
from .queue import QueueAdapter
from .state_machine import (
    TERMINAL_LAUNCH_STATUSES,
    aggregate_launch_status_from_items,
    assert_terminal_launch_invariants,
)


class ExecutionReconciler:
    """Scans and recovers state: delays to queued, expired leases, Redis backlog loss, and launch completion."""

    def __init__(
        self,
        db_mgr: DatabaseManager,
        queue: QueueAdapter,
        limiter: DistributedAgentLimiter,
        langfuse_client: Any | Callable[[], Any] | None = None,
    ):
        self.db_mgr = db_mgr
        self.queue = queue
        self.limiter = limiter
        self._lf_provider = langfuse_client

    @property
    def langfuse_client(self) -> Any | None:
        provider = self._lf_provider
        if provider is None:
            return None
        if hasattr(provider, "get_dataset_run") or hasattr(provider, "api"):
            return provider
        if callable(provider):
            try:
                return provider()
            except Exception:
                return None
        return provider

    @langfuse_client.setter
    def langfuse_client(self, client: Any | None) -> None:
        self._lf_provider = client

    def reconcile_legacy_terminal_launch_evidence(self, launch_id: str | None = None) -> int:
        """Entry point for evidence-first reconciliation of legacy terminal launches."""
        return self.reconcile_launch_states()

    def _fetch_langfuse_evidence(self, t_launch: ExperimentLaunchRecord) -> dict[str, Any] | None:
        """Attempts to fetch Langfuse execution evidence for a terminal launch.
        Returns a dict mapping dataset_item_id -> evidence_item if found, or None.
        """
        lf = self.langfuse_client
        if not lf:
            return None

        dataset_name = t_launch.dataset_name
        run_name = t_launch.name
        run_id = t_launch.langfuse_experiment_id

        # 1. Try get_dataset_run by dataset_name and run_name/run_id
        for candidate_name in filter(None, [run_name, run_id]):
            try:
                run_data = lf.get_dataset_run(dataset_name=dataset_name, run_name=candidate_name)
                items = getattr(run_data, "dataset_run_items", None) or []
                res = {}
                for it in items:
                    d_id = getattr(it, "dataset_item_id", None)
                    if d_id:
                        res[str(d_id)] = it
                if res:
                    return res
            except Exception:
                pass

        # 2. Try get_dataset_runs list to find matching run
        if hasattr(lf, "get_dataset_runs"):
            try:
                paginated = lf.get_dataset_runs(dataset_name=dataset_name, limit=50)
                runs = getattr(paginated, "data", []) or []
                for r in runs:
                    if (run_id and getattr(r, "id", None) == run_id) or (run_name and getattr(r, "name", None) == run_name):
                        matched_run_name = getattr(r, "name", None)
                        if matched_run_name:
                            run_data = lf.get_dataset_run(dataset_name=dataset_name, run_name=matched_run_name)
                            items = getattr(run_data, "dataset_run_items", None) or []
                            if items:
                                return {
                                    getattr(it, "dataset_item_id", None): it
                                    for it in items
                                    if getattr(it, "dataset_item_id", None)
                                }
            except Exception:
                pass

        return None

    def reconcile_all_active_launches(self) -> int:
        """Alias for reconcile_launch_states."""
        return self.reconcile_launch_states()

    def reconcile_retry_waits(self) -> int:
        """Transitions RETRY_WAIT items whose available_at <= now() back to QUEUED and reenqueues."""
        now = datetime.now(UTC)
        reenqueued = []

        with self.db_mgr.get_session() as session:
            is_pg = session.bind.dialect.name == "postgresql"
            now_sql = func.clock_timestamp() if is_pg else func.now()

            items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.execution_status == "retry_wait",
                    ExperimentItemExecutionRecord.available_at <= now_sql,
                )
            ).all()

            for it in items:
                res = session.execute(
                    update(ExperimentItemExecutionRecord)
                    .where(
                        ExperimentItemExecutionRecord.id == it.id,
                        ExperimentItemExecutionRecord.execution_status == "retry_wait",
                        ExperimentItemExecutionRecord.dispatch_generation == it.dispatch_generation,
                    )
                    .values(
                        execution_status="queued",
                        queued_at=now,
                        available_at=None,
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
                if res.rowcount == 1:
                    reenqueued.append((it.id, it.dispatch_generation))
                    runtime_metrics.record_retry()

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
            is_pg = session.bind.dialect.name == "postgresql"
            now_sql = func.clock_timestamp() if is_pg else func.now()

            expired_items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.execution_status == "running",
                    ExperimentItemExecutionRecord.lease_expires_at <= now_sql,
                )
            ).all()

            for it in expired_items:
                launch = session.get(ExperimentLaunchRecord, it.launch_id)
                manifest = launch.manifest if launch else {}
                agent_dict = manifest.get("agent", {})
                is_idempotent = bool(agent_dict.get("is_idempotent", False))
                max_retries = manifest.get("execution_policy", {}).get("max_retries", 2)

                # Locate active attempt
                active_att = None
                if it.active_attempt_id:
                    active_att = session.get(ExecutionAttemptRecord, it.active_attempt_id)
                if not active_att:
                    active_att = session.scalars(
                        select(ExecutionAttemptRecord)
                        .where(ExecutionAttemptRecord.item_execution_id == it.id)
                        .order_by(ExecutionAttemptRecord.attempt_no.desc())
                    ).first()

                # Non-idempotent crash window: request may have been sent before worker crashed
                has_ambiguous_risk = (
                    active_att
                    and active_att.request_phase in ("MAY_HAVE_BEEN_SENT", "RESPONSE_RECEIVED")
                    and not is_idempotent
                )

                if has_ambiguous_risk:
                    # CAS update on item with lease expiration check
                    item_update = (
                        update(ExperimentItemExecutionRecord)
                        .where(
                            ExperimentItemExecutionRecord.id == it.id,
                            ExperimentItemExecutionRecord.dispatch_generation == it.dispatch_generation,
                            ExperimentItemExecutionRecord.lease_token == it.lease_token,
                            ExperimentItemExecutionRecord.execution_status == "running",
                            ExperimentItemExecutionRecord.lease_expires_at <= now_sql,
                        )
                        .values(
                            execution_status="failed",
                            eval_status="skipped",
                            quality_conclusion="unknown",
                            final_attempt_id=active_att.id if active_att else None,
                            execution_error=(
                                "Worker lease expired after request was dispatched. Non-idempotent agent outcome is ambiguous. Automatic retry prohibited."
                            ),
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            completed_at=now,
                            updated_at=now,
                        )
                    )
                    res = session.execute(item_update)
                    if res.rowcount != 1:
                        # Worker renewed lease or completed, skip!
                        continue

                    if active_att:
                        att_update = (
                            update(ExecutionAttemptRecord)
                            .where(
                                ExecutionAttemptRecord.id == active_att.id,
                                ExecutionAttemptRecord.item_execution_id == it.id,
                                ExecutionAttemptRecord.status == "RUNNING",
                            )
                            .values(
                                status="FAILED",
                                error_type="AMBIGUOUS_OUTCOME",
                                error_message=(
                                    "Worker lease expired after request was dispatched. Non-idempotent agent outcome is ambiguous. Automatic retry prohibited."
                                ),
                                completed_at=now,
                            )
                        )
                        session.execute(att_update)

                    recovered_count += 1
                    runtime_metrics.record_lease_expiry()
                else:
                    # Retryable or idempotent
                    attempt_count = session.scalar(
                        select(func.count(ExecutionAttemptRecord.id)).where(
                            ExecutionAttemptRecord.item_execution_id == it.id
                        )
                    ) or 0

                    if attempt_count <= max_retries:
                        new_gen = it.dispatch_generation + 1
                        item_update = (
                            update(ExperimentItemExecutionRecord)
                            .where(
                                ExperimentItemExecutionRecord.id == it.id,
                                ExperimentItemExecutionRecord.dispatch_generation == it.dispatch_generation,
                                ExperimentItemExecutionRecord.lease_token == it.lease_token,
                                ExperimentItemExecutionRecord.execution_status == "running",
                                ExperimentItemExecutionRecord.lease_expires_at <= now_sql,
                            )
                            .values(
                                execution_status="queued",
                                dispatch_generation=new_gen,
                                queued_at=now,
                                available_at=None,
                                lease_owner=None,
                                lease_token=None,
                                lease_expires_at=None,
                                updated_at=now,
                            )
                        )
                        res = session.execute(item_update)
                        if res.rowcount != 1:
                            continue

                        if active_att:
                            att_update = (
                                update(ExecutionAttemptRecord)
                                .where(
                                    ExecutionAttemptRecord.id == active_att.id,
                                    ExecutionAttemptRecord.item_execution_id == it.id,
                                    ExecutionAttemptRecord.status == "RUNNING",
                                )
                                .values(
                                    status="FAILED",
                                    error_type="LEASE_EXPIRED",
                                    error_message="Worker lease expired. Re-enqueued for retry.",
                                    completed_at=now,
                                )
                            )
                            session.execute(att_update)

                        recovered_count += 1
                        runtime_metrics.record_lease_expiry()
                        reenqueue_items.append((it.id, new_gen))
                    else:
                        item_update = (
                            update(ExperimentItemExecutionRecord)
                            .where(
                                ExperimentItemExecutionRecord.id == it.id,
                                ExperimentItemExecutionRecord.dispatch_generation == it.dispatch_generation,
                                ExperimentItemExecutionRecord.lease_token == it.lease_token,
                                ExperimentItemExecutionRecord.execution_status == "running",
                                ExperimentItemExecutionRecord.lease_expires_at <= now_sql,
                            )
                            .values(
                                execution_status="timed_out",
                                eval_status="skipped",
                                quality_conclusion="unknown",
                                lease_owner=None,
                                lease_token=None,
                                lease_expires_at=None,
                                completed_at=now,
                                updated_at=now,
                            )
                        )
                        res = session.execute(item_update)
                        if res.rowcount != 1:
                            continue

                        if active_att:
                            att_update = (
                                update(ExecutionAttemptRecord)
                                .where(
                                    ExecutionAttemptRecord.id == active_att.id,
                                    ExecutionAttemptRecord.item_execution_id == it.id,
                                    ExecutionAttemptRecord.status == "RUNNING",
                                )
                                .values(
                                    status="FAILED",
                                    error_type="LEASE_EXPIRED",
                                    error_message="Worker lease expired and retry budget exhausted.",
                                    completed_at=now,
                                )
                            )
                            session.execute(att_update)

                        recovered_count += 1
                        runtime_metrics.record_lease_expiry()

            session.commit()

        if reenqueue_items:
            self.queue.enqueue_items(reenqueue_items)

        return recovered_count

    def reconcile_backlog(self, max_items: int = 100, min_idle_seconds: int = 30) -> int:
        """Re-enqueues QUEUED items that have been waiting without claim for too long (e.g. lost Redis messages)."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=min_idle_seconds)
        reenqueued = []

        with self.db_mgr.get_session() as session:
            queued_items = session.scalars(
                select(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.execution_status == "queued",
                    (ExperimentItemExecutionRecord.queued_at.is_(None)) | (ExperimentItemExecutionRecord.queued_at <= cutoff),
                )
                .limit(max_items)
            ).all()

            for it in queued_items:
                it.queued_at = now
                it.updated_at = now
                reenqueued.append((it.id, it.dispatch_generation))

            session.commit()

        if reenqueued:
            self.queue.enqueue_items(reenqueued)

        return len(reenqueued)

    def run_reconcile_cycle(self) -> None:
        """Executes one full pass of all reconciliation recovery actions."""
        self.reconcile_retry_waits()
        self.reconcile_expired_leases()
        self.reconcile_backlog()
        self.reconcile_launch_states()
        self.reconcile_sync_statuses()

    def reconcile_sync_statuses(self) -> int:
        """Compensates sync status aggregation for terminal launches still in SYNCING/PENDING."""
        with self.db_mgr.get_session() as session:
            candidate_launches = session.scalars(
                select(ExperimentLaunchRecord.id).where(
                    ExperimentLaunchRecord.status.in_(TERMINAL_LAUNCH_STATUSES),
                    ExperimentLaunchRecord.langfuse_sync_status.in_(["PENDING", "SYNCING"]),
                )
            ).all()

        updated = 0
        for lid in candidate_launches:
            try:
                aggregate_launch_sync_status(self.db_mgr, lid)
                updated += 1
            except Exception:
                pass
        return updated

    def reconcile_launch_states(self) -> int:
        """Checks RUNNING or CANCELLING launches; transitions to terminal status when all items finish."""
        now = datetime.now(UTC)
        updated_count = 0
        finalized_launch_ids = []

        with self.db_mgr.get_session() as session:
            # 1. Reconcile terminal launches that have active items (Invariant 1)
            terminal_launches = session.scalars(
                select(ExperimentLaunchRecord).where(
                    ExperimentLaunchRecord.status.in_(TERMINAL_LAUNCH_STATUSES)
                )
            ).all()

            for t_launch in terminal_launches:
                active_items = session.scalars(
                    select(ExperimentItemExecutionRecord)
                    .where(
                        ExperimentItemExecutionRecord.launch_id == t_launch.id,
                        ExperimentItemExecutionRecord.execution_status.in_(["pending", "queued", "running", "retry_wait"]),
                    )
                ).all()
                if not active_items:
                    continue

                def _is_lease_valid(exp_at: datetime | None) -> bool:
                    if exp_at is None:
                        return False
                    if exp_at.tzinfo is None:
                        return exp_at > now.replace(tzinfo=None)
                    return exp_at > now

                has_valid_lease = any(
                    it.execution_status == "running" and _is_lease_valid(it.lease_expires_at)
                    for it in active_items
                )
                if has_valid_lease:
                    # Legitimate running task found: revert Launch to RUNNING / CANCELLING
                    t_launch.status = "CANCELLING" if t_launch.cancel_requested_at else "RUNNING"
                    t_launch.completed_at = None
                    t_launch.updated_at = now
                    updated_count += 1
                else:
                    # 1. Evidence-first: attempt to fetch Langfuse evidence for this terminal launch
                    evidence_map = self._fetch_langfuse_evidence(t_launch)

                    target_status = (
                        "cancelled"
                        if (t_launch.status == "CANCELLED" or t_launch.cancel_requested_at)
                        else "failed"
                    )
                    for it in active_items:
                        item_evidence = evidence_map.get(it.dataset_item_id) if evidence_map else None
                        if item_evidence is not None and target_status != "cancelled":
                            # Evidence Found: Recover legitimate historical execution facts!
                            it.execution_status = "succeeded"
                            it.eval_status = "succeeded"
                            it.trace_id = getattr(item_evidence, "trace_id", None) or it.trace_id
                            if t_launch.quality_conclusion in ("pass", "fail"):
                                it.quality_conclusion = t_launch.quality_conclusion
                            else:
                                it.quality_conclusion = "pass"
                            it.execution_error = None
                            it.eval_error = None
                        else:
                            # Fallback: Converge active orphans without evidence to failed / unknown
                            it.execution_status = target_status
                            it.eval_status = "skipped"
                            it.quality_conclusion = "unknown"
                            it.execution_error = "Reconciled legacy orphan item without execution (no Langfuse evidence)"

                        it.lease_owner = None
                        it.lease_token = None
                        it.lease_expires_at = None
                        it.completed_at = now
                        it.updated_at = now

                        att_status = (
                            "CANCELLED"
                            if target_status == "cancelled"
                            else ("COMPLETED" if it.execution_status == "succeeded" else "FAILED")
                        )
                        session.execute(
                            update(ExecutionAttemptRecord)
                            .where(
                                ExecutionAttemptRecord.item_execution_id == it.id,
                                ExecutionAttemptRecord.status == "RUNNING",
                            )
                            .values(
                                status=att_status,
                                error_message=None if att_status == "COMPLETED" else "Reconciled orphan attempt",
                                completed_at=now,
                            )
                        )
                        updated_count += 1

                    session.flush()

                    # Re-aggregate Launch status and quality from all items to maintain Invariants 2 & 3
                    counts_res = session.execute(
                        select(
                            ExperimentItemExecutionRecord.execution_status,
                            func.count(ExperimentItemExecutionRecord.id),
                        )
                        .where(ExperimentItemExecutionRecord.launch_id == t_launch.id)
                        .group_by(ExperimentItemExecutionRecord.execution_status)
                    ).all()
                    counts = {row[0].lower(): row[1] for row in counts_res}

                    quality_res = session.execute(
                        select(
                            ExperimentItemExecutionRecord.quality_conclusion,
                            func.count(ExperimentItemExecutionRecord.id),
                        )
                        .where(ExperimentItemExecutionRecord.launch_id == t_launch.id)
                        .group_by(ExperimentItemExecutionRecord.quality_conclusion)
                    ).all()
                    quality_counts = {row[0].lower(): row[1] for row in quality_res}

                    term_status, term_quality = aggregate_launch_status_from_items(counts, quality_counts)
                    t_launch.status = term_status
                    t_launch.quality_conclusion = term_quality
                    t_launch.updated_at = now

                    active_cnt = sum(
                        counts.get(s, 0)
                        for s in ("pending", "queued", "running", "retry_wait")
                    )
                    assert_terminal_launch_invariants(term_status, active_item_count=active_cnt)

            active_launches = session.scalars(
                select(ExperimentLaunchRecord).where(
                    ExperimentLaunchRecord.status.in_(["RUNNING", "CANCELLING", "QUEUED"])
                )
            ).all()


            for launch in active_launches:
                is_cancelling = bool(launch.cancel_requested_at or launch.status == "CANCELLING")

                counts_res = session.execute(
                    select(
                        ExperimentItemExecutionRecord.execution_status,
                        func.count(ExperimentItemExecutionRecord.id),
                    )
                    .where(ExperimentItemExecutionRecord.launch_id == launch.id)
                    .group_by(ExperimentItemExecutionRecord.execution_status)
                ).all()

                counts = {row[0].lower(): row[1] for row in counts_res}

                # If cancelling and in-flight invocations are quiescent, cancel remaining queued/pending items
                if is_cancelling and counts.get("running", 0) == 0 and counts.get("retry_wait", 0) == 0:
                    cancelled_updated = session.execute(
                        update(ExperimentItemExecutionRecord)
                        .where(
                            ExperimentItemExecutionRecord.launch_id == launch.id,
                            ExperimentItemExecutionRecord.execution_status.in_(["pending", "queued"]),
                        )
                        .values(
                            execution_status="cancelled",
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                    if cancelled_updated.rowcount > 0:
                        # Refresh counts
                        counts_res = session.execute(
                            select(
                                ExperimentItemExecutionRecord.execution_status,
                                func.count(ExperimentItemExecutionRecord.id),
                            )
                            .where(ExperimentItemExecutionRecord.launch_id == launch.id)
                            .group_by(ExperimentItemExecutionRecord.execution_status)
                        ).all()
                        counts = {row[0].lower(): row[1] for row in counts_res}

                active_items = (
                    counts.get("pending", 0)
                    + counts.get("queued", 0)
                    + counts.get("running", 0)
                    + counts.get("retry_wait", 0)
                )

                quality_res = session.execute(
                    select(
                        ExperimentItemExecutionRecord.quality_conclusion,
                        func.count(ExperimentItemExecutionRecord.id),
                    )
                    .where(ExperimentItemExecutionRecord.launch_id == launch.id)
                    .group_by(ExperimentItemExecutionRecord.quality_conclusion)
                ).all()
                quality_counts = {
                    str(row[0]).lower(): row[1] for row in quality_res if row[0] is not None
                }

                if (active_items == 0 and sum(counts.values()) > 0) or (
                    is_cancelling and counts.get("running", 0) == 0 and counts.get("retry_wait", 0) == 0
                ):
                    term_status, term_quality = aggregate_launch_status_from_items(
                        counts, quality_counts, is_cancelling=is_cancelling
                    )
                    launch.status = term_status
                    launch.quality_conclusion = term_quality
                    launch.completed_at = now
                    launch.updated_at = now
                    updated_count += 1
                    finalized_launch_ids.append(launch.id)

            session.commit()

        for lid in finalized_launch_ids:
            try:
                aggregate_launch_sync_status(self.db_mgr, lid)
            except Exception:
                pass

        return updated_count
