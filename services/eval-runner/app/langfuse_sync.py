from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update

from .dataset import parse_dataset_version
from .db import DatabaseManager
from .db_models import (
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseSyncTaskRecord,
)
from .langfuse_links import LangfuseLaunchLinkService, select_current_run_evidence
from .langfuse_projection import (
    CombinedSyncState,
    combine_scope_states,
    project_typed_result,
    scope_state_from_tasks,
)
from .state_machine import TERMINAL_LAUNCH_STATUSES

logger = logging.getLogger("argus.langfuse_sync")


def aggregate_launch_sync_status(db_mgr: DatabaseManager, launch_id: str) -> str:
    """Aggregates Langfuse sync status for a launch under an exclusive launch lock.
    Strictly uses per-item generation tracking (item_id + item.dispatch_generation).
    """
    is_pg = db_mgr.engine.dialect.name == "postgresql"
    with db_mgr.get_session() as session:
        # 1. Exclusive lock on Launch
        launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
        if is_pg:
            launch_stmt = launch_stmt.with_for_update()
        launch = session.scalars(launch_stmt).first()
        if not launch:
            return "UNKNOWN"

        launch_is_final = launch.status in TERMINAL_LAUNCH_STATUSES
        if not launch_is_final:
            return launch.langfuse_sync_status

        # 2. Query all items belonging to this launch
        items = session.scalars(
            select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch_id)
        ).all()
        if not items:
            return launch.langfuse_sync_status

        # 3. Fetch all outbox tasks for this launch
        tasks = session.scalars(
            select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch_id)
        ).all()
        tasks_map = {(t.item_id, t.dispatch_generation): t for t in tasks}

        # Current generation task for each item
        current_tasks_map = {
            it.id: tasks_map.get((it.id, it.dispatch_generation))
            for it in items
        }

        # Items that require sync:
        # An item is exempt from outbox sync ONLY if:
        # 1. It has no task materialized, AND
        # 2. It has no trace_id (never invoked agent), AND
        # 3. Its execution terminated without success (failed or cancelled), AND
        # 4. The launch itself is not COMPLETED (a COMPLETED launch expects all items to have executed)
        def _is_exempt(it) -> bool:
            return (
                current_tasks_map[it.id] is None
                and not it.trace_id
                and it.execution_status in ("failed", "cancelled")
                and launch.status != "COMPLETED"
            )

        items_needing_sync = [it for it in items if not _is_exempt(it)]

        # Any required item missing its task -> keep SYNCING
        if any(current_tasks_map[it.id] is None for it in items_needing_sync):
            launch.langfuse_sync_status = "SYNCING"
            session.commit()
            return "SYNCING"

        valid_tasks = [current_tasks_map[it.id] for it in items_needing_sync if current_tasks_map[it.id] is not None]

        # 4. Extract and validate Run IDs from current-generation tasks only.
        # The same selection rule backs the link service, so a link can never point at a
        # different run than the one aggregation persisted.
        run_evidence = select_current_run_evidence(items, tasks)
        if run_evidence.has_conflict:
            raise ValueError(
                f"Conflicting dataset_run_ids found in launch {launch_id}: {run_evidence.conflicting_run_ids}"
            )
        if run_evidence.run_id:
            new_run_id = run_evidence.run_id
            if launch.langfuse_experiment_id != new_run_id:
                # A new run invalidates any previously derived link.
                launch.langfuse_experiment_url = None
                launch.langfuse_experiment_id = new_run_id

        # 5. Check task statuses
        task_statuses = [t.status for t in valid_tasks]

        # - Any valid task FAILED -> Launch sync FAILED
        if any(s == "FAILED" for s in task_statuses):
            launch.langfuse_sync_status = "FAILED"
            launch.langfuse_sync_error = "One or more item sync tasks failed"
            session.commit()
            return "FAILED"

        if any(s in ("PENDING", "PROCESSING") for s in task_statuses):
            launch.langfuse_sync_status = "SYNCING"
            session.commit()
            return "SYNCING"

        # - All valid tasks SYNCED (with at least one SYNCED) -> SYNCED
        if valid_tasks and all(s == "SYNCED" for s in task_statuses):
            launch.langfuse_sync_status = "SYNCED"
            launch.langfuse_sync_error = None
            session.commit()
            return "SYNCED"

        if valid_tasks and all(s in ("SYNCED", "SKIPPED") for s in task_statuses) and any(s == "SYNCED" for s in task_statuses):
            launch.langfuse_sync_status = "SYNCED"
            launch.langfuse_sync_error = None
            session.commit()
            return "SYNCED"

        # - Unconfigured check: to converge to NOT_APPLICABLE, all items needing sync must have a task materialized AND all be SKIPPED
        if items_needing_sync and len(valid_tasks) == len(items_needing_sync) and all(s == "SKIPPED" for s in task_statuses):
            launch.langfuse_sync_status = "NOT_APPLICABLE"
            launch.langfuse_sync_error = None
            session.commit()
            return "NOT_APPLICABLE"

        # No tasks at all and no trace_ids across all items
        if not items_needing_sync and not tasks:
            launch.langfuse_sync_status = "NOT_APPLICABLE"
            launch.langfuse_sync_error = None
            session.commit()
            return "NOT_APPLICABLE"

        launch.langfuse_sync_status = "SYNCING"
        session.commit()
        return "SYNCING"


