from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from .dataset import parse_dataset_version
from .db import DatabaseManager
from .db_models import (
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseSyncTaskRecord,
)

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

        # 2. Query all items belonging to this launch
        items = session.scalars(
            select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.launch_id == launch_id)
        ).all()
        if not items:
            return launch.langfuse_sync_status

        launch_is_final = launch.status in ("COMPLETED", "FAILED", "CANCELLED")

        # 3. Determine required Outbox keys based on items' valid accepted generation
        # Items with a trace_id must have an Outbox task for their current accepted generation.
        # Items cancelled before any agent invocation (no trace_id) are not applicable.
        required_keys: set[tuple[str, int]] = set()
        for it in items:
            if it.trace_id:
                required_keys.add((it.id, it.dispatch_generation))

        if not required_keys:
            if launch_is_final:
                launch.langfuse_sync_status = "NOT_APPLICABLE"
                session.commit()
            return launch.langfuse_sync_status

        # 4. Fetch all outbox tasks for this launch
        tasks = session.scalars(
            select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.launch_id == launch_id)
        ).all()
        tasks_map = {(t.item_id, t.dispatch_generation): t for t in tasks}

        missing_keys = [k for k in required_keys if k not in tasks_map]
        task_statuses = [tasks_map[k].status for k in required_keys if k in tasks_map]

        # 5. Determine aggregated status:
        # - Any task FAILED -> FAILED
        # - Not final, or missing tasks, or any task PENDING / PROCESSING -> SYNCING
        # - Final and all required tasks present and all SYNCED -> SYNCED
        if any(s == "FAILED" for s in task_statuses):
            new_status = "FAILED"
            launch.langfuse_sync_status = new_status
            launch.langfuse_sync_error = "One or more item sync tasks failed"
        elif not launch_is_final or missing_keys or any(s in ("PENDING", "PROCESSING") for s in task_statuses):
            new_status = "SYNCING"
            launch.langfuse_sync_status = new_status
        elif all(s == "SYNCED" for s in task_statuses):
            new_status = "SYNCED"
            launch.langfuse_sync_status = new_status
            launch.langfuse_sync_error = None
        else:
            new_status = "SYNCING"
            launch.langfuse_sync_status = new_status

        session.commit()
        return new_status


