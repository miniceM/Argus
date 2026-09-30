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
from .runner_identity import current_runner_identity, validate_runner_identity
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
        self.runner_identity = current_runner_identity()

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

        manifest = dict(manifest)
        manifest.setdefault("runner", self.runner_identity.model_dump())
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

    def _assert_runner_identity(self, launch: ExperimentLaunchRecord) -> None:
        reason = validate_runner_identity(launch.manifest.get("runner"), self.runner_identity)
        if reason:
            raise DomainConflictError(reason)

    def start_launch(self, launch_id: str) -> ExperimentLaunchRecord:
        """Transitions PENDING launch and its items to QUEUED, and dispatches to queue."""
        with self.db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")
            self._assert_runner_identity(launch)

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
            self._assert_runner_identity(launch)

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
            self._assert_runner_identity(launch)

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

    def retry_failed_evaluations(self, launch_id: str) -> dict[str, Any]:
        """Re-judge failed/missing evaluations by reusing stored Agent outputs.

        This never calls the Agent, never creates or advances an execution
        attempt and never touches ``dispatch_generation``, so the
        non-idempotent-execution safety boundary is preserved. Submission is
        idempotent: an item already being re-evaluated is reported as
        ``already_running`` and never re-dispatched, so a double-click or a
        competing request produces at most one effective submission.
        """
        from .evaluation_recovery import (
            EVALUATION_RUNNING,
            RECOVERABLE_EVAL_STATUSES,
            WORK_TYPE_EVALUATION,
        )
        from .execution_checkpoint import CheckpointUnavailableError, load_recoverable_checkpoint

        now = datetime.now(UTC)
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        dispatch_items: list[tuple[str, int]] = []
        submitted: list[str] = []
        already_running: list[str] = []
        blocked: list[dict[str, Any]] = []

        with self.db_mgr.get_session() as session:
            launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == launch_id)
            if is_pg:
                launch_stmt = launch_stmt.with_for_update()
            launch = session.scalars(launch_stmt).first()
            if not launch:
                raise ValueError(f"Launch '{launch_id}' not found")
            self._assert_runner_identity(launch)

            if launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"):
                raise DomainConflictError("评测任务已取消或正在取消，无法重试评测。")

            # No in-flight execution items may race with a re-evaluation.
            in_flight = session.scalar(
                select(func.count(ExperimentItemExecutionRecord.id)).where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status.in_(["running", "retry_wait", "queued"]),
                )
            ) or 0
            if in_flight:
                raise DomainConflictError("任务尚有未完成用例处于排队、准备或运行中，请稍后重试评测。")

            item_stmt = (
                select(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status == "succeeded",
                )
            )
            if is_pg:
                item_stmt = item_stmt.with_for_update()
            candidates = session.scalars(item_stmt).all()

            manifest = launch.manifest or {}
            for it in candidates:
                item_id = it.id
                # Already being re-evaluated: idempotent no-op (double-click).
                if it.evaluation_status == EVALUATION_RUNNING:
                    already_running.append(item_id)
                    continue
                if (it.eval_status or "").lower() not in RECOVERABLE_EVAL_STATUSES:
                    continue
                # Validate the recoverable artifact *before* dispatching, so a
                # missing/expired/corrupt checkpoint is a clear refusal, not a
                # silent re-invocation.
                try:
                    load_recoverable_checkpoint(
                        session,
                        item_execution_id=item_id,
                        dispatch_generation=it.dispatch_generation,
                        manifest=manifest,
                        now=now,
                    )
                except CheckpointUnavailableError as exc:
                    blocked.append(
                        {
                            "item_execution_id": item_id,
                            "dataset_item_id": it.dataset_item_id,
                            "code": exc.code,
                            "message": exc.message,
                            "hint": exc.hint,
                        }
                    )
                    continue

                it.evaluation_generation = (it.evaluation_generation or 0) + 1
                it.evaluation_status = EVALUATION_RUNNING
                it.evaluation_error = None
                it.evaluation_lease_owner = None
                it.evaluation_lease_token = None
                it.evaluation_lease_expires_at = None
                it.evaluation_started_at = now
                it.evaluation_completed_at = None
                it.updated_at = now
                dispatch_items.append((item_id, it.evaluation_generation))
                submitted.append(item_id)

            if not submitted and not already_running:
                if blocked:
                    # Everything is unrecoverable: surface the first reason.
                    first = blocked[0]
                    raise DomainConflictError(
                        f"没有可仅重试评测的用例：{first['message']}"
                    )
                raise DomainConflictError("当前评测没有失败或缺失的用例需要重评。")

            session.commit()
            session.refresh(launch)

        if dispatch_items:
            self.queue.enqueue_items(dispatch_items, work_type=WORK_TYPE_EVALUATION)

        return {
            "launch": launch,
            "submitted": submitted,
            "already_running": already_running,
            "blocked": blocked,
        }

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

            # Issue #84: a case is eligible for an evaluation-only retry when it
            # executed successfully, its evaluation failed / is missing, and a
            # recoverable checkpoint exists for the current generation.
            from .db_models import ExecutionCheckpointRecord
            from .evaluation_recovery import RECOVERABLE_EVAL_STATUSES

            recoverable_eval_count = session.scalar(
                select(func.count(ExperimentItemExecutionRecord.id))
                .join(
                    ExecutionCheckpointRecord,
                    (ExecutionCheckpointRecord.item_execution_id == ExperimentItemExecutionRecord.id)
                    & (ExecutionCheckpointRecord.dispatch_generation == ExperimentItemExecutionRecord.dispatch_generation),
                )
                .where(
                    ExperimentItemExecutionRecord.launch_id == launch_id,
                    ExperimentItemExecutionRecord.execution_status == "succeeded",
                    ExperimentItemExecutionRecord.eval_status.in_(list(RECOVERABLE_EVAL_STATUSES)),
                )
            ) or 0

            progress = calculate_launch_progress(counts, attempts_count, retries_count)
            progress["recoverable_evaluation_count"] = int(recoverable_eval_count)
            actions = determine_allowed_actions(
                launch_status=launch.status,
                cancel_requested_at=launch.cancel_requested_at,
                counts=counts,
                recoverable_eval_count=int(recoverable_eval_count),
            )
            progress["allowed_actions"] = actions["allowed"]
            progress["action_reasons"] = actions["reasons"]
            return progress
