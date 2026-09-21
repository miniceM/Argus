from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from opentelemetry.propagate import inject
from sqlalchemy import select

from .config import settings
from .dataset import parse_dataset_version
from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .evaluators import default_evaluator_registry, evaluate_item_quality
from .executor import RemoteAgentExecutor, aggregate_launch_status
from .manifest import acquire_launch_execution
from .registry import AgentRegistry, AgentVersionSpec, map_request


def get_langfuse_client_safe():
    """Safely obtain Langfuse client if configured; returns None if not available."""
    try:
        from .main import _client
        return _client()
    except Exception:
        try:
            from langfuse import get_client
            return get_client()
        except Exception:
            return None


async def _execute_single_item(
    db_mgr: DatabaseManager,
    executor: RemoteAgentExecutor,
    launch_id: str,
    item_row: dict[str, Any],
    spec: AgentVersionSpec,
    manifest: dict[str, Any],
    lf: Any = None,
) -> dict[str, Any]:
    item_id = str(item_row.get("id", ""))
    dataset_input = item_row.get("input", {})
    expected_output = item_row.get("expected_output", {})

    item_exec_id = str(uuid.uuid4())
    started_at = datetime.utcnow()

    # Pre-create or reuse ExperimentItemExecution record
    with db_mgr.get_session() as session:
        existing = session.scalars(
            select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == launch_id,
                ExperimentItemExecutionRecord.dataset_item_id == item_id,
            )
        ).first()
        if existing:
            item_exec_id = existing.id
            existing.execution_status = "running"
            existing.started_at = started_at
        else:
            item_rec = ExperimentItemExecutionRecord(
                id=item_exec_id,
                launch_id=launch_id,
                dataset_item_id=item_id,
                execution_status="running",
                eval_status="pending",
                quality_conclusion="unknown",
                started_at=started_at,
            )
            session.add(item_rec)

    last_attempt_id: str | None = None

    def on_attempt_start(attempt_no: int) -> str:
        nonlocal last_attempt_id
        att_id = str(uuid.uuid4())
        last_attempt_id = att_id
        with db_mgr.get_session() as session:
            att_rec = ExecutionAttemptRecord(
                id=att_id,
                item_execution_id=item_exec_id,
                attempt_no=attempt_no,
                status="RUNNING",
                started_at=datetime.utcnow(),
            )
            session.add(att_rec)
        return att_id

    def on_attempt_end(
        att_id: str | None,
        http_status: int | None,
        error_type: str | None,
        error_message: str | None,
        latency_ms: int,
        trace_received: bool,
    ) -> None:
        if not att_id:
            return
        with db_mgr.get_session() as session:
            att = session.get(ExecutionAttemptRecord, att_id)
            if att:
                att.status = "COMPLETED" if error_type is None else "FAILED"
                att.http_status = http_status
                att.error_type = str(error_type) if error_type else None
                att.error_message = error_message
                att.latency_ms = latency_ms
                att.trace_context_received = trace_received
                att.completed_at = datetime.utcnow()

    mapped_payload = map_request(dataset_input, spec.request_mapping)
    headers = {
        "Content-Type": "application/json",
        "X-Eval-Launch-Id": launch_id,
        "X-Eval-Dataset-Item-Id": item_id,
        "X-Eval-Agent-Version": spec.version,
    }

    try:
        inject(headers)
    except Exception:
        pass

    execution_status = "succeeded"
    eval_status = "succeeded"
    quality_conclusion = "unknown"
    execution_error: str | None = None
    eval_error: str | None = None
    scores_dict: dict[str, float] = {}
    agent_output: dict[str, Any] | None = None

    try:
        if lf and hasattr(lf, "start_as_current_observation"):
            with lf.start_as_current_observation(
                as_type="tool",
                name="remote-agent-http",
                input={"agent": f"{spec.agent_id}:{spec.version}", "request": mapped_payload},
                metadata={"endpoint": spec.endpoint, "execution_mode": "SYNC_HTTP"},
            ) as call_observation:
                call_res = await executor.invoke(
                    mapped_payload,
                    headers,
                    on_attempt_start=on_attempt_start,
                    on_attempt_end=on_attempt_end,
                )
                agent_output = call_res.body
                call_observation.update(
                    output=call_res.body,
                    metadata={
                        "endpoint": spec.endpoint,
                        "http_status": call_res.status_code,
                        "attempts": call_res.attempts,
                        "duration_ms": call_res.duration_ms,
                        "trace_context_received": call_res.trace_context_received,
                    },
                )
        else:
            call_res = await executor.invoke(
                mapped_payload,
                headers,
                on_attempt_start=on_attempt_start,
                on_attempt_end=on_attempt_end,
            )
            agent_output = call_res.body
    except Exception as exc:
        execution_status = "failed"
        execution_error = str(exc)
        eval_status = "skipped"
        quality_conclusion = "fail"

    if execution_status == "succeeded" and agent_output is not None:
        item_eval_specs = [
            ev_spec for ev_spec in manifest.get("evaluators", [])
            if ev_spec.get("scope", "item") == "item"
        ]
        if not item_eval_specs:
            eval_status = "skipped"
            quality_conclusion = "unknown"
        else:
            try:
                for ev_spec in item_eval_specs:
                    ev_id = ev_spec["id"]
                    ev_fn = default_evaluator_registry.get_evaluator_fn(ev_id, ev_spec.get("version"))
                    ev_res = ev_fn(output=agent_output, expected_output=expected_output)
                    scores_dict[ev_id] = float(getattr(ev_res, "value", 0.0))

                quality_conclusion = evaluate_item_quality(
                    scores_dict,
                    item_eval_specs,
                    manifest.get("quality_policy"),
                )
            except Exception as exc:
                eval_status = "failed"
                eval_error = str(exc)
                quality_conclusion = "unknown"

    completed_at = datetime.utcnow()
    with db_mgr.get_session() as session:
        rec = session.get(ExperimentItemExecutionRecord, item_exec_id)
        if rec:
            rec.execution_status = execution_status
            rec.eval_status = eval_status
            rec.quality_conclusion = quality_conclusion
            rec.execution_error = execution_error
            rec.eval_error = eval_error
            rec.scores = scores_dict
            if last_attempt_id:
                att = session.get(ExecutionAttemptRecord, last_attempt_id)
                if not att or att.item_execution_id != item_exec_id:
                    raise ValueError(f"Attempt '{last_attempt_id}' does not belong to item '{item_exec_id}'")
                rec.final_attempt_id = last_attempt_id
            rec.completed_at = completed_at

    return {
        "dataset_item_id": item_id,
        "execution_status": execution_status,
        "eval_status": eval_status,
        "quality_conclusion": quality_conclusion,
        "scores": scores_dict,
        "output": agent_output,
        "error": execution_error or eval_error,
    }


