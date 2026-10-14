"""成功响应后的执行恢复：同 scope、新租约，无 Agent 调用或新 Attempt。"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..db_models import (
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from ..evaluator_binding import EvaluatorBindingError, canonical_digest, evaluate_frozen_item
from ..execution_checkpoint import CheckpointUnavailableError, load_recoverable_checkpoint
from ..runner_identity import validate_runner_identity
from .worker_acquisition import WorkerTrajectoryError


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _claim_response(worker, item_id, generation, token):
    with worker.db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        if item is None:
            return False
        is_pg = session.bind.dialect.name == "postgresql"
        def locked(model, identity):
            statement = select(model).where(model.id == identity)
            if is_pg:
                statement = statement.with_for_update().execution_options(populate_existing=True)
            return session.scalar(statement)
        launch = locked(ExperimentLaunchRecord, item.launch_id)
        if launch is None or (launch.manifest or {}).get("schema_version") != "1.3":
            return None
        item = locked(ExperimentItemExecutionRecord, item_id)
        attempt = locked(ExecutionAttemptRecord, item.active_attempt_id)
        query = select(ExecutionCheckpointRecord).where(ExecutionCheckpointRecord.item_execution_id == item_id,
            ExecutionCheckpointRecord.dispatch_generation == generation)
        if is_pg:
            query = query.with_for_update().execution_options(populate_existing=True)
        checkpoint = session.scalar(query)
        now = session.scalar(select(func.clock_timestamp())) if is_pg else datetime.now(UTC)
        if (item.dispatch_generation != generation or item.lease_token != token or item.execution_status != "running"
            or item.lease_expires_at is None or _utc(item.lease_expires_at) <= _utc(now)):
            return False
        if checkpoint is None:
            if attempt is None or attempt.dispatch_generation != generation:
                return None
            # 原代活动 Attempt 是恢复标记；响应被清理也不得回退新业务调用。
            received = attempt.item_execution_id == item_id and attempt.request_phase == "RESPONSE_RECEIVED"
            cancelled = launch.cancel_requested_at or launch.status in {"CANCELLING", "CANCELLED"}
            item.execution_status = "cancelled" if cancelled else "succeeded" if received else "failed"
            item.eval_status, item.quality_conclusion = "skipped" if cancelled else "failed", "unknown"
            item.eval_error = "TRAJECTORY_RECOVERY_CHECKPOINT_MISSING"
            item.scores, item.quality_evaluation = None, None
            item.final_attempt_id = attempt.id if attempt.item_execution_id == item_id else None
            item.active_attempt_id = item.lease_owner = item.lease_token = item.lease_expires_at = None
            item.completed_at = item.updated_at = now
            if attempt.item_execution_id == item_id:
                attempt.status, attempt.completed_at = "SUCCEEDED" if received else "FAILED", now
                attempt.lease_token = token
            return True
        if (attempt is None or attempt.item_execution_id != item_id or attempt.dispatch_generation != generation
            or attempt.status != "RUNNING"):
            # 保存响应存在但原 Attempt 不合法，不能授权新的业务调用。
            item.execution_status, item.eval_status, item.quality_conclusion = "failed", "failed", "unknown"
            item.eval_error = "TRAJECTORY_RECOVERY_ATTEMPT_INVALID"
            item.scores, item.quality_evaluation = None, None
            item.active_attempt_id = item.lease_owner = item.lease_token = item.lease_expires_at = None
            item.completed_at = item.updated_at = now
            return True
        # 只转移写入 fence；原业务执行者与 Attempt 身份不重写。
        attempt.lease_token = token
        cancelled = bool(launch.cancel_requested_at or launch.status in {"CANCELLING", "CANCELLED"})
        error = None
        try:
            load_recoverable_checkpoint(session, item_execution_id=item_id, dispatch_generation=generation,
                manifest=launch.manifest, now=_utc(now))
            if (checkpoint.launch_id != launch.id or checkpoint.dataset_item_id != item.dataset_item_id
                or checkpoint.final_attempt_id != attempt.id or checkpoint.manifest_digest != canonical_digest(launch.manifest)):
                raise ValueError
            matches = [row for row in launch.manifest.get("dataset", {}).get("items", [])
                       if str(row.get("id")) == item.dataset_item_id]
            if (len(matches) != 1 or canonical_digest({"input": matches[0].get("input") if matches[0].get("input") is not None else {},
                    "expected": matches[0].get("expected_output")}) != canonical_digest({
                    "input": checkpoint.input_payload, "expected": checkpoint.expected_output})):
                raise ValueError
        except CheckpointUnavailableError as exc:
            error = "TRAJECTORY_RECOVERY_" + exc.code
        except (ValueError, TypeError, AttributeError, EvaluatorBindingError):
            error = "TRAJECTORY_RECOVERY_SOURCE_MISMATCH"
        identity_error = validate_runner_identity(launch.manifest.get("runner"), worker.runner_identity)
        if identity_error:
            error = "TRAJECTORY_RECOVERY_RUNNER_MISMATCH"
        response = {key: getattr(checkpoint, key) for key in (
            "agent_output", "expected_output", "trace_id", "observation_id", "langfuse_trace_url")}
        response.update(manifest=launch.manifest, attempt_id=attempt.id, launch_id=launch.id,
                        dataset_item_id=item.dataset_item_id, error=error, cancelled=cancelled)
        session.commit()
        return response


async def resume_successful_response(worker, *, item_id, generation, lease_token):
    """None 表示没有当前代响应；其余结果禁止进入 Agent 许可/凭据/调用路径。"""
    response = await asyncio.to_thread(_claim_response, worker, item_id, generation, lease_token)
    if response is None or type(response) is bool:
        return response
    lost = asyncio.Event()
    async def heartbeat():
        while True:
            await asyncio.sleep(getattr(worker, "heartbeat_interval_seconds", worker.heartbeat_interval))
            try:
                valid = await asyncio.to_thread(worker.renew_lease, item_id, lease_token, extension_seconds=30)
            except Exception:
                valid = False
            if not valid:
                lost.set()
                return
    task = asyncio.create_task(heartbeat())
    try:
        manifest = response["manifest"]
        error = response["error"]
        result = None
        if not response["cancelled"] and error is None:
            try:
                await worker.trajectory_collector.collect(manifest, item_id=item_id, generation=generation,
                    lease_token=lease_token, cancelled=lost)
            except WorkerTrajectoryError as exc:
                if exc.abandoned:
                    return False
                logging.getLogger(__name__).warning("Trajectory recovery diagnostic: %s", exc.reason_code)
            except asyncio.CancelledError:
                if lost.is_set():
                    return False
                raise
            if lost.is_set():
                return False
            def evaluate_response():
                from .inputs import context_for_item
                with worker.db_mgr.get_session() as session:
                    context = context_for_item(session, item_id=item_id, generation=generation, manifest=manifest)
                return evaluate_frozen_item(manifest, output=response["agent_output"],
                    expected_output=response["expected_output"], context=context)
            result = await asyncio.to_thread(evaluate_response)
        if lost.is_set():
            return False
        dataset = manifest.get("dataset", {})
        return worker.finalize_execution_and_attempt(item_id=item_id, generation=generation, lease_token=lease_token,
            target_item_status="CANCELLED" if response["cancelled"] else "SUCCEEDED",
            target_eval_status=result.eval_status if result else "skipped" if response["cancelled"] else "failed",
            target_quality_conclusion=result.quality_conclusion if result else "unknown",
            current_attempt_id=response["attempt_id"], attempt_updates={"status": "SUCCEEDED", "request_phase": "RESPONSE_RECEIVED"},
            scores=result.scores if result else None, typed_results=list(result.typed_results) if result else None,
            quality_evaluation=result.quality_decision.payload() if result and result.quality_decision else None,
            eval_error=result.eval_error if result else error, trace_id=response["trace_id"],
            trace_url=response["langfuse_trace_url"], observation_id=response["observation_id"],
            launch_id=response["launch_id"], dataset_item_id=response["dataset_item_id"],
            dataset_version=dataset.get("dataset_version") or dataset.get("version"), dataset_source=dataset.get("source"),
            dataset_run_name=manifest.get("name") or "argus-" + response["launch_id"][:12], respect_launch_cancellation=True)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
