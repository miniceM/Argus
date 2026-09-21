from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .evaluators import default_evaluator_registry, evaluate_item_quality
from .executor import RemoteAgentExecutor
from .limiter import DistributedAgentLimiter
from .queue import QueueAdapter
from .registry import AgentVersionSpec, map_request


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
    ) -> int | None:
        """Authorizes attempt creation serialized with launch cancellation."""
        now = datetime.now(UTC)
        with self.db_mgr.get_session() as session:
            item = session.get(ExperimentItemExecutionRecord, item_id)
            if not item or item.lease_token != lease_token:
                return None

            launch = session.get(ExperimentLaunchRecord, item.launch_id)
            if not launch or launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"):
                # Collaborative cancellation: reject new attempt
                item.execution_status = "cancelled"
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

            att_rec = ExecutionAttemptRecord(
                id=str(uuid.uuid4()),
                item_execution_id=item_id,
                attempt_no=next_attempt_no,
                status="RUNNING",
                worker_id=self.worker_id,
                request_phase="PREPARED",
                started_at=now,
            )
            session.add(att_rec)
            session.commit()
            return next_attempt_no

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
        """Finalizes item in DB using strict lease fencing. Time is checked against DB timestamp."""
        now = datetime.now(UTC)
        with self.db_mgr.get_session() as session:
            stmt = (
                update(ExperimentItemExecutionRecord)
                .where(
                    ExperimentItemExecutionRecord.id == item_id,
                    ExperimentItemExecutionRecord.dispatch_generation == generation,
                    ExperimentItemExecutionRecord.lease_token == lease_token,
                    ExperimentItemExecutionRecord.execution_status == "running",
                    ExperimentItemExecutionRecord.lease_expires_at > now,
                )
                .values(
                    execution_status=status.lower(),
                    eval_status=eval_status.lower(),
                    quality_conclusion=quality_conclusion.lower(),
                    final_attempt_id=final_attempt_id,
                    scores=scores,
                    execution_error=execution_error,
                    eval_error=eval_error,
                    completed_at=now,
                    updated_at=now,
                )
            )
            res = session.execute(stmt)
            if res.rowcount == 1:
                session.commit()
                return True
            session.rollback()
            return False

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

            attempt_no = self.authorize_attempt(item_id, token)
            if attempt_no is None:
                # Cancelled before attempt could be authorized
                self.queue.ack(message_id)
                return False

            # Retrieve launch manifest and item input
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

                # Find the running attempt record ID
                att_rec = session.scalars(
                    select(ExecutionAttemptRecord).where(
                        ExecutionAttemptRecord.item_execution_id == item_id,
                        ExecutionAttemptRecord.attempt_no == attempt_no,
                    )
                ).first()
                current_attempt_id = att_rec.id if att_rec else None

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
                id=agent_dict.get("agent_version_id", f"{agent_dict['agent_id']}-{agent_dict['version']}"),
            )

            # Acquire distributed rate & concurrency permit
            permit_id = self.limiter.acquire_concurrency_permit(spec.id, spec.max_concurrency, owner_id=self.worker_id)
            if not permit_id:
                # Could not acquire permit -> delay to RETRY_WAIT and release lease
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
                        )
                    )
                    session.execute(stmt)
                    session.commit()
                self.queue.ack(message_id)
                return False

            executor = RemoteAgentExecutor(spec)
            mapped_payload = map_request(dataset_input, spec.request_mapping)
            headers = {
                "Content-Type": "application/json",
                "X-Eval-Launch-Id": launch_id,
                "X-Eval-Dataset-Item-Id": claim_info["dataset_item_id"],
                "X-Eval-Agent-Version": spec.version,
                "Idempotency-Key": f"argus:{item_id}",
            }
            try:
                from opentelemetry.propagate import inject
                inject(headers)
            except Exception:
                pass

            # Mark attempt phase as MAY_HAVE_BEEN_SENT
            if current_attempt_id:
                with self.db_mgr.get_session() as session:
                    att = session.get(ExecutionAttemptRecord, current_attempt_id)
                    if att:
                        att.request_phase = "MAY_HAVE_BEEN_SENT"
                        session.commit()

            # Invoke remote agent once
            inv_res = await executor.invoke_once(mapped_payload, headers)

            # Update attempt record with outcome
            if current_attempt_id:
                with self.db_mgr.get_session() as session:
                    att = session.get(ExecutionAttemptRecord, current_attempt_id)
                    if att:
                        att.status = "COMPLETED" if inv_res.status_code == 200 else "FAILED"
                        att.http_status = inv_res.status_code
                        att.error_type = inv_res.error_category
                        att.error_message = inv_res.error_message
                        att.latency_ms = inv_res.duration_ms
                        att.trace_context_received = inv_res.trace_context_received
                        att.request_phase = "RESPONSE_RECEIVED"
                        att.completed_at = datetime.now(UTC)
                        session.commit()

            # Release distributed permit
            self.limiter.release_concurrency_permit(spec.id, permit_id)

            # Check if Launch was cancelled while we were invoking
            with self.db_mgr.get_session() as session:
                launch_curr = session.get(ExperimentLaunchRecord, launch_id)
                launch_cancelled = bool(launch_curr and (launch_curr.cancel_requested_at or launch_curr.status in ("CANCELLING", "CANCELLED")))

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

                self.finalize_item(
                    item_id=item_id,
                    generation=generation,
                    lease_token=token,
                    status="SUCCEEDED",
                    eval_status=eval_status,
                    quality_conclusion=quality_conclusion,
                    final_attempt_id=current_attempt_id,
                    scores=scores_dict,
                    eval_error=eval_error,
                )
            else:
                # Failed attempt
                # If launch is cancelled, transition straight to CANCELLED (no retries!)
                if launch_cancelled:
                    self.finalize_item(
                        item_id=item_id,
                        generation=generation,
                        lease_token=token,
                        status="CANCELLED",
                        eval_status="skipped",
                        quality_conclusion="unknown",
                        final_attempt_id=current_attempt_id,
                        execution_error=inv_res.error_message,
                    )
                elif inv_res.is_retryable and attempt_no <= spec.max_retries:
                    # Retryable error with remaining budget -> enter RETRY_WAIT
                    delay_sec = inv_res.retry_after_seconds or min(2 ** (attempt_no - 1), 30)
                    with self.db_mgr.get_session() as session:
                        stmt = (
                            update(ExperimentItemExecutionRecord)
                            .where(
                                ExperimentItemExecutionRecord.id == item_id,
                                ExperimentItemExecutionRecord.lease_token == token,
                                ExperimentItemExecutionRecord.execution_status == "running",
                            )
                            .values(
                                execution_status="retry_wait",
                                available_at=datetime.now(UTC) + timedelta(seconds=delay_sec),
                                lease_owner=None,
                                lease_token=None,
                                lease_expires_at=None,
                                execution_error=inv_res.error_message,
                                updated_at=datetime.now(UTC),
                            )
                        )
                        session.execute(stmt)
                        session.commit()
                else:
                    # Non-retryable error or budget exhausted
                    terminal_status = "TIMED_OUT" if inv_res.error_category == "READ_TIMEOUT" else "FAILED"
                    self.finalize_item(
                        item_id=item_id,
                        generation=generation,
                        lease_token=token,
                        status=terminal_status,
                        eval_status="skipped",
                        quality_conclusion="fail",
                        final_attempt_id=current_attempt_id,
                        execution_error=inv_res.error_message,
                    )

            self.queue.ack(message_id)
            return True
