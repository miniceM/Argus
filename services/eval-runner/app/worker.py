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
from .evaluators import default_evaluator_registry, evaluate_item_quality
from .executor import RemoteAgentExecutor
from .limiter import DistributedAgentLimiter
from .metrics import runtime_metrics
from .queue import QueueAdapter
from .registry import AgentVersionSpec, map_request

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
    ):
        self.db_mgr = db_mgr
        self.queue = queue
        self.limiter = limiter
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.local_concurrency = asyncio.Semaphore(10)
        self.heartbeat_interval = 5.0

    def poll_queue(self, count: int = 10, block_ms: int = 1000) -> list[tuple[str, str, int]]:
        """Synchronously reads/claims items from the queue adapter, meant to be run in asyncio.to_thread."""
        return self.queue.read_group(self.worker_id, count=count, block_ms=block_ms)

    def renew_lease(self, item_id: str, lease_token: str, extension_seconds: int = 30) -> bool:
        """Renews active lease timestamp for in-flight items. Fails if lease already expired."""
        now = datetime.now(UTC)
        with self.db_mgr.get_session() as session:
            stmt = (
                update(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.id == item_id,
                    ExperimentItemExecutionRecord.lease_token == lease_token,
                    ExperimentItemExecutionRecord.execution_status == "running",
                    ExperimentItemExecutionRecord.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=extension_seconds),
                    updated_at=now,
                )
            )
            res = session.execute(stmt)
            session.commit()
            return res.rowcount == 1

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
    ) -> int | None:
        """Authorizes attempt creation serialized with launch cancellation and strict ownership verification.
        Global lock ordering: Launch -> Item. Locks are released immediately after Attempt creation.
        """
        now = datetime.now(UTC)
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"

        with self.db_mgr.get_session() as session:
            # First find item to know launch_id
            item_pre = session.get(ExperimentItemExecutionRecord, item_id)
            if not item_pre:
                return None

            # 1. Lock Launch
            launch_stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.id == item_pre.launch_id)
            if is_pg:
                launch_stmt = launch_stmt.with_for_update()
            launch = session.scalars(launch_stmt).first()
            if not launch:
                return None

            # 2. Lock Item and verify strict lease ownership in both cancelled and uncancelled branches
            item_stmt = (
                select(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.id == item_id,
                    ExperimentItemExecutionRecord.dispatch_generation == generation,
                    ExperimentItemExecutionRecord.lease_token == lease_token,
                    ExperimentItemExecutionRecord.execution_status == "running",
                    ExperimentItemExecutionRecord.lease_expires_at > (func.clock_timestamp() if is_pg else now),
                )
            )
            if is_pg:
                item_stmt = item_stmt.with_for_update()
            item = session.scalars(item_stmt).first()
            if not item:
                # Lost ownership or expired! Exit safely without modifying anything.
                session.rollback()
                return None

            # Check collaborative cancellation
            if launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"):
                item.execution_status = "cancelled"
                item.active_attempt_id = None
                item.lease_owner = None
                item.lease_token = None
                item.lease_expires_at = None
                item.updated_at = now
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
                started_at=now,
            )
            session.add(att_rec)
            item.active_attempt_id = att_id
            item.updated_at = now
            # Commit immediately to release row locks before invoking HTTP
            session.commit()
            return next_attempt_no

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
        execution_error: str | None = None,
        eval_error: str | None = None,
        retry_available_at: datetime | None = None,
        trace_id: str | None = None,
        observation_id: str | None = None,
        launch_id: str | None = None,
        dataset_item_id: str | None = None,
        dataset_version: str | None = None,
        dataset_run_name: str | None = None,
    ) -> bool:
        """Atomic finalization for all outcome branches.
        Requires active_attempt_id CAS matching and Attempt status RUNNING.
        Rolls back entirely if either row count != 1.
        """
        now = datetime.now(UTC)
        is_pg = self.db_mgr.engine.dialect.name == "postgresql"
        clock_expr = func.clock_timestamp() if is_pg else now

        with self.db_mgr.get_session() as session:
            # Phase 1: Lock row
            lock_stmt = select(ExperimentItemExecutionRecord.id).where(ExperimentItemExecutionRecord.id == item_id)
            if is_pg:
                lock_stmt = lock_stmt.with_for_update()
            if not session.scalars(lock_stmt).first():
                return False

            # Phase 2: CAS update item
            item_where = [
                ExperimentItemExecutionRecord.id == item_id,
                ExperimentItemExecutionRecord.dispatch_generation == generation,
                ExperimentItemExecutionRecord.lease_token == lease_token,
                ExperimentItemExecutionRecord.execution_status == "running",
                ExperimentItemExecutionRecord.lease_expires_at > clock_expr,
            ]
            if current_attempt_id is not None:
                item_where.append(ExperimentItemExecutionRecord.active_attempt_id == current_attempt_id)
            else:
                item_where.append(ExperimentItemExecutionRecord.active_attempt_id.is_(None))

            is_retry_wait = target_item_status.lower() == "retry_wait"
            item_values: dict[str, Any] = {
                "execution_status": target_item_status.lower(),
                "eval_status": target_eval_status.lower(),
                "quality_conclusion": target_quality_conclusion.lower(),
                "final_attempt_id": current_attempt_id,
                "active_attempt_id": None,
                "scores": scores,
                "execution_error": execution_error,
                "eval_error": eval_error,
                "available_at": retry_available_at,
                "trace_id": trace_id,
                "observation_id": observation_id,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "completed_at": None if is_retry_wait else now,
                "updated_at": now,
            }

            stmt_item = (
                update(ExperimentItemExecutionRecord)
                .where(*item_where)
                .values(**item_values)
            )
            res_item = session.execute(stmt_item)
            if res_item.rowcount != 1:
                session.rollback()
                return False

            # Phase 3: CAS update attempt
            if current_attempt_id is not None and attempt_updates is not None:
                att_values = dict(attempt_updates)
                att_values.setdefault("completed_at", now)
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

            # Phase 4: Atomic insertion of Langfuse Outbox task if applicable
            if (
                not is_retry_wait
                and launch_id
                and dataset_item_id
                and dataset_run_name
                and trace_id
            ):
                task_id = str(uuid.uuid4())
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
                    scores_payload=scores or {},
                    status="PENDING",
                    attempts=0,
                    next_retry_at=now,
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
    ) -> bool:
        """Full pipeline: reserve local slot -> claim -> authorize attempt -> invoke -> finalize -> ack."""
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
                max_concurrency=policy_dict["max_concurrency"],
                is_idempotent=bool(agent_dict.get("is_idempotent", False)),
                credential_ref=agent_dict.get("credential_ref"),
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

            try:
                # 3. Both permits acquired: now authorize attempt
                attempt_no = self.authorize_attempt(item_id, token, generation)
                if attempt_no is None:
                    # Cancelled before attempt could be authorized or lost lease
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

                executor = RemoteAgentExecutor(spec)
                mapped_payload = map_request(dataset_input, spec.request_mapping)
                headers = {
                    "Content-Type": "application/json",
                    "X-Eval-Launch-Id": launch_id,
                    "X-Eval-Dataset-Item-Id": claim_info["dataset_item_id"],
                    "X-Eval-Agent-Version": spec.version,
                    "Idempotency-Key": f"argus:{item_id}",
                }

                lf = get_langfuse_client_safe()
                trace_id: str | None = None
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

                # Mark attempt phase as MAY_HAVE_BEEN_SENT before network call
                if current_attempt_id:
                    with self.db_mgr.get_session() as session:
                        att = session.get(ExecutionAttemptRecord, current_attempt_id)
                        if att:
                            att.request_phase = "MAY_HAVE_BEEN_SENT"
                            session.commit()

                # Start background heartbeat to renew lease AND concurrency permit during invocation
                stop_hb = asyncio.Event()
                hb_interval = getattr(self, "heartbeat_interval", 5.0)
                hb_extension = max(30, int(spec.timeout_seconds) + 15)

                async def _heartbeat_loop():
                    while not stop_hb.is_set():
                        try:
                            await asyncio.sleep(hb_interval)
                            if stop_hb.is_set():
                                break
                            self.renew_lease(item_id, token, extension_seconds=hb_extension)
                            self.limiter.renew_concurrency_permit(spec.id, permit_id, extra_sec=hb_extension)
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            pass

                hb_task = asyncio.create_task(_heartbeat_loop())

                try:
                    if parent_ctx:
                        with parent_ctx:
                            inv_res = await _do_invocation_and_eval()
                    else:
                        inv_res = await _do_invocation_and_eval()
                finally:
                    stop_hb.set()
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass

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
                }

            finally:
                # Always release distributed concurrency permit
                self.limiter.release_concurrency_permit(spec.id, permit_id)

            # Check if Launch was cancelled while we were invoking
            with self.db_mgr.get_session() as session:
                launch_curr = session.get(ExperimentLaunchRecord, launch_id)
                launch_cancelled = bool(
                    launch_curr and (launch_curr.cancel_requested_at or launch_curr.status in ("CANCELLING", "CANCELLED"))
                )

            dataset_version = manifest.get("dataset", {}).get("version")
            dataset_run_name = manifest.get("name") or f"argus-{launch_id[:12]}"

            if inv_res.status_code == 200 and inv_res.body is not None:
                # Successful execution -> evaluate quality
                scores_dict: dict[str, float] = {}
                item_eval_specs = [
                    ev for ev in manifest.get("evaluators", [])
                    if ev.get("scope", "item") == "item"
                ]
                eval_status = "succeeded" if item_eval_specs else "skipped"
                quality_conclusion = "unknown"
                eval_error = None

                try:
                    for ev in item_eval_specs:
                        ev_fn = default_evaluator_registry.get_evaluator_fn(ev["id"], ev.get("version"))
                        ev_res = ev_fn(output=inv_res.body, expected_output=expected_output)
                        scores_dict[ev["id"]] = float(getattr(ev_res, "value", 0.0))
                    if item_eval_specs:
                        quality_conclusion = evaluate_item_quality(
                            scores_dict, item_eval_specs, manifest.get("quality_policy")
                        )
                except Exception as exc:
                    eval_status = "failed"
                    eval_error = str(exc)

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
                    eval_error=eval_error,
                    trace_id=trace_id,
                    observation_id=obs_id,
                    launch_id=launch_id,
                    dataset_item_id=claim_info["dataset_item_id"],
                    dataset_version=dataset_version,
                    dataset_run_name=dataset_run_name,
                )
            else:
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
                        observation_id=obs_id,
                        launch_id=launch_id,
                        dataset_item_id=claim_info["dataset_item_id"],
                        dataset_version=dataset_version,
                        dataset_run_name=dataset_run_name,
                    )
                elif is_non_idem_read_timeout:
                    self.finalize_execution_and_attempt(
                        item_id=item_id,
                        generation=generation,
                        lease_token=token,
                        target_item_status="FAILED",
                        target_eval_status="skipped",
                        target_quality_conclusion="fail",
                        current_attempt_id=current_attempt_id,
                        attempt_updates=att_updates,
                        execution_error=f"AMBIGUOUS_OUTCOME: {inv_res.error_message}",
                        trace_id=trace_id,
                        observation_id=obs_id,
                        launch_id=launch_id,
                        dataset_item_id=claim_info["dataset_item_id"],
                        dataset_version=dataset_version,
                        dataset_run_name=dataset_run_name,
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
                        observation_id=obs_id,
                    )
                else:
                    terminal_status = "TIMED_OUT" if inv_res.error_category == "READ_TIMEOUT" else "FAILED"
                    self.finalize_execution_and_attempt(
                        item_id=item_id,
                        generation=generation,
                        lease_token=token,
                        target_item_status=terminal_status,
                        target_eval_status="skipped",
                        target_quality_conclusion="fail",
                        current_attempt_id=current_attempt_id,
                        attempt_updates=att_updates,
                        execution_error=inv_res.error_message,
                        trace_id=trace_id,
                        observation_id=obs_id,
                        launch_id=launch_id,
                        dataset_item_id=claim_info["dataset_item_id"],
                        dataset_version=dataset_version,
                        dataset_run_name=dataset_run_name,
                    )

            self.queue.ack(message_id)
            return True