class LangfuseOutboxSyncer:
    """Outbox syncer for Langfuse: reliably claims pending or crashed tasks,
    makes remote HTTP calls with explicit dataset_version & stable score IDs,
    supports in-task lease renewal for slow multi-metric jobs,
    and strongly confirms results using post-lock instantaneous clock CAS before transitioning to SYNCED.
    """

    def __init__(
        self,
        db_mgr: DatabaseManager,
        langfuse_client: Any | Callable[[], Any] | None = None,
        syncer_id: str | None = None,
        lease_duration_seconds: int = 30,
        max_attempts: int = 5,
    ):
        self.db_mgr = db_mgr
        self._lf_provider = langfuse_client
        self.syncer_id = syncer_id or f"syncer-{uuid.uuid4().hex[:8]}"
        self.lease_duration_seconds = lease_duration_seconds
        self.max_attempts = max_attempts

    @property
    def lf(self) -> Any | None:
        if self._lf_provider is not None and hasattr(self._lf_provider, "api"):
            return self._lf_provider
        if callable(self._lf_provider):
            try:
                return self._lf_provider()
            except Exception as exc:
                logger.warning("Failed to obtain Langfuse client from provider: %s", exc)
                return None
        return self._lf_provider

    @lf.setter
    def lf(self, client: Any | None) -> None:
        self._lf_provider = client

    def claim_tasks(self, batch_size: int = 1) -> list[tuple[str, str, dict[str, Any]]]:
        """Claims tasks in a short transaction using single claim tokens and crash recovery."""
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=self.lease_duration_seconds)
        claimed: list[tuple[str, str, dict[str, Any]]] = []

        with self.db_mgr.get_session() as session:
            stmt = (
                select(LangfuseSyncTaskRecord)
                .where(
                    or_(
                        (LangfuseSyncTaskRecord.status == "PENDING")
                        & (LangfuseSyncTaskRecord.next_retry_at <= now)
                        & (LangfuseSyncTaskRecord.attempts < self.max_attempts),
                        (LangfuseSyncTaskRecord.status == "PROCESSING")
                        & (LangfuseSyncTaskRecord.lease_expires_at <= now),
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
                task.status = "PROCESSING"
                task.owner_id = self.syncer_id
                task.claim_token = new_token
                task.lease_expires_at = lease_expires
                task.updated_at = now

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
                    "scores_payload": task.scores_payload or {},
                    "attempts": task.attempts,
                }
                claimed.append((task.id, new_token, payload))

            session.commit()

        return claimed

    def renew_task_lease(self, task_id: str, claim_token: str, extension_seconds: int = 30) -> bool:
        """Renews active lease for an in-flight sync task under row lock and instantaneous clock check."""
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            stmt = select(LangfuseSyncTaskRecord).where(LangfuseSyncTaskRecord.id == task_id)
            if is_pg:
                stmt = stmt.with_for_update()
            task = session.scalars(stmt).first()
            if not task:
                return False

            current_ts = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=UTC)
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

    def process_single_task(self, task_id: str, claim_token: str, payload: dict[str, Any]) -> bool:
        """Executes remote HTTP sync calls outside of DB transactions, renews lease during execution,
        and strongly verifies responses with post-lock instantaneous clock CAS.
        """
        lf = self.lf
        if not lf or not hasattr(lf, "api"):
            logger.warning("Langfuse client or api is unavailable, skipping task %s", task_id)
            return False

        error_msg = None
        scores = payload.get("scores_payload", {})
        dataset_source = scores.get("_dataset_source")

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
                res = lf.api.dataset_run_items.create(**kwargs)
                run_id = getattr(res, "dataset_run_id", None)
                if run_id:
                    logger.debug("Linked DatasetRunItem with remote dataset_run_id=%s", run_id)

            # In-task lease renewal check before scores synchronization
            self.renew_task_lease(task_id, claim_token, extension_seconds=self.lease_duration_seconds)

            # 2. Score creations with stable versioned ID
            clean_scores = {k: v for k, v in scores.items() if not k.startswith("_")}
            for ev_id, score_val in clean_scores.items():
                stable_score_id = f"score:{payload['item_id']}:gen{payload['dispatch_generation']}:{ev_id}"
                lf.api.scores.create(
                    id=stable_score_id,
                    name=ev_id,
                    value=float(score_val),
                    trace_id=payload["trace_id"],
                    observation_id=payload.get("observation_id"),
                )
                self.renew_task_lease(task_id, claim_token, extension_seconds=self.lease_duration_seconds)

        except Exception as exc:
            error_msg = str(exc)
            logger.exception("Failed to sync Langfuse task %s: %s", task_id, error_msg)

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

            current_ts = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=UTC)
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
                task_succeeded = True
            else:
                task.attempts += 1
                task.last_error = error_msg
                task.claim_token = None
                task.lease_expires_at = None
                task.updated_at = current_ts

                if task.attempts >= self.max_attempts:
                    task.status = "FAILED"
                    logger.error("Langfuse task %s permanently failed after %d attempts", task_id, task.attempts)
                else:
                    task.status = "PENDING"
                    backoff = min(60, (2 ** task.attempts)) + random.uniform(0.1, 1.0)
                    task.next_retry_at = current_ts + timedelta(seconds=backoff)

            session.commit()

        # 4. Independent launch status aggregation
        try:
            aggregate_launch_sync_status(self.db_mgr, payload["launch_id"])
        except Exception as exc:
            logger.exception("Failed to aggregate launch sync status for launch %s: %s", payload["launch_id"], exc)

        return task_succeeded

    def process_batch(self, batch_size: int = 1) -> int:
        """Claims a batch of tasks (default 1) and processes each one."""
        claimed = self.claim_tasks(batch_size=batch_size)
        success_count = 0
        for task_id, claim_token, payload in claimed:
            if self.process_single_task(task_id, claim_token, payload):
                success_count += 1
        return success_count