def _get_db_now(session, is_pg: bool) -> datetime:
    """Retrieves current database timestamp to prevent client clock skew and pre-transaction expiry reuse."""
    if is_pg:
        ts = session.scalar(select(func.clock_timestamp()))
    else:
        raw = session.scalar(select(func.strftime("%Y-%m-%d %H:%M:%f", "now")))
        import datetime as _dt_module
        ts = _dt_module.datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


_MAX_INFLIGHT_REMOTE_CALLS = 16
_REMOTE_CALL_SEMAPHORE = threading.BoundedSemaphore(value=_MAX_INFLIGHT_REMOTE_CALLS)


def _invoke_with_timeout(fn: Callable[..., Any], *args: Any, timeout: float, **kwargs: Any) -> Any:
    """Executes a blocking remote callable with an unyielding hard deadline,
    enforcing a strict concurrency budget on uncompleted remote calls and sharing
    a single deadline budget across slot acquisition and execution.
    """
    if timeout <= 0:
        raise TimeoutError("Timeout budget exhausted before invocation")

    start_time = time.monotonic()
    deadline = start_time + timeout

    sem = _REMOTE_CALL_SEMAPHORE
    # 1. Acquire inflight remote call slot within single shared deadline budget
    rem_to_acquire = deadline - time.monotonic()
    if rem_to_acquire <= 0 or not sem.acquire(timeout=rem_to_acquire):
        raise TimeoutError(
            f"Remote call concurrency limit ({_MAX_INFLIGHT_REMOTE_CALLS}) reached; slot unavailable within {timeout:.3f}s"
        )

    # 2. Check remaining time budget after acquiring slot
    rem_to_call = deadline - time.monotonic()
    if rem_to_call <= 0:
        sem.release()
        raise TimeoutError("Timeout budget exhausted while acquiring concurrency slot")

    res_box: list[Any] = []
    err_box: list[BaseException] = []
    finished = threading.Event()

    def _worker():
        try:
            res_box.append(fn(*args, **kwargs))
        except BaseException as exc:
            err_box.append(exc)
        finally:
            finished.set()
            sem.release()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    # 3. Wait for execution strictly within remaining deadline budget
    if not finished.wait(timeout=rem_to_call):
        raise TimeoutError(f"Remote invocation timed out after {timeout:.3f}s")

    if err_box:
        raise err_box[0]
    return res_box[0]




