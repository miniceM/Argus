from __future__ import annotations

import hashlib
import logging
import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text, update

from .db import DatabaseManager
from .db_models import (
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseRunScoreTaskRecord,
    LangfuseSyncTaskRecord,
    RunResultSnapshotRecord,
)

logger = logging.getLogger("argus.langfuse_run_scores")


class RunScorePublishError(RuntimeError):
    """A score write was not explicitly acknowledged by Langfuse."""


def _http_status_code(error: Exception) -> int | None:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    return status_code if isinstance(status_code, int) else None


def publish_run_score(
    client: Any,
    *,
    score_id: str,
    run_id: str,
    name: str,
    value: float,
    metadata: dict[str, Any],
    timeout_seconds: int = 5,
) -> None:
    response = client.api.scores.create(
        id=score_id,
        dataset_run_id=run_id,
        name=name,
        value=value,
        data_type="NUMERIC",
        metadata=metadata,
        request_options={"timeout_in_seconds": timeout_seconds, "max_retries": 0},
    )
    if getattr(response, "id", None) != score_id:
        raise RunScorePublishError("SCORE_ACK_MISMATCH")


class LangfuseRunScoreSyncer:
    """Publishes immutable run metrics through a separately leased transactional outbox."""

    def __init__(
        self,
        db_mgr: DatabaseManager,
        langfuse_client: Any | Callable[[], Any] | None = None,
        *,
        lease_seconds: int = 30,
        max_attempts: int = 5,
    ):
        self.db_mgr = db_mgr
        self.provider = langfuse_client
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.owner_id = f"run-score-{uuid.uuid4().hex[:8]}"

    def _client(self) -> tuple[Any | None, str | None]:
        try:
            client = self.provider() if callable(self.provider) else self.provider
        except Exception as exc:
            return None, str(exc)
        if client is None:
            return None, None
        api = getattr(client, "api", None)
        scores = getattr(api, "scores", None)
        if scores is None or not callable(getattr(scores, "create", None)):
            return None, "Langfuse client does not expose synchronous api.scores.create"
        if getattr(client, "_resources", True) is None:
            return None, "Langfuse client is not initialized"
        return client, None

    def _claim(self, limit: int) -> list[tuple[str, str, str]]:
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        claimed: list[tuple[str, str, str]] = []
        with self.db_mgr.get_session() as session:
            db_now = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if db_now.tzinfo is None:
                db_now = db_now.replace(tzinfo=UTC)
            now_expr = func.clock_timestamp() if is_pg else db_now
            session.execute(
                update(LangfuseRunScoreTaskRecord)
                .where(
                    LangfuseRunScoreTaskRecord.status == "PROCESSING",
                    LangfuseRunScoreTaskRecord.lease_expires_at <= now_expr,
                    LangfuseRunScoreTaskRecord.attempts >= self.max_attempts,
                )
                .values(status="FAILED", claim_token=None, lease_expires_at=None, last_error="Lease expired at maximum attempts")
            )
            stmt = select(LangfuseRunScoreTaskRecord).where(
                or_(
                    (LangfuseRunScoreTaskRecord.status == "PENDING")
                    & (LangfuseRunScoreTaskRecord.next_retry_at <= now_expr)
                    & (LangfuseRunScoreTaskRecord.attempts < self.max_attempts),
                    (LangfuseRunScoreTaskRecord.status == "PROCESSING")
                    & (LangfuseRunScoreTaskRecord.lease_expires_at <= now_expr)
                    & (LangfuseRunScoreTaskRecord.attempts < self.max_attempts),
                )
            ).order_by(LangfuseRunScoreTaskRecord.created_at).limit(limit)
            if is_pg:
                stmt = stmt.with_for_update(skip_locked=True)
            tasks = session.scalars(stmt).all()
            for task in tasks:
                token = uuid.uuid4().hex
                expiry = (
                    func.clock_timestamp() + text(f"interval '{int(self.lease_seconds)} seconds'")
                    if is_pg
                    else (db_now + timedelta(seconds=self.lease_seconds))
                )
                session.execute(
                    update(LangfuseRunScoreTaskRecord)
                    .where(LangfuseRunScoreTaskRecord.id == task.id)
                    .values(
                        status="PROCESSING",
                        owner_id=self.owner_id,
                        claim_token=token,
                        lease_expires_at=expiry,
                        attempts=LangfuseRunScoreTaskRecord.attempts + 1,
                        updated_at=now_expr,
                    )
                )
                claimed.append((task.id, token, task.snapshot_id))
            session.commit()
        return claimed

    def _finish(
        self,
        task_id: str,
        token: str,
        *,
        status: str,
        error: str | None = None,
        retry: bool = False,
        defer: bool = False,
    ) -> bool:
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            now = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            task = session.scalars(select(LangfuseRunScoreTaskRecord).where(
                LangfuseRunScoreTaskRecord.id == task_id,
                LangfuseRunScoreTaskRecord.claim_token == token,
                LangfuseRunScoreTaskRecord.status == "PROCESSING",
                LangfuseRunScoreTaskRecord.lease_expires_at > now,
            )).first()
            if not task:
                session.rollback()
                return False
            task.claim_token = None
            task.lease_expires_at = None
            task.last_error = error
            task.updated_at = now
            if defer:
                # Dependency polling is not a failed publication attempt.
                task.attempts = max(0, task.attempts - 1)
                task.status = "PENDING"
                task.next_retry_at = now + timedelta(seconds=2)
            elif retry and task.attempts < self.max_attempts:
                task.status = "PENDING"
                task.next_retry_at = now + timedelta(seconds=min(60, 2 ** task.attempts) + random.uniform(0.1, 1.0))
            elif retry:
                task.status = "FAILED"
            else:
                task.status = status
            session.commit()
            return True


    def _renew_lease(self, task_id: str, token: str) -> bool:
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            now = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            stmt = select(LangfuseRunScoreTaskRecord).where(
                LangfuseRunScoreTaskRecord.id == task_id,
                LangfuseRunScoreTaskRecord.claim_token == token,
                LangfuseRunScoreTaskRecord.status == "PROCESSING",
                LangfuseRunScoreTaskRecord.lease_expires_at > now,
            )
            if is_pg:
                stmt = stmt.with_for_update()
            task = session.scalars(stmt).first()
            if task is None:
                session.rollback()
                return False
            task.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            task.updated_at = now
            session.commit()
            return True

    def _dataset_run_id(self, launch_id: str) -> tuple[str | None, bool, bool]:
        """Return the linked DatasetRun id, whether item sync is terminal, and whether it failed."""
        with self.db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            persisted_run_id = (
                launch.langfuse_experiment_id
                if launch and launch.langfuse_sync_status == "SYNCED"
                else None
            )
            items = session.scalars(select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == launch_id
            )).all()
            current_generations = {item.id: item.dispatch_generation for item in items}
            tasks = session.scalars(select(LangfuseSyncTaskRecord).where(
                LangfuseSyncTaskRecord.launch_id == launch_id
            )).all()
        current_tasks = [
            task for task in tasks
            if current_generations.get(task.item_id) == task.dispatch_generation
        ]
        run_ids = {
            (task.scores_payload or {}).get("_dataset_run_id")
            for task in current_tasks
            if (task.scores_payload or {}).get("_dataset_run_id")
        }
        if len(run_ids) > 1:
            raise ValueError(f"Conflicting DatasetRun IDs for current Launch generation {launch_id}")
        all_terminal = all(task.status in {"SYNCED", "SKIPPED", "FAILED"} for task in current_tasks)
        has_failed = any(task.status == "FAILED" for task in current_tasks)
        dataset_run_id = next(iter(run_ids)) if run_ids else None
        if (
            dataset_run_id is None
            and not tasks
            and persisted_run_id
        ):
            # LaunchExecutionService's synchronous Langfuse path persists the DatasetRun
            # directly and does not create item outbox tasks.
            dataset_run_id = persisted_run_id
        return dataset_run_id, all_terminal, has_failed

    def _process_one(self, task_id: str, token: str, snapshot_id: str, client: Any | None, client_error: str | None) -> bool:
        from .langfuse_connection import BoundLangfuseClient, is_bound, resolve_manifest_connection
        from .langfuse_scores_client import ScoresReadError
        from .secret_providers import CredentialUnavailable

        with self.db_mgr.get_session() as session:
            snapshot = session.get(RunResultSnapshotRecord, snapshot_id)
            manifest = snapshot.manifest if snapshot else {}
        if is_bound(manifest):
            try:
                connection = resolve_manifest_connection(self.db_mgr, manifest, operation="RUN_SCORES", actor="snapshot:" + snapshot_id)
                with BoundLangfuseClient(connection) as bound_client:
                    bound_client.verify_project()
                    return self._process_with_client(task_id, token, snapshot_id, bound_client, None)
            except (CredentialUnavailable, ScoresReadError) as exc:
                return self._finish(task_id, token, status="FAILED", error=getattr(exc, "code", "CREDENTIAL_UNAVAILABLE"))
        return self._process_with_client(task_id, token, snapshot_id, client, client_error)

    def _process_with_client(self, task_id, token, snapshot_id, client, client_error):
        from .langfuse_connection import is_bound

        missing_data = False
        bound = False
        with self.db_mgr.get_session() as session:
            task = session.get(LangfuseRunScoreTaskRecord, task_id)
            snapshot = session.get(RunResultSnapshotRecord, snapshot_id)
            launch = session.get(ExperimentLaunchRecord, task.launch_id) if task else None
            if not task or not snapshot or not launch:
                missing_data = True
                scores, source, launch_id, revision = {}, None, "", 0
            else:
                bound = is_bound(snapshot.manifest)
                scores = dict(task.scores_payload or {})
                source = snapshot.manifest.get("dataset", {}).get("source")
                launch_id = launch.id
                revision = snapshot.revision

        if missing_data:
            return self._finish(task_id, token, status="FAILED", error="Run score task references missing result data")

        if source != "langfuse":
            return self._finish(task_id, token, status="SKIPPED", error="Run scores are only published for Langfuse datasets")
        numeric_scores = {
            metric: value
            for metric, value in scores.items()
            if not isinstance(value, bool) and isinstance(value, (int, float))
        }
        if not numeric_scores:
            return self._finish(task_id, token, status="SKIPPED", error="NO_NUMERIC_RUN_SCORES")
        if client_error:
            return self._finish(task_id, token, status="PENDING", error=client_error, retry=True)
        if client is None:
            return self._finish(task_id, token, status="FAILED", error="LANGFUSE_CLIENT_UNAVAILABLE")

        try:
            dataset_run_id, all_item_sync_terminal, item_sync_failed = self._dataset_run_id(launch_id)
            if not dataset_run_id:
                if item_sync_failed:
                    return self._finish(task_id, token, status="FAILED", error="Item Langfuse synchronization failed before DatasetRun creation")
                if all_item_sync_terminal:
                    return self._finish(task_id, token, status="SKIPPED", error="No linked Langfuse DatasetRun was produced")
                return self._finish(
                    task_id,
                    token,
                    status="PENDING",
                    error="Waiting for item DatasetRun links",
                    defer=True,
                )

            timeout_seconds = min(5, max(1, self.lease_seconds // 3))
            for metric, value in numeric_scores.items():
                if not self._renew_lease(task_id, token):
                    # The former claimant must not send another score after losing its lease.
                    return False
                stable_id = "argus-run-" + hashlib.sha256(f"{snapshot_id}:{metric}".encode()).hexdigest()[:48]
                publish_run_score(
                    client,
                    score_id=stable_id,
                    name=f"argus_{metric}",
                    value=float(value),
                    run_id=str(dataset_run_id),
                    metadata={"argus_snapshot_id": snapshot_id, "argus_snapshot_revision": revision},
                    timeout_seconds=timeout_seconds,
                )
        except Exception as exc:
            if bound:
                logger.warning("Failed to publish bound Run scores for Launch %s snapshot %s", launch_id, snapshot_id)
            else:
                logger.exception("Failed to publish Run scores for Launch %s snapshot %s", launch_id, snapshot_id)
            http_status = _http_status_code(exc)
            if http_status is not None and 400 <= http_status < 500 and http_status not in {408, 425, 429}:
                return self._finish(task_id, token, status="FAILED", error=f"LANGFUSE_HTTP_{http_status}")
            error_code = str(exc) if isinstance(exc, RunScorePublishError) else "LANGFUSE_SCORE_PUBLISH_FAILED"
            return self._finish(task_id, token, status="PENDING", error=error_code, retry=True)
        return self._finish(task_id, token, status="SYNCED")

    def process_batch(self, batch_size: int = 1) -> int:
        from .langfuse_connection import is_bound

        claimed = self._claim(batch_size)
        global_client = None
        success = 0
        for task_id, token, snapshot_id in claimed:
            with self.db_mgr.get_session() as session:
                snapshot = session.get(RunResultSnapshotRecord, snapshot_id)
                bound = snapshot is not None and is_bound(snapshot.manifest)
            if not bound and global_client is None:
                global_client = self._client()
            client, error = (None, None) if bound else global_client
            success += self._process_one(task_id, token, snapshot_id, client, error)
        return success
