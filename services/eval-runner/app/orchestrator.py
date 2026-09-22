from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .limiter import DistributedAgentLimiter
from .queue import QueueAdapter
from .state_machine import (
    DomainConflictError,
    calculate_launch_progress,
    determine_allowed_actions,
    transition_launch_status,
    validate_launch_action_allowed,
)


class LaunchOrchestrator:
    """Orchestrates Launch lifecycle: materialization, dispatch, cancel, resume, and retry-failed."""

    def __init__(
        self,
        db_mgr: DatabaseManager,
        queue: QueueAdapter,
        limiter: DistributedAgentLimiter,
    ):
        self.db_mgr = db_mgr
        self.queue = queue
        self.limiter = limiter

    def create_launch(
        self,
        agent_id: str,
        agent_version: str,
        dataset_name: str,
        dataset_version: str,
        name: str,
        manifest: dict[str, Any],
        idempotency_key: str | None = None,
        created_by: str | None = None,
    ) -> ExperimentLaunchRecord:
        launch_id = str(uuid.uuid4())
        agent_version_id = manifest.get("agent", {}).get("agent_version_id") or f"{agent_id}-{agent_version}"

        items_seed = manifest.get("dataset", {}).get("items", [])

        with self.db_mgr.get_session() as session:
            launch = ExperimentLaunchRecord(
                id=launch_id,
                name=name,
                status="PENDING",
                quality_conclusion="unknown",
                idempotency_key=idempotency_key,
                dataset_name=dataset_name,
                dataset_version=dataset_version,
                agent_id=agent_id,
                agent_version=agent_version,
                agent_version_id=agent_version_id,
                manifest=manifest,
                created_by=created_by,
                created_at=datetime.now(UTC),
            )
            session.add(launch)
            session.flush()

            # Pre-materialize all dataset items as PENDING with generation 1
            for item in items_seed:
                item_id = str(item.get("id", uuid.uuid4()))
                item_rec = ExperimentItemExecutionRecord(
                    id=str(uuid.uuid4()),
                    launch_id=launch_id,
                    dataset_item_id=item_id,
                    execution_status="pending",
                    eval_status="pending",
                    quality_conclusion="unknown",
                    dispatch_generation=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(item_rec)

            session.commit()
            session.refresh(launch)
            return launch

    def start_launch(self, launch_id: str) -> ExperimentLaunchRecord:
        """Transitions PENDING launch and its items to QUEUED, and dispatches to queue."""
        with self.db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")

            transition_launch_status(launch.status, "QUEUED")
            launch.status = "QUEUED"
            launch.updated_at = datetime.now(UTC)

            # Update all pending items to queued
            now = datetime.now(UTC)
            items = session.scalars(
                select(ExperimentItemExecutionRecord).where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status == "pending",
                )
            ).all()

            dispatch_items = []
            for it in items:
                it.execution_status = "queued"
                it.queued_at = now
                it.updated_at = now
                dispatch_items.append((it.id, it.dispatch_generation))

            session.commit()
            session.refresh(launch)

        # Post-commit dispatch to queue
        if dispatch_items:
            self.queue.enqueue_items(dispatch_items)

        return launch

    def cancel_launch(self, launch_id: str) -> ExperimentLaunchRecord:
        """Requests collaborative cancellation for a launch using strict Launch -> Item lock order."""
        now = datetime.now(UTC)
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"

        with self.db_mgr.get_session() as session:
            # 1. Lock Launch
            launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                launch_stmt = launch_stmt.with_for_update()
            launch = session.scalars(launch_stmt).first()
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")

            if launch.cancel_requested_at:
                return launch  # Idempotent

            launch.cancel_requested_at = now
            launch.updated_at = now

            # 2. Lock Items and immediately cancel items that are pending, queued, or retry_wait
            item_stmt = (
                select(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status.in_(["pending", "queued", "retry_wait"]),
                )
            )
            if is_pg:
                item_stmt = item_stmt.with_for_update()
            cancelable_items = session.scalars(item_stmt).all()

            for it in cancelable_items:
                it.execution_status = "cancelled"
                it.active_attempt_id = None
                it.lease_owner = None
                it.lease_token = None
                it.lease_expires_at = None
                it.updated_at = now

            # Check if any items are currently running
            running_count = session.scalar(
                select(func.count(ExperimentItemExecutionRecord.id)).where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status == "running",
                )
            ) or 0

            if running_count == 0:
                launch.status = "CANCELLED"
                launch.completed_at = now
            else:
                launch.status = "CANCELLING"

            session.commit()
            session.refresh(launch)
            return launch

    def resume_launch(self, launch_id: str, force: bool = False) -> ExperimentLaunchRecord:
        """Resumes cancelled launch by advancing generation exclusively for cancelled items.
        Requires no active items. Clears cancel flag and transitions launch to QUEUED.
        """
        now = datetime.now(UTC)
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        dispatch_items = []

        with self.db_mgr.get_session() as session:
            # 1. Lock Launch
            launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                launch_stmt = launch_stmt.with_for_update()
            launch = session.scalars(launch_stmt).first()
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")

            # Check non-idempotent ambiguous outcome protection across all attempts of this launch
            ambiguous_attempts = session.scalars(
                select(ExecutionAttemptRecord)
                .join(ExperimentItemExecutionRecord, ExecutionAttemptRecord.item_execution_id == ExperimentItemExecutionRecord.id)
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExecutionAttemptRecord.error_type == "AMBIGUOUS_OUTCOME",
                )
            ).all()

            if ambiguous_attempts and not force:
                raise DomainConflictError(
                    f"Found {len(ambiguous_attempts)} failed attempts with AMBIGUOUS_OUTCOME for non-idempotent agent. "
                    "Automatic replay is unsafe. Please specify force=True to confirm re-execution."
                )

            # Query item counts to enforce single common lifecycle guard
            counts_res = session.execute(
                select(
                    ExperimentItemExecutionRecord.execution_status,
                    func.count(ExperimentItemExecutionRecord.id),
                )
                .where(ExperimentItemExecutionRecord.launch_id == launch_id)
                .group_by(ExperimentItemExecutionRecord.execution_status)
            ).all()
            counts = {row[0].lower(): row[1] for row in counts_res}

            validate_launch_action_allowed(launch.status, launch.cancel_requested_at, counts, "resume")

            # 2. Lock target cancelled items
            item_stmt = (
                select(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status == "cancelled",
                )
            )
            if is_pg:
                item_stmt = item_stmt.with_for_update()
            resumable_items = session.scalars(item_stmt).all()

            if not resumable_items:
                raise DomainConflictError("No cancelled items found to resume")

            # Reset launch and transition to QUEUED
            launch.cancel_requested_at = None
            launch.status = "QUEUED"
            launch.langfuse_sync_status = "PENDING"
            launch.langfuse_sync_error = None
            launch.completed_at = None
            launch.updated_at = now

            for it in resumable_items:
                it.dispatch_generation += 1
                it.execution_status = "queued"
                it.eval_status = "pending"
                it.quality_conclusion = "unknown"
                it.queued_at = now
                it.available_at = None
                it.lease_owner = None
                it.lease_token = None
                it.lease_expires_at = None
                it.active_attempt_id = None
                it.completed_at = None
                it.execution_error = None
                it.eval_error = None
                it.updated_at = now
                dispatch_items.append((it.id, it.dispatch_generation))

            session.commit()
            session.refresh(launch)

        if dispatch_items:
            self.queue.enqueue_items(dispatch_items)

        return launch

    def retry_failed_items(self, launch_id: str, force: bool = False) -> ExperimentLaunchRecord:
        """Retries only FAILED and TIMED_OUT items, preserving SUCCEEDED and CANCELLED items.
        Requires no active items. Clears cancel flag and transitions launch to QUEUED.
        """
        now = datetime.now(UTC)
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        dispatch_items = []

        with self.db_mgr.get_session() as session:
            # 1. Lock Launch
            launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                launch_stmt = launch_stmt.with_for_update()
            launch = session.scalars(launch_stmt).first()
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")

            # Query item counts to enforce single common lifecycle guard
            counts_res = session.execute(
                select(
                    ExperimentItemExecutionRecord.execution_status,
                    func.count(ExperimentItemExecutionRecord.id),
                )
                .where(ExperimentItemExecutionRecord.launch_id == launch_id)
                .group_by(ExperimentItemExecutionRecord.execution_status)
            ).all()
            counts = {row[0].lower(): row[1] for row in counts_res}

            # 2. Lock target failed/timed_out items
            item_stmt = (
                select(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status.in_(["failed", "timed_out"]),
                )
            )
            if is_pg:
                item_stmt = item_stmt.with_for_update()
            failed_items = session.scalars(item_stmt).all()

            if not failed_items:
                raise DomainConflictError("No failed or timed out items found to retry")

            # 3. Non-idempotent crash protection: check failed items FIRST
            item_ids = [it.id for it in failed_items]
            ambiguous_attempts = session.scalars(
                select(ExecutionAttemptRecord).where(
                    ExecutionAttemptRecord.item_execution_id.in_(item_ids),
                    ExecutionAttemptRecord.error_type == "AMBIGUOUS_OUTCOME",
                )
            ).all()

            if ambiguous_attempts and not force:
                raise DomainConflictError(
                    f"Found {len(ambiguous_attempts)} failed attempts with AMBIGUOUS_OUTCOME for non-idempotent agent. "
                    "Automatic replay is unsafe. Please specify force=True to confirm re-execution."
                )

            # Enforce common lifecycle contract
            validate_launch_action_allowed(launch.status, launch.cancel_requested_at, counts, "retry_failed")

            # Reset launch and transition to QUEUED
            launch.cancel_requested_at = None
            launch.status = "QUEUED"
            launch.langfuse_sync_status = "PENDING"
            launch.langfuse_sync_error = None
            launch.completed_at = None
            launch.updated_at = now

            for it in failed_items:
                it.dispatch_generation += 1
                it.execution_status = "queued"
                it.eval_status = "pending"
                it.quality_conclusion = "unknown"
                it.queued_at = now
                it.available_at = None
                it.lease_owner = None
                it.lease_token = None
                it.lease_expires_at = None
                it.active_attempt_id = None
                it.completed_at = None
                it.execution_error = None
                it.eval_error = None
                it.updated_at = now
                dispatch_items.append((it.id, it.dispatch_generation))

            session.commit()
            session.refresh(launch)

        if dispatch_items:
            self.queue.enqueue_items(dispatch_items)

        return launch

    def get_launch_progress(self, launch_id: str) -> dict[str, Any]:
        """Calculates exact item counts, percentage, and allowed actions for the launch."""
        with self.db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")

            # Count items grouped by execution_status
            counts_res = session.execute(
                select(
                    ExperimentItemExecutionRecord.execution_status,
                    func.count(ExperimentItemExecutionRecord.id),
                )
                .where(ExperimentItemExecutionRecord.launch_id == launch_id)
                .group_by(ExperimentItemExecutionRecord.execution_status)
            ).all()

            counts = {row[0].lower(): row[1] for row in counts_res}

            # Count attempts and retries
            attempts_count = session.scalar(
                select(func.count(ExecutionAttemptRecord.id))
                .join(ExperimentItemExecutionRecord, ExecutionAttemptRecord.item_execution_id == ExperimentItemExecutionRecord.id)
                .where(ExperimentItemExecutionRecord.launch_id == launch_id)
            ) or 0

            retries_count = session.scalar(
                select(func.count(ExecutionAttemptRecord.id))
                .join(ExperimentItemExecutionRecord, ExecutionAttemptRecord.item_execution_id == ExperimentItemExecutionRecord.id)
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExecutionAttemptRecord.attempt_no > 1,
                )
            ) or 0

            progress = calculate_launch_progress(counts, attempts_count, retries_count)
            actions = determine_allowed_actions(
                launch_status=launch.status,
                cancel_requested_at=launch.cancel_requested_at,
                counts=counts,
            )
            progress["allowed_actions"] = actions["allowed"]
            progress["action_reasons"] = actions["reasons"]
            return progress