def launch_sync_breakdown(db_mgr: DatabaseManager, launch_id: str, *, max_attempts: int = 5) -> CombinedSyncState:
    """Read-only, per-scope sync state for one Launch (Issue #87).

    Item/Trace projection and the Run Score outbox are separate scopes with
    separate failure modes, so the UI must be able to say which one is behind
    instead of collapsing them into a single "SYNCED".
    """
    from .db_models import LangfuseRunScoreTaskRecord

    with db_mgr.get_session() as session:
        item_rows = session.scalars(
            select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch_id)
        ).all()
        run_rows = session.scalars(
            select(LangfuseRunScoreTaskRecord).where(LangfuseRunScoreTaskRecord.launch_id == launch_id)
        ).all()
        item_tasks = [
            {"status": row.status, "attempts": row.attempts, "last_error": row.last_error}
            for row in item_rows
        ]
        run_tasks = [
            {"status": row.status, "attempts": row.attempts, "last_error": row.last_error}
            for row in run_rows
        ]

    return combine_scope_states(
        item_trace=scope_state_from_tasks(item_tasks, max_attempts=max_attempts),
        run_score=scope_state_from_tasks(run_tasks, max_attempts=max_attempts),
    )


def _project_scores(
    payload: dict[str, Any],
    *,
    scores: dict[str, Any],
    snapshot_id: str | None,
    revision: int | None,
    policy_digest: str | None,
) -> list[Any]:
    """Turn one frozen result into the Langfuse scores it can honestly produce.

    Typed results are authoritative. A pre-#82 outbox payload without them falls
    back to the legacy numeric ``scores`` map so historical launches still sync.
    """
    item_id = str(payload.get("item_id") or "")
    generation = int(payload.get("dispatch_generation") or 1)
    typed = scores.get("_typed_results")

    if isinstance(typed, list) and typed:
        projected = []
        for entry in typed:
            if not isinstance(entry, dict):
                continue
            provenance = entry.get("provenance") or {}
            score = project_typed_result(
                entry,
                item_id=item_id,
                generation=generation,
                snapshot_id=snapshot_id,
                revision=revision,
                policy_digest=policy_digest,
                definition_digest=provenance.get("definition_digest"),
                attempt=payload.get("attempts"),
            )
            if score.applicable:
                projected.append(score)
        return projected

    projected = []
    for evaluator_id, value in scores.items():
        if evaluator_id.startswith("_"):
            continue
        projected.append(
            project_typed_result(
                {
                    "evaluator_id": evaluator_id,
                    "result_type": "numeric",
                    "status": "succeeded",
                    "value": value,
                },
                item_id=item_id,
                generation=generation,
                snapshot_id=snapshot_id,
                revision=revision,
                policy_digest=policy_digest,
            )
        )
    return [score for score in projected if score.applicable]


