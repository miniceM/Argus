from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select

from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .evaluators import default_evaluator_registry, evaluate_item_quality
from .executor import RemoteAgentExecutor
from .models import (
    ExecutionAttemptResponse,
    ExperimentItemExecutionResponse,
    ExperimentLaunchCreateRequest,
    ExperimentLaunchResponse,
    ExperimentLaunchRunRequest,
)
from .registry import AgentRegistry, AgentVersionSpec, map_request

router = APIRouter(prefix="/api/v1", tags=["Experiment Launches"])


def get_services():
    from .main import db_manager, launch_service, registry
    return db_manager, registry, launch_service


@router.post(
    "/experiment-launches",
    response_model=ExperimentLaunchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an Experiment Launch with frozen 4D Manifest & Idempotency",
)
def create_experiment_launch(
    payload: ExperimentLaunchCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    services=Depends(get_services),
) -> ExperimentLaunchResponse:
    _, _, launch_svc = services
    effective_key = idempotency_key or payload.idempotency_key
    try:
        launch = launch_svc.create_launch(
            agent_id=payload.agent_id,
            agent_version=payload.agent_version,
            dataset_name=payload.dataset_name,
            dataset_version=payload.dataset_version,
            name=payload.name,
            idempotency_key=effective_key,
            max_concurrency=payload.max_concurrency,
            evaluator_ids=payload.evaluator_ids,
        )
        return ExperimentLaunchResponse.model_validate(launch)
    except ValueError as exc:
        msg = str(exc)
        if "conflict" in msg.lower():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg) from exc
        elif "not found" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg) from exc
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc


@router.get(
    "/experiment-launches",
    response_model=ExperimentLaunchResponse | list[ExperimentLaunchResponse],
    summary="Query Experiment Launch by ID (?id=...) or list all with optional filters",
)
def get_or_list_launches(
    id: str | None = Query(default=None, description="Optional Launch ID. If omitted, returns all launches."),
    agent_id: str | None = Query(default=None, description="Optional filter by Agent ID"),
    status: str | None = Query(default=None, description="Optional filter by execution status"),
    quality_conclusion: str | None = Query(default=None, description="Optional filter by quality conclusion"),
    limit: int | None = Query(default=None, ge=1, le=500, description="Optional limit"),
    offset: int = Query(default=0, ge=0, description="Optional offset"),
    services=Depends(get_services),
) -> Any:
    _, _, launch_svc = services
    if id:
        launch = launch_svc.get_launch(id)
        if not launch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Launch '{id}' not found")
        return ExperimentLaunchResponse.model_validate(launch)
    else:
        launches = launch_svc.list_launches(
            agent_id=agent_id,
            status=status,
            quality_conclusion=quality_conclusion,
            limit=limit,
            offset=offset,
        )
        return [ExperimentLaunchResponse.model_validate(item) for item in launches]


@router.get(
    "/experiment-launch-items",
    response_model=list[ExperimentItemExecutionResponse],
    summary="List item executions for a Launch (?launch_id=...)",
)
def list_launch_items(
    launch_id: str = Query(..., description="Launch ID (required)"),
    services=Depends(get_services),
) -> list[ExperimentItemExecutionResponse]:
    db_mgr, _, _ = services
    with db_mgr.get_session() as session:
        stmt = (
            select(ExperimentItemExecutionRecord)
            .where(ExperimentItemExecutionRecord.launch_id == launch_id)
            .order_by(ExperimentItemExecutionRecord.started_at)
        )
        items = session.scalars(stmt).all()
        if not items:
            return []

        item_ids = [i.id for i in items]
        final_attempt_ids = [i.final_attempt_id for i in items if i.final_attempt_id]

        # Aggregate attempt count per item in single query
        counts_res = session.execute(
            select(
                ExecutionAttemptRecord.item_execution_id,
                func.count(ExecutionAttemptRecord.id),
            )
            .where(ExecutionAttemptRecord.item_execution_id.in_(item_ids))
            .group_by(ExecutionAttemptRecord.item_execution_id)
        ).all()
        counts_map = {row[0]: row[1] for row in counts_res}

        final_attempts_map = {}
        if final_attempt_ids:
            final_attempts = session.scalars(
                select(ExecutionAttemptRecord).where(ExecutionAttemptRecord.id.in_(final_attempt_ids))
            ).all()
            for fa in final_attempts:
                final_attempts_map[fa.id] = fa

        responses = []
        for i in items:
            res = ExperimentItemExecutionResponse.model_validate(i)
            res.attempt_count = counts_map.get(i.id, 0)
            fa = final_attempts_map.get(i.final_attempt_id) if i.final_attempt_id else None
            res.final_attempt_http_status = fa.http_status if fa else None
            res.final_attempt_latency_ms = fa.latency_ms if fa else None
            responses.append(res)
        return responses


@router.get(
    "/execution-attempts",
    response_model=list[ExecutionAttemptResponse],
    summary="List execution attempts for an item (?item_execution_id=...)",
)
def list_execution_attempts(
    item_execution_id: str = Query(..., description="Item Execution ID (required)"),
    services=Depends(get_services),
) -> list[ExecutionAttemptResponse]:
    db_mgr, _, _ = services
    with db_mgr.get_session() as session:
        stmt = (
            select(ExecutionAttemptRecord)
            .where(ExecutionAttemptRecord.item_execution_id == item_execution_id)
            .order_by(ExecutionAttemptRecord.attempt_no)
        )
        attempts = session.scalars(stmt).all()
        return [ExecutionAttemptResponse.model_validate(a) for a in attempts]


async def _execute_single_item(
    db_mgr: DatabaseManager,
    executor: RemoteAgentExecutor,
    launch_id: str,
    item_row: dict[str, Any],
    spec: AgentVersionSpec,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    item_id = str(item_row["id"])
    dataset_input = item_row.get("input", {})
    expected_output = item_row.get("expected_output", {})

    item_exec_id = str(uuid.uuid4())
    started_at = datetime.utcnow()

    # Pre-create ExperimentItemExecution record
    with db_mgr.get_session() as session:
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

    # Set up Attempt Hooks
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

    # Inject W3C Trace context if tracing is enabled
    try:
        from opentelemetry.propagate import inject
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
                # Enforce attempt ownership verification
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



async def run_launch_synchronously(
    db_mgr: DatabaseManager,
    registry: AgentRegistry,
    launch_id: str,
) -> ExperimentLaunchRecord:
    from .execution import LaunchExecutionService

    svc = LaunchExecutionService(db_mgr, registry, gather_fn=asyncio.gather)
    return await svc.execute_launch(launch_id)



@router.post(
    "/experiment-launches/run",
    response_model=ExperimentLaunchResponse,
    summary="Synchronously run an existing Experiment Launch (Atomic execution lock)",
)
async def run_experiment_launch(
    payload: ExperimentLaunchRunRequest,
    services=Depends(get_services),
) -> ExperimentLaunchResponse:
    db_mgr, reg, _ = services
    launch_rec = await run_launch_synchronously(db_mgr, reg, payload.launch_id)
    return ExperimentLaunchResponse.model_validate(launch_rec)
