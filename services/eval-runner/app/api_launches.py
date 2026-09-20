from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select

from .config import settings
from .db import DatabaseManager
from .db_models import (
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .evaluators import ITEM_EVALUATORS
from .executor import RemoteAgentExecutor, aggregate_launch_status
from .manifest import acquire_launch_execution
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
    summary="Query Experiment Launch by ID (?id=...) or list all",
)
def get_or_list_launches(
    id: str | None = Query(default=None, description="Optional Launch ID. If omitted, returns all launches."),
    services=Depends(get_services),
) -> Any:
    _, _, launch_svc = services
    if id:
        launch = launch_svc.get_launch(id)
        if not launch:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Launch '{id}' not found")
        return ExperimentLaunchResponse.model_validate(launch)
    else:
        launches = launch_svc.list_launches()
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
        return [ExperimentItemExecutionResponse.model_validate(i) for i in items]


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

    executor.set_attempt_hooks(on_attempt_start, on_attempt_end)

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
        call_res = await executor.invoke(mapped_payload, headers)
        agent_output = call_res.body
    except Exception as exc:
        execution_status = "failed"
        execution_error = str(exc)
        eval_status = "skipped"
        quality_conclusion = "fail"

    if execution_status == "succeeded" and agent_output is not None:
        try:
            # Evaluate using registered evaluators
            for evaluator in ITEM_EVALUATORS:
                ev_res = evaluator(output=agent_output, expected_output=expected_output)
                val = float(getattr(ev_res, "value", 0.0))
                name = getattr(ev_res, "name", evaluator.__name__)
                scores_dict[name] = val

            overall = scores_dict.get("overall_pass", 0.0)
            quality_conclusion = "pass" if overall == 1.0 else "fail"
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
    # 1. Acquire atomic execution lock
    acquired = acquire_launch_execution(db_mgr, launch_id)
    if not acquired:
        with db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            if not launch:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Launch '{launch_id}' not found")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Launch '{launch_id}' is already in status '{launch.status}', duplicate run rejected.",
            )

    try:
        # 2. Load launch and frozen manifest
        with db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            assert launch is not None
            manifest = launch.manifest
            agent_spec_dict = manifest["agent"]
            exec_policy = manifest["execution_policy"]

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
        )

        executor = RemoteAgentExecutor(spec)

        # 3. Read dataset seed items
        dataset_file = Path(settings.dataset_seed_path)
        if not dataset_file.exists():
            # Fallback to local data dir if relative
            dataset_file = Path(__file__).resolve().parents[3] / "data" / "dataset.json"
        seed = json.loads(dataset_file.read_text(encoding="utf-8"))
        items = seed.get("items", [])

        # 4. Concurrently run items with semaphore
        semaphore = asyncio.Semaphore(spec.max_concurrency)

        async def _worker(item_row):
            async with semaphore:
                return await _execute_single_item(db_mgr, executor, launch_id, item_row, spec)

        item_results = await asyncio.gather(*[_worker(row) for row in items])

        # 5. Aggregate launch status
        agg_status, agg_quality = aggregate_launch_status(item_results)

        # 6. Update launch record in DB
        completed_at = datetime.utcnow()
        with db_mgr.get_session() as session:
            launch_rec = session.get(ExperimentLaunchRecord, launch_id)
            assert launch_rec is not None
            launch_rec.status = agg_status
            launch_rec.quality_conclusion = agg_quality
            launch_rec.completed_at = completed_at
            session.commit()
            session.refresh(launch_rec)
            return launch_rec
    except Exception:
        with db_mgr.get_session() as session:
            launch_rec = session.get(ExperimentLaunchRecord, launch_id)
            if launch_rec and launch_rec.status == "RUNNING":
                launch_rec.status = "FAILED"
                launch_rec.quality_conclusion = "fail"
                launch_rec.completed_at = datetime.utcnow()
                session.commit()
        raise


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