class LangfuseOutboxSyncer:
    """Consumes and finalizes Langfuse outbox tasks asynchronously with robust CAS,
    per-claim tokens, heartbeats, and atomic state transitions.
    """

    def __init__(
        self,
        db_mgr: DatabaseManager,
        langfuse_client: Any | Callable[[], Any] | None = None,
        lease_duration_seconds: float = 30.0,
        max_attempts: int = 5,
        syncer_id: str | None = None,
        heartbeat_interval_seconds: float | None = None,
        task_timeout_seconds: float = 300.0,
        *,
        link_service: LangfuseLaunchLinkService | None = None,
    ):
        self.db_mgr = db_mgr
        self.link_service = link_service
        self._lf_provider = langfuse_client
        self.lease_duration_seconds = float(lease_duration_seconds)
        self.max_attempts = int(max_attempts)
        self.syncer_id = syncer_id or f"syncer-{uuid.uuid4().hex[:8]}"
        self.heartbeat_interval_seconds = (
            float(heartbeat_interval_seconds) if heartbeat_interval_seconds is not None else None
        )
        self.task_timeout_seconds = float(task_timeout_seconds)

    def resolve_client_status(self) -> tuple[str, Any | None, str | None]:
        """Resolves the current status of the Langfuse client provider.
        Returns:
            ("READY", client, None)
            ("UNCONFIGURED", None, reason)
            ("ERROR", None, reason)
            ("INCOMPATIBLE", None, reason)
        """
        provider = self._lf_provider
        if provider is None:
            return ("UNCONFIGURED", None, "Langfuse client is not configured")

        if hasattr(provider, "api"):
            client = provider
        elif callable(provider):
            try:
                client = provider()
            except Exception as exc:
                logger.warning("Failed to obtain Langfuse client from provider: %s", exc)
                return ("ERROR", None, f"Client initialization failed: {exc}")

            if client is None:
                return ("UNCONFIGURED", None, "Langfuse client provider returned None")
        else:
            client = provider

        if hasattr(client, "api"):
            return ("READY", client, None)

        # Check if client is an officially disabled Langfuse client (missing public/secret keys)
        if (
            type(client).__name__ == "Langfuse"
            and type(client).__module__.startswith("langfuse")
            and (getattr(client, "_resources", None) is None or getattr(client, "_tracing_enabled", None) is False)
        ):
            return ("UNCONFIGURED", None, "Langfuse SDK client disabled: missing credentials")

        return ("INCOMPATIBLE", None, f"Incompatible Langfuse client {type(client)}: missing 'api' attribute")

    @property
    def lf(self) -> Any | None:
        st, client, _ = self.resolve_client_status()
        return client if st == "READY" else None

    @lf.setter
    def lf(self, client: Any | None) -> None:
        self._lf_provider = client

    def claim_tasks(self, batch_size: int = 1) -> list[tuple[str, str, dict[str, Any]]]:
        """Claims tasks in a short transaction using single claim tokens and crash recovery,
        strictly evaluated against database time.
        """
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        claimed: list[tuple[str, str, dict[str, Any]]] = []

        with self.db_mgr.get_session() as session:
            db_now = _get_db_now(session, is_pg)
            now_expr = func.clock_timestamp() if is_pg else db_now

            # 1. Sweep expired PROCESSING tasks that already reached max_attempts to FAILED using atomic conditional UPDATE
            over_limit_update = (
                update(LangfuseSyncTaskRecord)
                .where(
                    LangfuseSyncTaskRecord.status == "PROCESSING",
                    LangfuseSyncTaskRecord.lease_expires_at <= now_expr,
                    LangfuseSyncTaskRecord.attempts >= self.max_attempts,
                )
                .values(
                    status="FAILED",
                    claim_token=None,
                    lease_expires_at=None,
                    last_error=f"Lease expired after reaching max {self.max_attempts} attempts",
                    updated_at=now_expr,
                )
            )
            session.execute(over_limit_update)

            # 2. Select eligible tasks strictly under max_attempts evaluated against database time
            stmt = (
                select(LangfuseSyncTaskRecord)
                .where(
                    or_(
                        (LangfuseSyncTaskRecord.status == "PENDING")
                        & (LangfuseSyncTaskRecord.next_retry_at <= now_expr)
                        & (LangfuseSyncTaskRecord.attempts < self.max_attempts),
                        (LangfuseSyncTaskRecord.status == "PROCESSING")
                        & (LangfuseSyncTaskRecord.lease_expires_at <= now_expr)
                        & (LangfuseSyncTaskRecord.attempts < self.max_attempts),
                    )
                )
                .order_by(LangfuseSyncTaskRecord.created_at.asc())
                .limit(batch_size)
            )
            if self.db_mgr.engine.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)

            tasks = session.scalars(stmt).all()

            for task in tasks:
                new_token = uuid.uuid4().hex
                if is_pg:
                    lease_exp_expr = func.clock_timestamp() + text(f"interval '{self.lease_duration_seconds} seconds'")
                    updated_expr = func.clock_timestamp()
                else:
                    lease_exp_expr = func.strftime(
                        "%Y-%m-%d %H:%M:%f", "now", f"+{self.lease_duration_seconds} seconds"
                    )
                    updated_expr = func.strftime("%Y-%m-%d %H:%M:%f", "now")

                payload = {
                    "task_id": task.id,
                    "launch_id": task.launch_id,
                    "dataset_run_name": task.dataset_run_name,
                    "dataset_item_id": task.dataset_item_id,
                    "dataset_version": task.dataset_version,
                    "item_id": task.item_id,
                    "dispatch_generation": task.dispatch_generation,
                    "trace_id": task.trace_id,
                    "observation_id": task.observation_id,
                    "scores_payload": dict(task.scores_payload or {}),
                    "attempts": task.attempts + 1,
                }
                claimed.append((task.id, new_token, payload))

                session.execute(
                    update(LangfuseSyncTaskRecord)
                    .where(LangfuseSyncTaskRecord.id == task.id)
                    .values(
                        status="PROCESSING",
                        owner_id=self.syncer_id,
                        claim_token=new_token,
                        lease_expires_at=lease_exp_expr,
                        attempts=LangfuseSyncTaskRecord.attempts + 1,
                        updated_at=updated_expr,
                    )
                )

            session.commit()

        return claimed

    def renew_task_lease(self, task_id: str, claim_token: str, extension_seconds: float = 45.0) -> bool:
        """Renews active lease for an in-flight sync task under row lock and instantaneous clock check."""
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            stmt = select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == task_id)
            if is_pg:
                stmt = stmt.with_for_update()
            task = session.scalars(stmt).first()
            if not task:
                return False

            current_ts = _get_db_now(session, is_pg)
            lease_exp = task.lease_expires_at
            if lease_exp and lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=UTC)

            if (
                task.claim_token != claim_token
                or task.status != "PROCESSING"
                or lease_exp is None
                or lease_exp <= current_ts
            ):
                session.rollback()
                return False

            task.lease_expires_at = current_ts + timedelta(seconds=extension_seconds)
            task.updated_at = current_ts
            session.commit()
            return True

    def _mark_task_skipped(self, task_id: str, claim_token: str, reason: str, launch_id: str) -> bool:
        """Transitions a task to SKIPPED under CAS when Langfuse is explicitly unconfigured."""
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            stmt = select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == task_id)
            if is_pg:
                stmt = stmt.with_for_update()
            task = session.scalars(stmt).first()
            if not task:
                return False

            current_ts = _get_db_now(session, is_pg)
            lease_exp = task.lease_expires_at
            if lease_exp and lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=UTC)

            if (
                task.claim_token != claim_token
                or task.status != "PROCESSING"
                or lease_exp is None
                or lease_exp <= current_ts
            ):
                session.rollback()
                return False

            task.status = "SKIPPED"
            task.claim_token = None
            task.lease_expires_at = None
            task.last_error = reason
            task.updated_at = current_ts
            session.commit()

        try:
            aggregate_launch_sync_status(self.db_mgr, launch_id)
        except Exception as exc:
            logger.exception("Failed to aggregate launch sync status for launch %s: %s", launch_id, exc)
        return True

    def _resolve_snapshot_provenance(self, launch_id: str) -> tuple[str | None, int | None]:
        """The frozen snapshot and revision this projection belongs to.

        Read-only: a missing snapshot (still running, or never frozen) simply
        yields no provenance and never blocks the projection itself.
        """
        try:
            from .result_snapshots import latest_result_snapshot

            with self.db_mgr.get_session() as session:
                snapshot = latest_result_snapshot(session, launch_id)
                if snapshot is None:
                    return None, None
                return snapshot.id, snapshot.revision
        except Exception as exc:  # pragma: no cover - provenance is best effort
            logger.warning("Could not resolve snapshot provenance for launch %s: %s", launch_id, exc)
            return None, None

    def process_single_task(self, task_id: str, claim_token: str, payload: dict[str, Any]) -> bool:
        """Executes remote HTTP sync calls outside of DB transactions, renews lease during execution,
        and strongly verifies responses with post-lock instantaneous clock CAS.
        """
        client_st, lf, err_detail = self.resolve_client_status()

        # Explicitly unconfigured branch -> transition to SKIPPED
        if client_st == "UNCONFIGURED":
            return self._mark_task_skipped(
                task_id, claim_token, err_detail or "Langfuse is not configured", payload["launch_id"]
            )

        error_msg: str | None = None
        run_id: str | None = None
        lease_lost = threading.Event()

        if client_st != "READY" or lf is None:
            error_msg = err_detail or "Langfuse client unavailable"
        else:
            scores = payload.get("scores_payload", {})
            dataset_source = scores.get("_dataset_source")
            snapshot_id, revision = self._resolve_snapshot_provenance(payload["launch_id"])

            stop_hb = threading.Event()
            lease_lost = threading.Event()
            hb_interval = self.heartbeat_interval_seconds or max(0.1, min(5.0, self.lease_duration_seconds / 3.0))
            task_start_time = time.monotonic()

            def _is_timeout_or_lost() -> bool:
                if lease_lost.is_set():
                    return True
                if (time.monotonic() - task_start_time) >= self.task_timeout_seconds:
                    logger.warning(
                        "Syncer %s task %s exceeded deadline (%.3fs >= %.3fs)",
                        self.syncer_id,
                        task_id,
                        time.monotonic() - task_start_time,
                        self.task_timeout_seconds,
                    )
                    lease_lost.set()
                    return True
                return False

            # 1. Pre-flight lease and deadline check before making remote calls
            if _is_timeout_or_lost():
                return False
            if not self.renew_task_lease(task_id, claim_token, extension_seconds=self.lease_duration_seconds):
                logger.warning("Syncer %s pre-flight lease check failed for task %s", self.syncer_id, task_id)
                return False

            def _heartbeat_worker():
                while not stop_hb.is_set():
                    if stop_hb.wait(timeout=hb_interval):
                        break
                    # Total duration check
                    if (time.monotonic() - task_start_time) >= self.task_timeout_seconds:
                        logger.warning(
                            "Syncer %s task %s exceeded max task timeout %ss",
                            self.syncer_id,
                            task_id,
                            self.task_timeout_seconds,
                        )
                        lease_lost.set()
                        break
                    try:
                        ok = self.renew_task_lease(task_id, claim_token, extension_seconds=self.lease_duration_seconds)
                        if not ok:
                            logger.warning(
                                "Syncer %s heartbeat lease renewal rejected for task %s",
                                self.syncer_id,
                                task_id,
                            )
                            lease_lost.set()
                            break
                    except Exception as exc:
                        logger.exception(
                            "Syncer %s heartbeat encountered error on task %s: %s",
                            self.syncer_id,
                            task_id,
                            exc,
                        )
                        lease_lost.set()
                        break

            hb_thread = threading.Thread(target=_heartbeat_worker, daemon=True)
            hb_thread.start()

            def _remaining_timeout() -> float:
                rem = self.task_timeout_seconds - (time.monotonic() - task_start_time)
                return max(0.001, rem)

            try:
                # 1. Dataset Run Item linking (Skip for local seed datasets)
                if dataset_source == "seed":
                    logger.debug("Skipping remote DatasetRunItem association for seed dataset item %s", payload["dataset_item_id"])
                else:
                    # Production Langfuse Dataset: dataset_version MUST be explicitly frozen
                    raw_ver = payload.get("dataset_version")
                    if not raw_ver or str(raw_ver).lower() == "latest":
                        raise ValueError(
                            f"Langfuse dataset requires explicit frozen dataset_version timestamp, got '{raw_ver}'. Silent fallback prohibited."
                        )
                    try:
                        version_val = parse_dataset_version(raw_ver)
                        if version_val is None:
                            version_val = raw_ver
                    except Exception:
                        version_val = raw_ver

                    kwargs: dict[str, Any] = {
                        "run_name": payload["dataset_run_name"],
                        "dataset_item_id": payload["dataset_item_id"],
                        "trace_id": payload["trace_id"],
                        "observation_id": payload.get("observation_id"),
                        "dataset_version": version_val,
                    }
                    res = _invoke_with_timeout(
                        lf.api.dataset_run_items.create,
                        timeout=_remaining_timeout(),
                        **kwargs,
                    )
                    run_id = getattr(res, "dataset_run_id", None)
                    if run_id:
                        logger.debug("Linked DatasetRunItem with remote dataset_run_id=%s", run_id)

                if _is_timeout_or_lost():
                    logger.warning("Syncer %s lost lease or timed out on task %s during linking call, aborting scores", self.syncer_id, task_id)
                    return False
                if not self.renew_task_lease(task_id, claim_token, extension_seconds=self.lease_duration_seconds):
                    logger.warning("Syncer %s failed to renew lease on task %s after linking, aborting scores", self.syncer_id, task_id)
                    return False

                # 2. Score creations with stable versioned ID (Issue #87)
                # Typed results are projected with their real semantics; a type
                # Langfuse cannot represent is recorded as NOT_APPLICABLE and is
                # never written as a number.
                projected = _project_scores(
                    payload,
                    scores=scores,
                    snapshot_id=snapshot_id,
                    revision=revision,
                    policy_digest=scores.get("_policy_digest"),
                )
                for projected_score in projected:
                    if _is_timeout_or_lost():
                        logger.warning("Syncer %s lost lease or timed out on task %s before score upload, aborting", self.syncer_id, task_id)
                        return False
                    _invoke_with_timeout(
                        lf.api.scores.create,
                        timeout=_remaining_timeout(),
                        id=projected_score.score_id,
                        name=projected_score.evaluator_id,
                        value=projected_score.value,
                        trace_id=payload["trace_id"],
                        observation_id=payload.get("observation_id"),
                        comment=projected_score.comment,
                        metadata=projected_score.evidence,
                    )
                    if _is_timeout_or_lost():
                        logger.warning("Syncer %s lost lease or timed out on task %s during score upload, aborting", self.syncer_id, task_id)
                        return False

            except TimeoutError as exc:
                lease_lost.set()
                error_msg = f"Task execution deadline exceeded: {exc}"
                logger.warning("Syncer %s task %s timed out during remote call: %s", self.syncer_id, task_id, exc)
            except Exception as exc:
                error_msg = str(exc)
                logger.exception("Failed to sync Langfuse task %s: %s", task_id, error_msg)
            finally:
                stop_hb.set()
                hb_thread.join(timeout=0.5)

            if _is_timeout_or_lost():
                logger.warning(
                    "Syncer %s lease lost or timed out during execution for task %s, aborting write-back",
                    self.syncer_id,
                    task_id,
                )
                return False

        # 3. Post-lock instantaneous clock CAS write-back
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        task_succeeded = False

        with self.db_mgr.get_session() as session:
            stmt = select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == task_id)
            if is_pg:
                stmt = stmt.with_for_update()
            task = session.scalars(stmt).first()
            if not task:
                return False

            current_ts = _get_db_now(session, is_pg)
            lease_exp = task.lease_expires_at
            if lease_exp and lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=UTC)

            if (
                task.claim_token != claim_token
                or task.status != "PROCESSING"
                or lease_exp is None
                or lease_exp <= current_ts
            ):
                logger.warning("Syncer %s lost lease ownership on task %s", self.syncer_id, task_id)
                session.rollback()
                return False

            if error_msg is None:
                task.status = "SYNCED"
                task.claim_token = None
                task.lease_expires_at = None
                task.last_error = None
                task.updated_at = current_ts
                if run_id:
                    updated_scores = dict(task.scores_payload or {})
                    updated_scores["_dataset_run_id"] = str(run_id)
                    task.scores_payload = updated_scores
                task_succeeded = True
            else:
                # Do NOT increment attempts here! attempts is claim-count only!
                task.claim_token = None
                task.lease_expires_at = None
                task.last_error = error_msg
                task.updated_at = current_ts
                if task.attempts >= self.max_attempts:
                    task.status = "FAILED"
                    logger.error("Langfuse task %s permanently failed after %d attempts: %s", task_id, task.attempts, error_msg)
                else:
                    task.status = "PENDING"
                    backoff = min(60, (2 ** task.attempts)) + random.uniform(0.1, 1.0)
                    task.next_retry_at = current_ts + timedelta(seconds=backoff)

            session.commit()

        # 4. Independent launch status aggregation (separate transaction with Launch lock first)
        try:
            aggregate_launch_sync_status(self.db_mgr, payload["launch_id"])
        except Exception as exc:
            logger.exception("Failed to aggregate launch sync status for launch %s: %s", payload["launch_id"], exc)

        # 5. Independent link backfill. A link failure never changes task_succeeded, never
        # resends traces/scores and never rewrites the sync status.
        if task_succeeded and self.link_service is not None:
            try:
                self.link_service.ensure_launch_link(payload["launch_id"])
            except Exception as exc:
                logger.warning(
                    "Langfuse link backfill failed for launch %s (non-fatal): %s",
                    payload["launch_id"],
                    exc,
                )

        return task_succeeded

    def process_batch(self, batch_size: int = 1) -> int:
        """Claims a batch of tasks (default 1) and processes each one."""
        claimed = self.claim_tasks(batch_size=batch_size)
        success_count = 0
        for task_id, claim_token, payload in claimed:
            if self.process_single_task(task_id, claim_token, payload):
                success_count += 1
        return success_count
