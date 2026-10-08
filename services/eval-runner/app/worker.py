from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.db_models import (  # noqa: E402
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseSyncTaskRecord,
)
from app.execution import get_langfuse_client_safe  # noqa: E402

from .db import DatabaseManager
from .evaluation_result_store import persist_typed_results
from .evaluator_binding import EvaluatorBindingError, evaluate_frozen_item, resolve_execution_plan
from .executor import RemoteAgentExecutor
from .limiter import DistributedAgentLimiter
from .metrics import runtime_metrics
from .queue import QueueAdapter
from .registry import AgentVersionSpec, map_request
from .runner_identity import RunnerIdentity, current_runner_identity, validate_runner_identity

try:
    from opentelemetry.propagate import inject
except Exception:
    def inject(carrier: Any) -> None:
        pass


class ExecutionWorker:
    """Consumes items from queue, acquires strict DB-fenced leases, invokes remote agent, and conditional finalizes."""

    def __init__(
        self,
        db_mgr: DatabaseManager,
        queue: QueueAdapter,
        limiter: DistributedAgentLimiter,
        worker_id: str | None = None,
        runner_identity: RunnerIdentity | None = None,
    ):
        self.db_mgr = db_mgr
        self.queue = queue
        self.limiter = limiter
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.runner_identity = runner_identity or current_runner_identity()
        self.local_concurrency = asyncio.Semaphore(10)
        self.heartbeat_interval = 5.0

    def poll_queue(self, count: int = 10, block_ms: int = 1000) -> list[tuple[str, str, int, str]]:
        """Synchronously reads/claims items from the queue adapter, meant to be run in asyncio.to_thread."""
        return self.queue.read_group(self.worker_id, count=count, block_ms=block_ms)

    def renew_lease(self, item_id: str, lease_token: str, extension_seconds: int = 30) -> bool:
        """Renews active lease timestamp for in-flight items under row lock and post-lock clock check."""
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            stmt = select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.id == item_id)
            if is_pg:
                stmt = stmt.with_for_update()
            item = session.scalars(stmt).first()
            if not item:
                return False

            current_ts = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=UTC)
            lease_exp = item.lease_expires_at
            if lease_exp and lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=UTC)

            if (
                item.lease_token != lease_token
                or item.execution_status != "running"
                or lease_exp is None
                or lease_exp <= current_ts
            ):
                session.rollback()
                return False

            item.lease_expires_at = current_ts + timedelta(seconds=extension_seconds)
            item.updated_at = current_ts
            session.commit()
            return True

    def claim_item(
        self,
        item_id: str,
        generation: int,
        lease_seconds: int = 30,
    ) -> dict[str, Any] | None:
        """Ordinary worker claims ONLY currently valid QUEUED items with matching generation."""
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=lease_seconds)
        token = str(uuid.uuid4())

        with self.db_mgr.get_session() as session:
            # Atomic conditional claim
            stmt = (
                update(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.id == item_id,
                    ExperimentItemExecutionRecord.dispatch_generation == generation,
                    ExperimentItemExecutionRecord.execution_status == "queued",
                    (ExperimentItemExecutionRecord.available_at.is_(None))
                    | (ExperimentItemExecutionRecord.available_at <= now),
                )
                .values(
                    execution_status="running",
                    lease_owner=self.worker_id,
                    lease_token=token,
                    lease_expires_at=expires_at,
                    started_at=func.coalesce(ExperimentItemExecutionRecord.started_at, now),
                    updated_at=now,
                )
            )
            res = session.execute(stmt)
            if res.rowcount == 1:
                item = session.get(ExperimentItemExecutionRecord, item_id)
                session.commit()
                return {
                    "item_id": item_id,
                    "launch_id": item.launch_id,
                    "dataset_item_id": item.dataset_item_id,
                    "lease_token": token,
                    "lease_expires_at": expires_at,
                }
            session.rollback()
            return None

    def authorize_attempt(
        self,
        item_id: str,
        lease_token: str,
        generation: int = 1,
        launch_id: str | None = None,
    ) -> int | None:
        """Authorizes attempt creation serialized with launch cancellation and strict ownership verification.
        Global lock ordering: Launch -> Item. Locks are released immediately after Attempt creation.
        """
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"

        with self.db_mgr.get_session() as session:
            effective_launch_id = launch_id
            if not effective_launch_id:
                item_pre = session.get(ExperimentItemExecutionRecord, item_id)
                if not item_pre:
                    return None
                effective_launch_id = item_pre.launch_id

            # 1. Lock Launch
            launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == effective_launch_id)
            if is_pg:
                launch_stmt = launch_stmt.with_for_update()
            launch = session.scalars(launch_stmt).first()
            if not launch:
                return None

            # 2. Lock Item
            item_stmt = select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.id == item_id)
            if is_pg:
                item_stmt = item_stmt.with_for_update()
            item = session.scalars(item_stmt).first()
            if not item:
                session.rollback()
                return None

            # 3. Post-lock instantaneous clock check
            current_ts = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=UTC)
            lease_exp = item.lease_expires_at
            if lease_exp and lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=UTC)

            if (
                item.dispatch_generation != generation
                or item.lease_token != lease_token
                or item.execution_status != "running"
                or lease_exp is None
                or lease_exp <= current_ts
            ):
                # Lost ownership or expired! Exit safely without modifying anything.
                session.rollback()
                return None

            # Check collaborative cancellation: worker holds valid lease, now cooperatively cancels item
            if launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"):
                item.execution_status = "cancelled"
                item.active_attempt_id = None
                item.lease_owner = None
                item.lease_token = None
                item.lease_expires_at = None
                item.updated_at = current_ts
                session.commit()
                return None

            # Count existing attempts for this item
            existing_count = session.scalar(
                select(func.count(ExecutionAttemptRecord.id)).where(
                    ExecutionAttemptRecord.item_execution_id == item_id
                )
            ) or 0
            next_attempt_no = existing_count + 1
            att_id = str(uuid.uuid4())

            att_rec = ExecutionAttemptRecord(
                id=att_id,
                item_execution_id=item_id,
                attempt_no=next_attempt_no,
                status="RUNNING",
                worker_id=self.worker_id,
                request_phase="PREPARED",
                dispatch_generation=generation,
                lease_token=lease_token,
                started_at=current_ts,
            )
            session.add(att_rec)
            item.active_attempt_id = att_id
            item.updated_at = current_ts
            # Commit immediately to release row locks before invoking HTTP
            session.commit()
            return next_attempt_no

    def mark_attempt_dispatched(
        self, item_id: str, launch_id: str, lease_token: str, generation: int,
        attempt_id: str, credential: Any = None,
    ) -> bool:
        """凭据预取后按 Launch -> Item -> Attempt 加锁，再确认发送权和租约。"""
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        with self.db_mgr.get_session() as session:
            def locked(model, record_id):
                statement = select(model).where(model.id == record_id)
                if is_pg:
                    statement = statement.with_for_update()
                return session.scalars(statement).first()

            launch = locked(ExperimentLaunchRecord, launch_id)
            item = locked(ExperimentItemExecutionRecord, item_id)
            attempt = locked(ExecutionAttemptRecord, attempt_id)
            current_ts = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=UTC)
            expires = item.lease_expires_at if item else None
            if expires and expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if (
                not launch or not item or not attempt
                or launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED")
                or item.launch_id != launch_id or item.execution_status != "running"
                or item.lease_token != lease_token or item.dispatch_generation != generation
                or not expires or expires <= current_ts or item.active_attempt_id != attempt_id
                or attempt.item_execution_id != item_id or attempt.status != "RUNNING"
                or attempt.lease_token != lease_token or attempt.dispatch_generation != generation
                or attempt.request_phase != "PREPARED"
            ):
                session.rollback()
                return False
            if credential:
                attempt.credential_id = credential.credential_id
                attempt.credential_version = credential.version
                attempt.credential_provider = credential.provider
            attempt.request_phase = "MAY_HAVE_BEEN_SENT"
            session.commit()
            return True

    def finalize_execution_and_attempt(
        self,
        item_id: str,
        generation: int,
        lease_token: str,
        target_item_status: str,  # 'succeeded', 'failed', 'timed_out', 'cancelled', 'retry_wait'
        target_eval_status: str,
        target_quality_conclusion: str,
        current_attempt_id: str | None = None,
        attempt_updates: dict[str, Any] | None = None,
        scores: dict[str, Any] | None = None,
        typed_results: list[Any] | None = None,
        quality_evaluation: dict[str, Any] | None = None,
        execution_error: str | None = None,
        eval_error: str | None = None,
        retry_available_at: datetime | None = None,
        trace_id: str | None = None,
        trace_url: str | None = None,
        observation_id: str | None = None,
        launch_id: str | None = None,
        dataset_item_id: str | None = None,
        dataset_version: str | None = None,
        dataset_run_name: str | None = None,
        dataset_source: str | None = None,
    ) -> bool:
        """Atomic finalization for all outcome branches.
        Requires active_attempt_id CAS matching and Attempt status RUNNING.
        Rolls back entirely if either row count != 1.
        """
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"

        with self.db_mgr.get_session() as session:
            # Phase 1: Lock row
            lock_stmt = select(ExperimentItemExecutionRecord).where(ExperimentItemExecutionRecord.id == item_id)
            if is_pg:
                lock_stmt = lock_stmt.with_for_update()
            item = session.scalars(lock_stmt).first()
            if not item:
                return False

            # Post-lock instantaneous clock check
            current_ts = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
            if current_ts.tzinfo is None:
                current_ts = current_ts.replace(tzinfo=UTC)
            lease_exp = item.lease_expires_at
            if lease_exp and lease_exp.tzinfo is None:
                lease_exp = lease_exp.replace(tzinfo=UTC)

            if (
                item.dispatch_generation != generation
                or item.lease_token != lease_token
                or item.execution_status != "running"
                or lease_exp is None
                or lease_exp <= current_ts
            ):
                session.rollback()
                return False

            if current_attempt_id is not None:
                if item.active_attempt_id != current_attempt_id:
                    session.rollback()
                    return False
            else:
                if item.active_attempt_id is not None:
                    session.rollback()
                    return False

            is_retry_wait = target_item_status.lower() == "retry_wait"
            item.execution_status = target_item_status.lower()
            item.eval_status = target_eval_status.lower()
            item.quality_conclusion = target_quality_conclusion.lower()
            item.final_attempt_id = current_attempt_id
            item.active_attempt_id = None
            item.scores = scores
            # Only overwrite a recorded decision with a real one: a retry that
            # never reached evaluation must not erase the earlier reasons.
            if quality_evaluation is not None:
                item.quality_evaluation = quality_evaluation
            item.execution_error = execution_error
            item.eval_error = eval_error
            item.available_at = retry_available_at
            item.trace_id = trace_id
            item.langfuse_trace_url = trace_url
            item.observation_id = observation_id
            item.lease_owner = None
            item.lease_token = None
            item.lease_expires_at = None
            item.completed_at = None if is_retry_wait else current_ts
            item.updated_at = current_ts

            # Phase 3: CAS update attempt
            if current_attempt_id is not None and attempt_updates is not None:
                att_values = dict(attempt_updates)
                att_values.setdefault("completed_at", current_ts)
                stmt_att = (
                    update(ExecutionAttemptRecord)
                    .where(
                        ExecutionAttemptRecord.id == current_attempt_id,
                        ExecutionAttemptRecord.item_execution_id == item_id,
                        ExecutionAttemptRecord.dispatch_generation == generation,
                        ExecutionAttemptRecord.lease_token == lease_token,
                        ExecutionAttemptRecord.status == "RUNNING",
                    )
                    .values(**att_values)
                )
                res_att = session.execute(stmt_att)
                if res_att.rowcount != 1:
                    session.rollback()
                    return False

            # Phase 3.5: persist the authoritative typed results (Issue #82).
            # This runs in the same transaction as the item update, so a
            # committed item always has its typed evidence, and an empty
            # result set never blanks previously stored measurements.
            if launch_id and typed_results:
                persist_typed_results(
                    session,
                    item_execution_id=item_id,
                    launch_id=launch_id,
                    results=typed_results,
                )

            # Phase 4: Atomic insertion of Langfuse Outbox task if applicable
            if (
                not is_retry_wait
                and launch_id
                and dataset_item_id
                and dataset_run_name
                and trace_id
            ):
                task_id = str(uuid.uuid4())
                payload_scores = dict(scores or {})
                if dataset_source:
                    payload_scores["_dataset_source"] = dataset_source
                # Issue #87: the Langfuse projection reads the *typed* frozen
                # results, so a text or unordered category is reported as not
                # applicable instead of being coerced into a number. Keys
                # starting with "_" are never uploaded as scores.
                if typed_results:
                    payload_scores["_typed_results"] = [
                        result.to_payload() for result in typed_results
                    ]
                # The frozen policy identity travels with the projection so a
                # Langfuse score can be traced back to the decision rules used.
                launch_row = session.get(ExperimentLaunchRecord, launch_id)
                frozen_policy = (launch_row.manifest or {}).get("quality_policy") or {}
                payload_scores["_policy_digest"] = frozen_policy.get("policy_digest")
                outbox_task = LangfuseSyncTaskRecord(
                    id=task_id,
                    launch_id=launch_id,
                    item_id=item_id,
                    dataset_item_id=dataset_item_id,
                    dataset_version=dataset_version,
                    dispatch_generation=generation,
                    task_type="FULL_EVAL_SYNC",
                    trace_id=trace_id,
                    observation_id=observation_id,
                    dataset_run_name=dataset_run_name,
                    scores_payload=payload_scores,
                    status="PENDING",
                    attempts=0,
                    next_retry_at=current_ts,
                    created_at=current_ts,
                    updated_at=current_ts,
                )
                session.add(outbox_task)

            session.commit()
            runtime_metrics.record_item_status(target_item_status)
            if attempt_updates and "status" in attempt_updates:
                runtime_metrics.record_attempt(attempt_updates["status"])
            return True

    def finalize_item(
        self,
        item_id: str,
        generation: int,
        lease_token: str,
        status: str,
        eval_status: str,
        quality_conclusion: str,
        final_attempt_id: str | None = None,
        scores: dict[str, Any] | None = None,
        execution_error: str | None = None,
        eval_error: str | None = None,
    ) -> bool:
        """Backward-compatible finalize wrapper."""
        return self.finalize_execution_and_attempt(
            item_id=item_id,
            generation=generation,
            lease_token=lease_token,
            target_item_status=status,
            target_eval_status=eval_status,
            target_quality_conclusion=quality_conclusion,
            current_attempt_id=final_attempt_id,
            scores=scores,
            execution_error=execution_error,
            eval_error=eval_error,
        )


    async def execute_item_message(
        self,
        message_id: str,
        item_id: str,
        generation: int,
        work_type: str | None = None,
    ) -> bool:
        """Full pipeline: reserve local slot -> claim -> authorize attempt -> invoke -> finalize -> ack.

        ``work_type`` is accepted (and ignored) so a raw queue message tuple can
        be splatted straight into this method; ``main`` routes evaluation-only
        work to :meth:`execute_evaluation_message` before calling here.
        """
        async with self.local_concurrency:
            claim_info = self.claim_item(item_id, generation)
            if not claim_info:
                # Expired generation or already claimed by another worker -> ACK message
                self.queue.ack(message_id)
                return False

            token = claim_info["lease_token"]
            launch_id = claim_info["launch_id"]

            # Retrieve launch manifest and item input before authorizing attempt
            with self.db_mgr.get_session() as session:
                item_rec = session.get(ExperimentItemExecutionRecord, item_id)
                launch_rec = session.get(ExperimentLaunchRecord, launch_id)
                manifest = launch_rec.manifest
                agent_dict = manifest["agent"]
                policy_dict = manifest["execution_policy"]

                # Find input and expected output in dataset items
                items_seed = manifest.get("dataset", {}).get("items", [])
                matched = next((it for it in items_seed if str(it.get("id")) == item_rec.dataset_item_id), {})
                dataset_input = matched.get("input", {})
                expected_output = matched.get("expected_output", {})

                # Issue #81: validate the frozen Evaluator identity before dispatching
                # an item, so an unrecoverable artifact never reaches the Agent.
                try:
                    resolve_execution_plan(manifest)
                except EvaluatorBindingError as binding_exc:
                    # Fence the item exactly like a Runner identity mismatch: the
                    # frozen implementation cannot be honored, so no Agent call and
                    # no score may be produced (Issue #81).
                    finalized = self.finalize_item(
                        item_id=item_id,
                        generation=generation,
                        lease_token=token,
                        status="FAILED",
                        eval_status="skipped",
                        quality_conclusion="unknown",
                        execution_error=binding_exc.code,
                    )
                    self.queue.ack(message_id)
                    return finalized

                identity_error = validate_runner_identity(manifest.get("runner"), self.runner_identity)
                if identity_error:
                    # Fence the claimed item as a pre-execution failure. No Attempt or Agent call is created.
                    finalized = self.finalize_item(
                        item_id=item_id,
                        generation=generation,
                        lease_token=token,
                        status="FAILED",
                        eval_status="skipped",
                        quality_conclusion="unknown",
                        execution_error=identity_error,
                    )
                    self.queue.ack(message_id)
                    return finalized

                # If launch is still QUEUED, transition to RUNNING atomically
                if launch_rec.status == "QUEUED":
                    launch_rec.status = "RUNNING"
                    launch_rec.started_at = datetime.now(UTC)
                    launch_rec.updated_at = datetime.now(UTC)
                    session.commit()

            spec = AgentVersionSpec(
                agent_id=agent_dict["agent_id"],
                version=agent_dict["version"],
                endpoint=agent_dict["endpoint"],
                method=agent_dict["method"],
                timeout_seconds=policy_dict["timeout_seconds"],
                max_retries=policy_dict["max_retries"],
                rate_limit_per_minute=policy_dict["rate_limit_per_minute"],
                request_mapping=agent_dict["request_mapping"],
                usage_cost_mapping=agent_dict.get("usage_cost_mapping"),
                max_concurrency=policy_dict["max_concurrency"],
                is_idempotent=bool(agent_dict.get("is_idempotent", False)),
                credential_ref=agent_dict.get("credential_ref"),
                credential_id=agent_dict.get("credential_id"),
                id=agent_dict.get("agent_version_id", f"{agent_dict['agent_id']}-{agent_dict['version']}"),
            )

            # Ensure initial lease covers configured timeout
            initial_lease_sec = max(30, int(spec.timeout_seconds * 1.5) + 15)
            self.renew_lease(item_id, token, extension_seconds=initial_lease_sec)

            # 1. Acquire distributed rate permit (do NOT authorize attempt if rate limited)
            if spec.rate_limit_per_minute and spec.rate_limit_per_minute > 0:
                rate_ok = self.limiter.acquire_rate_permit(spec.id, spec.rate_limit_per_minute)
                if not rate_ok:
                    # Rate limited -> delay to RETRY_WAIT and release lease (No attempt consumed!)
                    with self.db_mgr.get_session() as session:
                        stmt = (
                            update(ExperimentItemExecutionRecord)
                            .where(
                                ExperimentItemExecutionRecord.id == item_id,
                                ExperimentItemExecutionRecord.lease_token == token,
                            )
                            .values(
                                execution_status="retry_wait",
                                available_at=datetime.now(UTC) + timedelta(seconds=1.5),
                                lease_owner=None,
                                lease_token=None,
                                lease_expires_at=None,
                                updated_at=datetime.now(UTC),
                            )
                        )
                        session.execute(stmt)
                        session.commit()
                    self.queue.ack(message_id)
                    return False

            # 2. Acquire distributed concurrency permit covering full execution timeout
            permit_id = self.limiter.acquire_concurrency_permit(
                spec.id, spec.max_concurrency, timeout_sec=float(initial_lease_sec), owner_id=self.worker_id
            )
            if not permit_id:
                # Could not acquire concurrency permit -> delay to RETRY_WAIT and release lease (No attempt consumed!)
                with self.db_mgr.get_session() as session:
                    stmt = (
                        update(ExperimentItemExecutionRecord)
                        .where(
                            ExperimentItemExecutionRecord.id == item_id,
                            ExperimentItemExecutionRecord.lease_token == token,
                        )
                        .values(
                            execution_status="retry_wait",
                            available_at=datetime.now(UTC) + timedelta(seconds=1.5),
                            lease_owner=None,
                            lease_token=None,
                            lease_expires_at=None,
                            updated_at=datetime.now(UTC),
                        )
                    )
                    session.execute(stmt)
                    session.commit()
                self.queue.ack(message_id)
                return False

            # 3. Both permits acquired: now authorize attempt
            attempt_no = self.authorize_attempt(item_id, token, generation)
            if attempt_no is None:
                # Cancelled before attempt could be authorized or lost lease
                self.limiter.release_concurrency_permit(spec.id, permit_id)
                self.queue.ack(message_id)
                return False

            with self.db_mgr.get_session() as session:
                att_rec = session.scalars(
                    select(ExecutionAttemptRecord).where(
                        ExecutionAttemptRecord.item_execution_id == item_id,
                        ExecutionAttemptRecord.attempt_no == attempt_no,
                    )
                ).first()
                current_attempt_id = att_rec.id if att_rec else None

            executor = RemoteAgentExecutor(spec, credential_db_manager=self.db_mgr)
            mapped_payload = map_request(dataset_input, spec.request_mapping)
            # 在可能发送标记、Agent Trace 和 HTTP 调用之前预取凭据。
            try:
                await executor.prepare_credential()
            except ValueError:
                finalized = self.finalize_execution_and_attempt(
                    item_id=item_id, generation=generation, lease_token=token,
                    target_item_status="FAILED", target_eval_status="skipped", target_quality_conclusion="unknown",
                    current_attempt_id=current_attempt_id, execution_error="CREDENTIAL_UNAVAILABLE",
                    attempt_updates={"status": "FAILED", "error_type": "CREDENTIAL_UNAVAILABLE",
                                     "error_message": "CREDENTIAL_UNAVAILABLE", "request_phase": "PREPARED"},
                )
                self.limiter.release_concurrency_permit(spec.id, permit_id)
                await executor._client.aclose()
                self.queue.ack(message_id)
                return finalized
            headers = {
                "Content-Type": "application/json",
                "X-Eval-Launch-Id": launch_id,
                "X-Eval-Dataset-Item-Id": claim_info["dataset_item_id"],
                "X-Eval-Agent-Version": spec.version,
                "Idempotency-Key": f"argus:{item_id}",
            }

            lf = get_langfuse_client_safe()
            trace_id: str | None = None
            trace_url: str | None = None
            obs_id: str | None = None

            # Double-layered observation or OpenTelemetry
            if lf and hasattr(lf, "start_as_current_observation"):
                parent_ctx = lf.start_as_current_observation(
                    as_type="chain",
                    name=f"eval_item_execution:{claim_info['dataset_item_id']}",
                    input=dataset_input,
                    metadata={"launch_id": launch_id, "item_id": item_id, "generation": generation},
                )
            else:
                parent_ctx = None

            # Generate W3C traceparent fallback if not set
            default_tid = uuid.uuid4().hex
            default_sid = uuid.uuid4().hex[:16]

            async def _do_invocation_and_eval():
                nonlocal trace_id, obs_id
                if lf and hasattr(lf, "start_as_current_observation"):
                    with lf.start_as_current_observation(
                        as_type="tool",
                        name="remote-agent-http",
                        input={"agent": f"{spec.agent_id}:{spec.version}", "request": mapped_payload},
                        metadata={"endpoint": spec.endpoint},
                    ) as http_obs:
                        try:
                            inject(headers)
                        except Exception:
                            pass
                        if "traceparent" not in headers:
                            headers["traceparent"] = f"00-{default_tid}-{default_sid}-01"
                        trace_id = lf.get_current_trace_id()
                        obs_id = lf.get_current_observation_id()
                        res = await executor.invoke_once(mapped_payload, headers)
                        http_obs.update(
                            output=res.body,
                            metadata={
                                "http_status": res.status_code,
                                "duration_ms": res.duration_ms,
                                "trace_context_received": res.trace_context_received,
                            },
                        )
                        return res
                else:
                    try:
                        from opentelemetry import trace
                        tracer = trace.get_tracer("argus-eval-runner")
                        with tracer.start_as_current_span(
                            f"eval_item_execution:{claim_info['dataset_item_id']}",
                            attributes={
                                "launch_id": launch_id,
                                "item_id": item_id,
                                "agent_id": spec.agent_id,
                                "agent_version": spec.version,
                            },
                        ):
                            inject(headers)
                    except Exception:
                        pass

                    if "traceparent" not in headers:
                        headers["traceparent"] = f"00-{default_tid}-{default_sid}-01"
                    trace_id = headers["traceparent"].split("-")[1]
                    return await executor.invoke_once(mapped_payload, headers)

            # 解析期间可能已失去租约或被取消，禁止旧 Worker 标记发送或调用 Agent。
            if not current_attempt_id or not self.mark_attempt_dispatched(
                item_id, launch_id, token, generation, current_attempt_id, executor.resolved_credential,
            ):
                self.limiter.release_concurrency_permit(spec.id, permit_id)
                await executor._client.aclose()
                self.queue.ack(message_id)
                return False

            # Start background heartbeat to renew lease AND concurrency permit during invocation
            stop_hb = asyncio.Event()
            lease_lost = asyncio.Event()
            hb_interval = getattr(self, "heartbeat_interval_seconds", getattr(self, "heartbeat_interval", 5.0))
            hb_extension = max(30, int(spec.timeout_seconds) + 15)
            holding_permit = True

            async def _heartbeat_loop():
                while not stop_hb.is_set():
                    try:
                        await asyncio.sleep(hb_interval)
                        if stop_hb.is_set():
                            break
                        renew_ok = await asyncio.to_thread(
                            self.renew_lease, item_id, token, extension_seconds=hb_extension
                        )
                        if not renew_ok:
                            lease_lost.set()
                            break
                        if holding_permit:
                            self.limiter.renew_concurrency_permit(spec.id, permit_id, extra_sec=hb_extension)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        pass

            hb_task = asyncio.create_task(_heartbeat_loop())

            from contextlib import nullcontext
            parent_scope = parent_ctx if parent_ctx is not None else nullcontext()

            try:
                with parent_scope:
                    try:
                        inv_res = await _do_invocation_and_eval()
                    finally:
                        # Always release distributed agent concurrency permit immediately after HTTP finishes
                        holding_permit = False
                        self.limiter.release_concurrency_permit(spec.id, permit_id)

                    if lease_lost.is_set():
                        return False

                    if lf and trace_id and hasattr(lf, "get_trace_url"):
                        try:
                            candidate_trace_url = lf.get_trace_url(trace_id=trace_id)
                            trace_url = candidate_trace_url if isinstance(candidate_trace_url, str) else None
                        except Exception:
                            trace_url = None

                    is_non_idem_read_timeout = (
                        not spec.is_idempotent and inv_res.error_category == "READ_TIMEOUT"
                    )

                    # Determine final phase: only RESPONSE_RECEIVED if we got a response
                    final_phase = "RESPONSE_RECEIVED" if inv_res.status_code is not None else "MAY_HAVE_BEEN_SENT"
                    att_status = "COMPLETED" if inv_res.status_code == 200 else "FAILED"
                    att_err_type = "AMBIGUOUS_OUTCOME" if is_non_idem_read_timeout else inv_res.error_category
                    att_err_msg = (
                        f"AMBIGUOUS_OUTCOME: {inv_res.error_message}"
                        if is_non_idem_read_timeout
                        else inv_res.error_message
                    )

                    att_updates = {
                        "status": att_status,
                        "http_status": inv_res.status_code,
                        "error_type": att_err_type,
                        "error_message": att_err_msg,
                        "latency_ms": inv_res.duration_ms,
                        "trace_context_received": inv_res.trace_context_received,
                        "request_phase": final_phase,
                        "usage_cost": inv_res.usage_cost,
                    }

                    # Check if Launch was cancelled while we were invoking
                    with self.db_mgr.get_session() as session:
                        launch_curr = session.get(ExperimentLaunchRecord, launch_id)
                        launch_cancelled = bool(
                            launch_curr and (launch_curr.cancel_requested_at or launch_curr.status in ("CANCELLING", "CANCELLED"))
                        )

                    dataset_meta = manifest.get("dataset", {})
                    dataset_version = dataset_meta.get("dataset_version") or dataset_meta.get("version")
                    dataset_source = dataset_meta.get("source")
                    dataset_run_name = manifest.get("name") or f"argus-{launch_id[:12]}"

                    if inv_res.status_code == 200 and inv_res.body is not None:
                        # Intercept lease lost before evaluation
                        if lease_lost.is_set():
                            return False

                        # Issue #84: persist the recoverable Agent output BEFORE
                        # evaluating, so a failed evaluation can be retried later
                        # without ever calling the Agent again — even if Langfuse
                        # is unavailable or has not synced.
                        self._persist_execution_checkpoint(
                            item_id=item_id,
                            launch_id=launch_id,
                            dataset_item_id=claim_info["dataset_item_id"],
                            generation=generation,
                            output=inv_res.body,
                            dataset_input=dataset_input,
                            expected_output=expected_output,
                            manifest=manifest,
                            final_attempt_id=current_attempt_id,
                            trace_id=trace_id,
                            trace_url=trace_url,
                            observation_id=obs_id,
                        )

                        # Successful execution -> evaluate quality (ASYNC offloaded via asyncio.to_thread)
                        def _do_evaluation():
                            # Issue #81: shared frozen evaluation boundary; no per-id lookup.
                            result = evaluate_frozen_item(
                                manifest,
                                output=inv_res.body,
                                expected_output=expected_output,
                            )
                            return (
                                result.scores,
                                result.eval_status,
                                result.quality_conclusion,
                                result.eval_error,
                                result.typed_results,
                                result.quality_decision,
                            )

                        (
                            scores_dict,
                            eval_status,
                            quality_conclusion,
                            eval_error,
                            typed_results,
                            quality_decision,
                        ) = await asyncio.to_thread(_do_evaluation)

                        if lease_lost.is_set():
                            # Lost lease ownership during evaluation -> discard results
                            return False

                        self.finalize_execution_and_attempt(
                            item_id=item_id,
                            generation=generation,
                            lease_token=token,
                            target_item_status="SUCCEEDED",
                            target_eval_status=eval_status,
                            target_quality_conclusion=quality_conclusion,
                            current_attempt_id=current_attempt_id,
                            attempt_updates=att_updates,
                            scores=scores_dict,
                            typed_results=list(typed_results),
                            quality_evaluation=quality_decision.payload()
                            if quality_decision is not None
                            else None,
                            eval_error=eval_error,
                            trace_id=trace_id,
                            trace_url=trace_url,
                            observation_id=obs_id,
                            launch_id=launch_id,
                            dataset_item_id=claim_info["dataset_item_id"],
                            dataset_version=dataset_version,
                            dataset_run_name=dataset_run_name,
                            dataset_source=dataset_source,
                        )
                    else:
                        # Intercept lease lost before failure finalization
                        if lease_lost.is_set():
                            return False

                        # Failed attempt
                        if launch_cancelled:
                            self.finalize_execution_and_attempt(
                                item_id=item_id,
                                generation=generation,
                                lease_token=token,
                                target_item_status="CANCELLED",
                                target_eval_status="skipped",
                                target_quality_conclusion="unknown",
                                current_attempt_id=current_attempt_id,
                                attempt_updates=att_updates,
                                execution_error=inv_res.error_message,
                                trace_id=trace_id,
                                trace_url=trace_url,
                                observation_id=obs_id,
                                launch_id=launch_id,
                                dataset_item_id=claim_info["dataset_item_id"],
                                dataset_version=dataset_version,
                                dataset_run_name=dataset_run_name,
                                dataset_source=dataset_source,
                            )
                        elif is_non_idem_read_timeout:
                            self.finalize_execution_and_attempt(
                                item_id=item_id,
                                generation=generation,
                                lease_token=token,
                                target_item_status="FAILED",
                                target_eval_status="skipped",
                                target_quality_conclusion="unknown",
                                current_attempt_id=current_attempt_id,
                                attempt_updates=att_updates,
                                execution_error=f"AMBIGUOUS_OUTCOME: {inv_res.error_message}",
                                trace_id=trace_id,
                                trace_url=trace_url,
                                observation_id=obs_id,
                                launch_id=launch_id,
                                dataset_item_id=claim_info["dataset_item_id"],
                                dataset_version=dataset_version,
                                dataset_run_name=dataset_run_name,
                                dataset_source=dataset_source,
                            )
                        elif inv_res.is_retryable and attempt_no <= spec.max_retries:
                            delay_sec = inv_res.retry_after_seconds or min(2 ** (attempt_no - 1), 30)
                            self.finalize_execution_and_attempt(
                                item_id=item_id,
                                generation=generation,
                                lease_token=token,
                                target_item_status="RETRY_WAIT",
                                target_eval_status="pending",
                                target_quality_conclusion="unknown",
                                current_attempt_id=current_attempt_id,
                                attempt_updates=att_updates,
                                execution_error=inv_res.error_message,
                                retry_available_at=datetime.now(UTC) + timedelta(seconds=delay_sec),
                                trace_id=trace_id,
                                trace_url=trace_url,
                                observation_id=obs_id,
                                launch_id=launch_id,
                                dataset_item_id=claim_info["dataset_item_id"],
                                dataset_version=dataset_version,
                                dataset_run_name=dataset_run_name,
                                dataset_source=dataset_source,
                            )
                        else:
                            terminal_status = "TIMED_OUT" if inv_res.error_category == "READ_TIMEOUT" else "FAILED"
                            self.finalize_execution_and_attempt(
                                item_id=item_id,
                                generation=generation,
                                lease_token=token,
                                target_item_status=terminal_status,
                                target_eval_status="skipped",
                                target_quality_conclusion="unknown",
                                current_attempt_id=current_attempt_id,
                                attempt_updates=att_updates,
                                execution_error=inv_res.error_message,
                                trace_id=trace_id,
                                trace_url=trace_url,
                                observation_id=obs_id,
                                launch_id=launch_id,
                                dataset_item_id=claim_info["dataset_item_id"],
                                dataset_version=dataset_version,
                                dataset_run_name=dataset_run_name,
                                dataset_source=dataset_source,
                            )
            finally:
                stop_hb.set()
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass
                if holding_permit:
                    self.limiter.release_concurrency_permit(spec.id, permit_id)

            self.queue.ack(message_id)
            return True



    def _persist_execution_checkpoint(
        self,
        *,
        item_id: str,
        launch_id: str,
        dataset_item_id: str,
        generation: int,
        output: Any,
        dataset_input: Any,
        expected_output: Any,
        manifest: dict[str, Any],
        final_attempt_id: str | None,
        trace_id: str | None,
        trace_url: str | None,
        observation_id: str | None,
    ) -> None:
        """Write the recoverable Agent output for a successful execution (#84).

        Best-effort: a checkpoint failure must never turn a successful execution
        into a failure. When retention is disabled this is simply a no-op and
        evaluation-only recovery is unavailable for that deployment.
        """
        from .execution_checkpoint import write_execution_checkpoint

        try:
            with self.db_mgr.get_session() as session:
                write_execution_checkpoint(
                    session,
                    item_execution_id=item_id,
                    launch_id=launch_id,
                    dataset_item_id=dataset_item_id,
                    dispatch_generation=generation,
                    output=output,
                    input_payload=dataset_input,
                    expected_output=expected_output,
                    manifest=manifest,
                    final_attempt_id=final_attempt_id,
                    trace_id=trace_id,
                    observation_id=observation_id,
                    langfuse_trace_url=trace_url,
                )
                session.commit()
        except Exception:  # noqa: BLE001 - recovery metadata must not fail execution
            pass

    async def execute_evaluation_message(
        self,
        message_id: str,
        item_id: str,
        evaluation_generation: int,
    ) -> bool:
        """Evaluation-only recovery: re-judge a stored Agent output (Issue #84).

        This path never invokes the Agent and never touches the execution
        attempt or ``dispatch_generation``. It claims a generation-scoped
        evaluation lease, re-runs only the failed / missing Bindings against the
        verified checkpoint, and finalizes under a compare-and-set so a late or
        superseded result can never overwrite the current one.
        """
        async with self.local_concurrency:
            from .evaluation_recovery import recover_evaluation

            # Take the fenced lease first; a duplicate / competing delivery is a
            # no-op here and the message is simply acked.
            outcome = await asyncio.to_thread(
                recover_evaluation,
                self.db_mgr,
                item_id=item_id,
                evaluation_generation=evaluation_generation,
                worker_id=self.worker_id,
            )
            if not outcome.get("claimed"):
                self.queue.ack(message_id)
                return False
            self.queue.ack(message_id)
            return bool(outcome.get("finalized"))
