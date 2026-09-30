"""Persistence for typed EvaluationResults (Argus Issue #82).

The typed rows are the authoritative record of a measurement. ``scores`` on the
item execution stays a restricted numeric projection only, so a re-evaluation
(or a partial failure) can never erase results that already succeeded.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db_models import EvaluationResultRecord


def persist_typed_results(
    session: Session,
    *,
    item_execution_id: str,
    launch_id: str,
    results: Iterable[Any],
) -> list[EvaluationResultRecord]:
    """Replace the typed results of one item execution with ``results``.

    Idempotent: re-evaluating an item rewrites exactly its own rows and never
    touches another item's evidence. An empty ``results`` iterable writes
    nothing and leaves any existing rows untouched, so a "no evaluators" pass
    cannot blank out valid measurements.
    """
    materialized = list(results)
    if not materialized:
        return []

    session.execute(
        delete(EvaluationResultRecord).where(
            EvaluationResultRecord.item_execution_id == item_execution_id
        )
    )

    records: list[EvaluationResultRecord] = []
    for result in materialized:
        provenance = getattr(result, "provenance", None)
        record = EvaluationResultRecord(
            id=f"er_{uuid.uuid4().hex[:24]}",
            item_execution_id=item_execution_id,
            launch_id=launch_id,
            evaluator_id=str(result.evaluator_id),
            evaluator_version=getattr(provenance, "evaluator_version", None),
            result_type=str(result.result_type),
            status=str(result.status),
            value=_jsonable(getattr(result, "value", None)),
            normalized_value=(
                float(result.normalized_value)
                if getattr(result, "normalized_value", None) is not None
                else None
            ),
            comment=getattr(result, "comment", None),
            evidence=getattr(result, "evidence", None),
            duration_ms=(
                float(result.duration_ms)
                if getattr(result, "duration_ms", None) is not None
                else None
            ),
            error_code=getattr(result, "error_code", None),
            error_message=getattr(result, "error_message", None),
            binding_id=getattr(provenance, "binding_id", None),
            definition_digest=getattr(provenance, "definition_digest", None),
            executor_type=getattr(provenance, "executor_type", None),
            manifest_schema_version=getattr(provenance, "manifest_schema_version", None),
            contract_status=getattr(provenance, "contract_status", None),
        )
        session.add(record)
        records.append(record)
    return records


def _jsonable(value: Any) -> Any:
    """Keep booleans, numbers, strings and text; drop non-finite numbers."""
    import math

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (int, str)):
        return value
    return str(value)


def load_typed_results(
    session: Session, item_execution_id: str
) -> list[EvaluationResultRecord]:
    return list(
        session.scalars(
            select(EvaluationResultRecord)
            .where(EvaluationResultRecord.item_execution_id == item_execution_id)
            .order_by(EvaluationResultRecord.evaluator_id)
        )
    )


def result_to_payload(record: EvaluationResultRecord) -> dict[str, Any]:
    """Serialise one persisted typed result for the API (Issue #82)."""
    return {
        "evaluator_id": record.evaluator_id,
        "evaluator_version": record.evaluator_version,
        "result_type": record.result_type,
        "status": record.status,
        "value": record.value,
        "normalized_value": record.normalized_value,
        "comment": record.comment,
        "evidence": record.evidence,
        "duration_ms": record.duration_ms,
        "error_code": record.error_code,
        "error_message": record.error_message,
        "provenance": {
            "binding_id": record.binding_id,
            "evaluator_id": record.evaluator_id,
            "evaluator_version": record.evaluator_version,
            "definition_digest": record.definition_digest,
            "executor_type": record.executor_type,
            "manifest_schema_version": record.manifest_schema_version,
            "contract_status": record.contract_status,
        },
    }
