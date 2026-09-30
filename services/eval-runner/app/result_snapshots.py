from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .aggregation import aggregate_run
from .costs import aggregate_attempt_costs
from .db_models import (
    EvaluationResultRecord,
    ExecutionAttemptRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseRunScoreTaskRecord,
    RunResultSnapshotRecord,
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_scores(scores: dict[str, Any] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in (scores or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            result[str(key)] = number
    return result


def build_result_items(
    launch: ExperimentLaunchRecord,
    items: list[ExperimentItemExecutionRecord],
    attempts: list[ExecutionAttemptRecord],
    typed_results: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    manifest_items = {
        str(item.get("id")): item for item in (launch.manifest.get("dataset", {}).get("items") or [])
    }
    attempt_by_id = {attempt.id: attempt for attempt in attempts}
    attempts_by_item: dict[str, list[ExecutionAttemptRecord]] = {}
    for attempt in attempts:
        attempts_by_item.setdefault(attempt.item_execution_id, []).append(attempt)
    result: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda record: record.dataset_item_id):
        dataset_item = manifest_items.get(item.dataset_item_id)
        attempt = attempt_by_id.get(item.final_attempt_id or "")
        cost_result = aggregate_attempt_costs(attempts_by_item.get(item.id, []))
        item_content = None if dataset_item is None else {
            "input": dataset_item.get("input"),
            "expected_output": dataset_item.get("expected_output"),
            "metadata": dataset_item.get("metadata"),
        }
        result.append({
            "dataset_item_id": item.dataset_item_id,
            "case_digest": _canonical_digest(item_content) if item_content is not None else None,
            "execution_status": item.execution_status.lower(),
            "eval_status": item.eval_status.lower(),
            "quality_conclusion": item.quality_conclusion.lower(),
            "scores": _safe_scores(item.scores),
            # Issue #82: the typed results are the authoritative record; the
            # `scores` map above is only the restricted numeric projection.
            "evaluation_results": list((typed_results or {}).get(item.id) or []),
            # Issue #83: the snapshot keeps the per-rule reasons next to the
            # verdict, so a frozen result can still be explained years later.
            "quality_evaluation": item.quality_evaluation,
            "latency_ms": attempt.latency_ms if attempt else None,
            "usage": cost_result["usage"],
            "cost": cost_result["cost"],
            "cost_evidence": cost_result["cost_evidence"],
            "final_attempt_id": item.final_attempt_id,
            "dispatch_generation": item.dispatch_generation,
            "trace_id": item.trace_id,
            "trace_url": item.langfuse_trace_url,
            "observation_id": item.observation_id,
            # Full Agent output remains in Langfuse; Argus snapshots retain only stable deep-link references.
            "output_ref": {"trace_id": item.trace_id, "observation_id": item.observation_id}
            if item.trace_id else None,
        })
    return result


def create_result_snapshot(session: Session, launch: ExperimentLaunchRecord) -> RunResultSnapshotRecord | None:
    """Idempotently freeze a terminal launch result; caller owns the surrounding DB transaction."""
    if session.get_bind().dialect.name == "postgresql":
        locked_launch = session.scalars(
            select(ExperimentLaunchRecord)
            .where(ExperimentLaunchRecord.id == launch.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if locked_launch is None:
            return None
        launch = locked_launch

    if launch.status not in {"COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}:
        return None

    items = list(session.scalars(
        select(ExperimentItemExecutionRecord)
        .where(ExperimentItemExecutionRecord.launch_id == launch.id)
        .order_by(ExperimentItemExecutionRecord.dataset_item_id)
    ).all())
    if any(item.execution_status.lower() in {"pending", "queued", "running", "retry_wait"} for item in items):
        return None

    item_execution_ids = [item.id for item in items]
    attempts = list(session.scalars(
        select(ExecutionAttemptRecord)
        .where(ExecutionAttemptRecord.item_execution_id.in_(item_execution_ids))
        .order_by(ExecutionAttemptRecord.item_execution_id, ExecutionAttemptRecord.dispatch_generation, ExecutionAttemptRecord.attempt_no)
    ).all()) if item_execution_ids else []
    # Issue #82: carry the typed results into the immutable snapshot so a
    # boolean / categorical / text measurement round-trips with its type.
    typed_by_item: dict[str, list[dict[str, Any]]] = {}
    if item_execution_ids:
        typed_rows = session.execute(
            select(
                EvaluationResultRecord.item_execution_id,
                EvaluationResultRecord.evaluator_id,
                EvaluationResultRecord.evaluator_version,
                EvaluationResultRecord.result_type,
                EvaluationResultRecord.status,
                EvaluationResultRecord.value,
                EvaluationResultRecord.normalized_value,
                EvaluationResultRecord.comment,
                EvaluationResultRecord.evidence,
                EvaluationResultRecord.duration_ms,
                EvaluationResultRecord.error_code,
                EvaluationResultRecord.error_message,
                EvaluationResultRecord.binding_id,
                EvaluationResultRecord.definition_digest,
                EvaluationResultRecord.executor_type,
                EvaluationResultRecord.manifest_schema_version,
                EvaluationResultRecord.contract_status,
            )
            .where(EvaluationResultRecord.item_execution_id.in_(item_execution_ids))
            .order_by(EvaluationResultRecord.item_execution_id, EvaluationResultRecord.evaluator_id)
        ).all()
        for row in typed_rows:
            typed_by_item.setdefault(row[0], []).append({
                "evaluator_id": row[1],
                "evaluator_version": row[2],
                "result_type": row[3],
                "status": row[4],
                "value": row[5],
                "normalized_value": row[6],
                "comment": row[7],
                "evidence": row[8],
                "duration_ms": row[9],
                "error_code": row[10],
                "error_message": row[11],
                "provenance": {
                    "binding_id": row[12],
                    "evaluator_id": row[1],
                    "evaluator_version": row[2],
                    "definition_digest": row[13],
                    "executor_type": row[14],
                    "manifest_schema_version": row[15],
                    "contract_status": row[16],
                },
            })
    result_items = build_result_items(launch, items, attempts, typed_by_item)
    source_digest = _canonical_digest(result_items)
    existing = session.scalars(select(RunResultSnapshotRecord).where(
        RunResultSnapshotRecord.launch_id == launch.id,
        RunResultSnapshotRecord.source_result_digest == source_digest,
    )).first()
    if existing:
        return existing

    evaluator_specs = launch.manifest.get("evaluators", [])
    summary = aggregate_run(result_items, evaluator_specs)
    latest_revision = session.scalar(select(func.max(RunResultSnapshotRecord.revision)).where(
        RunResultSnapshotRecord.launch_id == launch.id
    )) or 0
    snapshot = RunResultSnapshotRecord(
        id=str(uuid.uuid4()),
        launch_id=launch.id,
        agent_id=launch.agent_id,
        revision=latest_revision + 1,
        source_result_digest=source_digest,
        manifest_digest=_canonical_digest(launch.manifest),
        manifest=launch.manifest,
        summary=summary,
        items=result_items,
        created_at=datetime.utcnow(),
    )
    session.add(snapshot)
    session.flush()

    # Langfuse Run scores are emitted through a separate transactional outbox. Null metrics are omitted.
    scores = {
        "pass_rate": summary["pass_rate"],
        "evaluation_coverage": summary["evaluation_coverage"],
        "execution_error_rate": summary["execution_error_rate"],
        "critical_failure_count": summary["critical_failure_count"],
        "p95_latency_ms": summary["p95_latency_ms"],
        **{f"score_mean_{key}": value for key, value in summary["score_means"].items()},
    }
    session.add(LangfuseRunScoreTaskRecord(
        id=str(uuid.uuid4()),
        launch_id=launch.id,
        snapshot_id=snapshot.id,
        scores_payload={key: value for key, value in scores.items() if value is not None},
        status="PENDING",
        next_retry_at=datetime.utcnow(),
    ))
    return snapshot


def latest_result_snapshot(session: Session, launch_id: str) -> RunResultSnapshotRecord | None:
    return session.scalars(
        select(RunResultSnapshotRecord)
        .where(RunResultSnapshotRecord.launch_id == launch_id)
        .order_by(RunResultSnapshotRecord.revision.desc())
        .limit(1)
    ).first()
