from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, or_, select, update

from .db import DatabaseManager
from .db_models import (
    EvaluationAttemptRecord,
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseSyncTaskRecord,
)
from .langfuse_links import UPDATED
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
        # Link backfill scheduling state (in-process only; no migration required).
        self._link_backoff: dict[str, tuple[float, str]] = {}
        self._link_backoff_delays: dict[str, int] = {}
        self._link_cursor: str | None = None

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

    def reconcile_expired_evaluation_leases(self) -> int:
        """Re-enqueues evaluation-only recovery whose worker lease expired (#84).

        A crashed evaluation worker leaves the item ``evaluating`` with a stale
        lease. The stored Agent output is untouched, so recovery simply re-dispatches
        the same evaluation generation; the fenced claim guarantees at most one
        effective run and no duplicate Agent call.
        """
        from .evaluation_recovery import EVALUATION_RUNNING, WORK_TYPE_EVALUATION

        now = datetime.now(UTC)
        reenqueue: list[tuple[str, int]] = []
        recovered = 0

        with self.db_mgr.get_session() as session:
            is_pg = session.bind.dialect.name == "postgresql"
            now_sql = func.clock_timestamp() if is_pg else func.now()

            expired = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.evaluation_status == EVALUATION_RUNNING,
                    ExperimentItemExecutionRecord.evaluation_lease_expires_at.isnot(None),
                    ExperimentItemExecutionRecord.evaluation_lease_expires_at <= now_sql,
                )
            ).all()

            for it in expired:
                it.evaluation_lease_owner = None
                it.evaluation_lease_token = None
                it.evaluation_lease_expires_at = None
                it.evaluation_error = "EVALUATION_LEASE_EXPIRED: 评测工作进程中断，已重新调度（Agent 输出未变）。"
                it.updated_at = now
                # Mark the interrupted attempt as failed so the audit trail is honest.
                session.execute(
                    update(EvaluationAttemptRecord)
                    .where(
                        EvaluationAttemptRecord.item_execution_id == it.id,
                        EvaluationAttemptRecord.evaluation_generation == it.evaluation_generation,
                        EvaluationAttemptRecord.status == "running",
                    )
                    .values(
                        status="failed",
                        error_type="LEASE_EXPIRED",
                        error_message="Evaluation worker lease expired; re-dispatched.",
                        completed_at=now,
                    )
                )
                reenqueue.append((it.id, it.evaluation_generation))
                recovered += 1

            session.commit()

        if reenqueue:
            self.queue.enqueue_items(reenqueue, work_type=WORK_TYPE_EVALUATION)

        return recovered

    def run_reconcile_cycle(self) -> None:
        """Executes one full pass of all reconciliation recovery actions."""
        self.reconcile_retry_waits()
        self.reconcile_expired_leases()
        self.reconcile_expired_evaluation_leases()
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

    # -- Langfuse link backfill -------------------------------------------
    # Escalating retry ladder. Its cumulative horizon (5/15/35/75/135s) is what the
    # Console backfill polling window has to outlast, otherwise the link is persisted
    # after the open view already stopped polling. See LINK_BACKFILL_WINDOW_MS in
    # services/console/src/features/launches/useLaunchPolling.ts; the two are asserted
    # to stay in sync by test_console_polling_window_outlasts_the_backoff_ladder.
    _LINK_BACKOFF_STEPS = (5.0, 10.0, 20.0, 40.0, 60.0)
    _LINK_BACKOFF_MAX_ENTRIES = 1024

    def _link_candidate_ids(self, after_id: str | None, limit: int) -> list[str]:
        """Terminal launches with a persisted run id, or with a current-generation SYNCED
        task carrying run evidence, and no link yet. Keyset paginated by primary key."""
        current_gen_task = (
            select(LangfuseSyncTaskRecord.id)
            .join(
                ExperimentItemExecutionRecord,
                ExperimentItemExecutionRecord.id == LangfuseSyncTaskRecord.item_id,
            )
            .where(
                LangfuseSyncTaskRecord.launch_id == ExperimentLaunchRecord.id,
                LangfuseSyncTaskRecord.dispatch_generation
                == ExperimentItemExecutionRecord.dispatch_generation,
                LangfuseSyncTaskRecord.status == "SYNCED",
            )
        )
        stmt = (
            select(ExperimentLaunchRecord.id)
            .where(
                ExperimentLaunchRecord.status.in_(TERMINAL_LAUNCH_STATUSES),
                ExperimentLaunchRecord.langfuse_experiment_url.is_(None),
                or_(
                    ExperimentLaunchRecord.langfuse_experiment_id.is_not(None),
                    exists(current_gen_task),
                ),
            )
            .order_by(ExperimentLaunchRecord.id.asc())
            .limit(limit)
        )
        if after_id:
            stmt = stmt.where(ExperimentLaunchRecord.id > after_id)
        with self.db_mgr.get_session() as session:
            return [str(x) for x in session.scalars(stmt).all()]

    def _link_note_failure(self, launch_id: str, signature: str, now: float) -> None:
        """Escalating 5/10/20/40/60s backoff bound to the current evidence signature."""
        previous = self._link_backoff.get(launch_id)
        used = 0
        if previous is not None and previous[1] == signature:
            used = self._link_backoff_delays.get(launch_id, 0)
        delay = self._LINK_BACKOFF_STEPS[min(used, len(self._LINK_BACKOFF_STEPS) - 1)]
        self._link_backoff_delays[launch_id] = used + 1
        self._link_backoff[launch_id] = (now + delay, signature)
        while len(self._link_backoff) > self._LINK_BACKOFF_MAX_ENTRIES:
            # Evict from both maps: leaving the delay counter behind would grow
            # `_link_backoff_delays` without bound.
            evicted = next(iter(self._link_backoff))
            self._link_backoff.pop(evicted)
            self._link_backoff_delays.pop(evicted, None)

    def _link_clear(self, launch_id: str) -> None:
        self._link_backoff.pop(launch_id, None)
        self._link_backoff_delays.pop(launch_id, None)

    def reconcile_langfuse_links(
        self,
        link_service: Any | None = None,
        *,
        batch_size: int = 10,
        budget_seconds: float = 10.0,
    ) -> int:
        """Backfills missing Langfuse links for terminal launches. Runs outside the blocking
        execution recovery cycle and never modifies sync facts, tasks, traces or scores."""
        if link_service is None:
            return 0

        deadline = time.monotonic() + float(budget_seconds)
        cursor = self._link_cursor
        candidate_ids = self._link_candidate_ids(cursor, batch_size)
        if not candidate_ids and cursor is not None:
            # Exhausted this pass: wrap so later launches are never starved.
            self._link_cursor = None
            candidate_ids = self._link_candidate_ids(None, batch_size)

        updated = 0
        # Highest candidate id actually examined this pass. The cursor must never move
        # past an unvisited candidate: skipping the tail delays those launches until the
        # whole keyspace wraps, and starves them outright when new ids keep arriving
        # past the cursor.
        last_visited: str | None = None
        truncated = False
        for launch_id in candidate_ids:
            now = time.monotonic()
            entry = self._link_backoff.get(launch_id)
            if entry is not None and entry[0] > now:
                # Skipped without remote work, but still examined: safe to advance past.
                last_visited = launch_id
                continue
            # The budget bounds the whole pass. Gating it on `updated > 0` let a batch
            # where every lookup fails run all sequential remote calls, each with its
            # own timeout, far beyond the advertised budget.
            if now >= deadline:
                truncated = True
                break
            last_visited = launch_id
            try:
                result = link_service.ensure_launch_link(launch_id)
            except Exception:
                self._link_note_failure(launch_id, "error", time.monotonic())
                continue
            status = getattr(result, "status", None)
            if status == UPDATED:
                updated += 1
                self._link_clear(launch_id)
            elif status == "UNCHANGED":
                self._link_clear(launch_id)
            else:
                self._link_note_failure(
                    launch_id, f"{status}:{getattr(result, 'run_id', None)}", time.monotonic()
                )

        if truncated:
            # Resume from the last examined candidate so the unvisited tail is retried on
            # the next pass. If nothing was examined the cursor is left untouched.
            self._link_cursor = last_visited if last_visited is not None else cursor
        else:
            self._link_cursor = candidate_ids[-1] if len(candidate_ids) >= batch_size else None
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

            # Freeze new result revisions and recover terminal launches left without a snapshot
            # after a process crash. Digest uniqueness keeps this scan idempotent.
            session.flush()
            from .db_models import RunResultSnapshotRecord
            from .result_snapshots import create_result_snapshot

            snapshot_exists = select(RunResultSnapshotRecord.id).where(
                RunResultSnapshotRecord.launch_id == ExperimentLaunchRecord.id
            ).exists()
            snapshot_for_current_completion = select(RunResultSnapshotRecord.id).where(
                RunResultSnapshotRecord.launch_id == ExperimentLaunchRecord.id,
                RunResultSnapshotRecord.created_at >= ExperimentLaunchRecord.completed_at,
            ).exists()
            terminal_stmt = select(ExperimentLaunchRecord).where(
                ExperimentLaunchRecord.status.in_(TERMINAL_LAUNCH_STATUSES),
                (~snapshot_exists) | (~snapshot_for_current_completion),
            )
            if self.db_mgr.engine.dialect.name == "postgresql":
                terminal_stmt = terminal_stmt.with_for_update(skip_locked=True)
            terminal_launches_for_snapshots = session.scalars(terminal_stmt).all()
            for terminal_launch in terminal_launches_for_snapshots:
                try:
                    with session.begin_nested():
                        create_result_snapshot(session, terminal_launch)
                except Exception:
                    # Keep lifecycle reconciliation healthy; a later cycle retries the materialization.
                    continue

            session.commit()

        for lid in finalized_launch_ids:
            try:
                aggregate_launch_sync_status(self.db_mgr, lid)
            except Exception:
                pass

        return updated_count