class LaunchExecutionService:
    """Unified service for executing versioned experiment launches with Langfuse sync."""

    def __init__(self, db_manager: DatabaseManager, registry: AgentRegistry, gather_fn: Any = None):
        self.db_manager = db_manager
        self.registry = registry
        self.gather_fn = gather_fn or asyncio.gather

    async def execute_launch(self, launch_id: str) -> ExperimentLaunchRecord:
        # 1. Acquire atomic execution lock
        acquired = acquire_launch_execution(self.db_manager, launch_id)
        if not acquired:
            with self.db_manager.get_session() as session:
                launch = session.get(ExperimentLaunchRecord, launch_id)
                if not launch:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Launch '{launch_id}' not found",
                    )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Launch '{launch_id}' is already in status '{launch.status}', duplicate run rejected.",
                )

        try:
            # 2. Load launch and frozen manifest
            with self.db_manager.get_session() as session:
                launch = session.get(ExperimentLaunchRecord, launch_id)
                assert launch is not None
                manifest = launch.manifest
                agent_spec_dict = manifest["agent"]
                exec_policy = manifest["execution_policy"]
                dataset_name = launch.dataset_name
                dataset_version = launch.dataset_version
                experiment_name = launch.name

            spec = AgentVersionSpec(
                agent_id=agent_spec_dict["agent_id"],
                version=agent_spec_dict["version"],
                endpoint=agent_spec_dict["endpoint"],
                method=agent_spec_dict["method"],
                timeout_seconds=exec_policy["timeout_seconds"],
                max_retries=exec_policy["max_retries"],
                rate_limit_per_minute=exec_policy["rate_limit_per_minute"],
                request_mapping=agent_spec_dict["request_mapping"],
                max_concurrency=exec_policy["max_concurrency"],
                credential_ref=agent_spec_dict.get("credential_ref"),
                id=agent_spec_dict.get("agent_version_id", ""),
                is_idempotent=bool(agent_spec_dict.get("is_idempotent", False)),
            )

            executor = RemoteAgentExecutor(spec)
            items = manifest.get("dataset", {}).get("items", [])

            # 3. Check Langfuse integration
            lf = get_langfuse_client_safe()
            lf_dataset = None
            if lf:
                try:
                    version_dt = None
                    try:
                        version_dt = parse_dataset_version(dataset_version)
                    except ValueError:
                        pass

                    if version_dt is not None:
                        lf_dataset = lf.get_dataset(dataset_name, version=version_dt)
                    else:
                        lf_dataset = lf.get_dataset(dataset_name)
                except Exception:
                    lf_dataset = None

            run_id = None
            run_url = None
            sync_status = "NOT_APPLICABLE"
            sync_error = None

            # 4. Run items with Langfuse experiment if dataset is available
            if lf and lf_dataset and hasattr(lf_dataset, "run_experiment"):
                frozen_eval_specs = manifest.get("evaluators", [])
                frozen_item_evaluators = [
                    default_evaluator_registry.get_evaluator_fn(ev["id"], ev.get("version"))
                    for ev in frozen_eval_specs
                    if ev.get("scope", "item") == "item"
                ]
                frozen_run_evaluators = [
                    default_evaluator_registry.get_evaluator_fn(ev["id"], ev.get("version"))
                    for ev in frozen_eval_specs
                    if ev.get("scope") == "run"
                ]

                item_results_map: list[dict[str, Any]] = []

                async def remote_task(*, item: Any, **_: Any) -> dict[str, Any]:
                    item_id = str(getattr(item, "id", ""))
                    item_row = next((r for r in items if str(r.get("id")) == item_id), None)
                    if item_row is None:
                        item_row = {
                            "id": item_id,
                            "input": getattr(item, "input", {}),
                            "expected_output": getattr(item, "expected_output", {}),
                        }
                    res = await _execute_single_item(
                        self.db_manager, executor, launch_id, item_row, spec, manifest, lf=lf
                    )
                    item_results_map.append(res)
                    return res.get("output") or {}

                try:
                    result = lf_dataset.run_experiment(
                        name=experiment_name,
                        description="Argus Versioned Launch with Langfuse System of Record",
                        task=remote_task,
                        evaluators=frozen_item_evaluators,
                        run_evaluators=frozen_run_evaluators,
                        max_concurrency=spec.max_concurrency,
                        metadata={
                            "launch_id": launch_id,
                            "agent_id": spec.agent_id,
                            "agent_version": spec.version,
                            "runner_version": settings.runner_version,
                            "execution_mode": "SYNC_HTTP",
                        },
                    )
                    if asyncio.iscoroutine(result):
                        result = await result

                    if hasattr(lf, "flush"):
                        lf.flush()

                    run_id = (
                        getattr(result, "experiment_id", None)
                        or getattr(result, "dataset_run_id", None)
                        or getattr(result, "id", None)
                    )
                    run_url = getattr(result, "dataset_run_url", None)
                    if not run_url and run_id:
                        base = settings.langfuse_base_url.rstrip("/")
                        run_url = f"{base}/project/poc-project/datasets/{dataset_name}/runs/{run_id}"

                    sync_status = "SYNCED"
                except Exception as exc:
                    sync_status = "FAILED"
                    sync_error = str(exc)

                if sync_status == "FAILED" or (items and len(item_results_map) < len(items)):
                    agg_status = "FAILED"
                    agg_quality = "fail"
                else:
                    agg_status, agg_quality = aggregate_launch_status(item_results_map)

            else:
                # Local execution without Langfuse experiment
                semaphore = asyncio.Semaphore(spec.max_concurrency)

                async def _worker(item_row):
                    async with semaphore:
                        return await _execute_single_item(
                            self.db_manager, executor, launch_id, item_row, spec, manifest, lf=lf
                        )

                item_results = await self.gather_fn(*[_worker(row) for row in items])
                agg_status, agg_quality = aggregate_launch_status(item_results)

            # 5. Persist final launch state
            completed_at = datetime.utcnow()
            with self.db_manager.get_session() as session:
                launch_rec = session.get(ExperimentLaunchRecord, launch_id)
                assert launch_rec is not None
                launch_rec.status = agg_status
                launch_rec.quality_conclusion = agg_quality
                launch_rec.langfuse_sync_status = sync_status
                launch_rec.langfuse_sync_error = sync_error
                launch_rec.langfuse_experiment_id = str(run_id) if run_id else None
                launch_rec.langfuse_experiment_url = str(run_url) if run_url else None
                launch_rec.completed_at = completed_at
                session.commit()
                session.refresh(launch_rec)
                return launch_rec

        except Exception:
            with self.db_manager.get_session() as session:
                launch_rec = session.get(ExperimentLaunchRecord, launch_id)
                if launch_rec and launch_rec.status == "RUNNING":
                    launch_rec.status = "FAILED"
                    launch_rec.quality_conclusion = "fail"
                    launch_rec.completed_at = datetime.utcnow()
                    session.commit()
            raise
