from __future__ import annotations

import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from .db import DatabaseManager
from .db_models import LangfuseSyncTaskRecord

logger = logging.getLogger("argus.langfuse_sync")


class LangfuseOutboxSyncer:
    """Outbox syncer for Langfuse: reliably claims pending or crashed tasks,
    makes remote HTTP calls with explicit dataset_version & stable score IDs,
    and strongly confirms results before transitioning to SYNCED.
    """

    def __init__(
        self,
        db_mgr: DatabaseManager,
        langfuse_client: Any | None = None,
        syncer_id: str | None = None,
        lease_duration_seconds: int = 30,
        max_attempts: int = 5,
    ):
        self.db_mgr = db_mgr
        self.lf = langfuse_client
        self.syncer_id = syncer_id or f"syncer-{uuid.uuid4().hex[:8]}"
        self.lease_duration_seconds = lease_duration_seconds
        self.max_attempts = max_attempts

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
            # Try with_for_update if supported by dialect
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

    def process_single_task(self, task_id: str, claim_token: str, payload: dict[str, Any]) -> bool:
        """Executes remote HTTP sync calls outside of DB transactions and strongly verifies responses."""
        if not self.lf or not hasattr(self.lf, "api"):
            logger.warning("Langfuse client or api is unavailable, skipping task %s", task_id)
            return False

        error_msg = None
        try:
            # 1. Dataset Run Item linking
            kwargs: dict[str, Any] = {
                "run_name": payload["dataset_run_name"],
                "dataset_item_id": payload["dataset_item_id"],
                "trace_id": payload["trace_id"],
                "observation_id": payload.get("observation_id"),
            }
            if payload.get("dataset_version"):
                kwargs["dataset_version"] = payload["dataset_version"]

            self.lf.api.dataset_run_items.create(**kwargs)

            # 2. Score creations with stable versioned ID
            scores = payload.get("scores_payload", {})
            for ev_id, score_val in scores.items():
                stable_score_id = f"score:{payload['item_id']}:gen{payload['dispatch_generation']}:{ev_id}"
                self.lf.api.scores.create(
                    id=stable_score_id,
                    name=ev_id,
                    value=float(score_val),
                    trace_id=payload["trace_id"],
                    observation_id=payload.get("observation_id"),
                )

        except Exception as exc:
            error_msg = str(exc)
            logger.exception("Failed to sync Langfuse task %s: %s", task_id, error_msg)

        # Write-back short transaction
        now = datetime.now(UTC)
        with self.db_mgr.get_session() as session:
            task = session.get(LangfuseSyncTaskRecord, task_id)
            if not task or task.claim_token != claim_token or task.status != "PROCESSING":
                # Lost lease ownership! Abort write-back
                logger.warning("Syncer %s lost claim token on task %s", self.syncer_id, task_id)
                return False

            if error_msg is None:
                # Strong confirmation succeeded
                task.status = "SYNCED"
                task.claim_token = None
                task.lease_expires_at = None
                task.last_error = None
                task.updated_at = now
                session.commit()
                return True
            else:
                task.attempts += 1
                task.last_error = error_msg
                task.claim_token = None
                task.lease_expires_at = None
                task.updated_at = now

                if task.attempts >= self.max_attempts:
                    task.status = "FAILED"
                    logger.error("Langfuse task %s permanently failed after %d attempts", task_id, task.attempts)
                else:
                    task.status = "PENDING"
                    # Exponential backoff with jitter
                    backoff = min(60, (2 ** task.attempts)) + random.uniform(0.1, 1.0)
                    task.next_retry_at = now + timedelta(seconds=backoff)

                session.commit()
                return False

    def process_batch(self, batch_size: int = 1) -> int:
        """Claims a batch of tasks and processes each one."""
        claimed = self.claim_tasks(batch_size=batch_size)
        success_count = 0
        for task_id, claim_token, payload in claimed:
            if self.process_single_task(task_id, claim_token, payload):
                success_count += 1
        return success_count
