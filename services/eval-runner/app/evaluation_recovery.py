"""Evaluation-only recovery (Argus Issue #84).

The Agent already answered. Only the evaluation failed. This module re-judges
the *stored* output under the *same* frozen Manifest, touching neither the Agent
nor the execution attempt:

* :func:`claim_evaluation` takes a fenced, generation-scoped evaluation lease,
  so a duplicate click or a competing worker can never start a second effective
  evaluation of the same generation.
* :func:`recover_evaluation` loads and verifies the checkpoint, re-runs only the
  failed / missing Bindings, and merges those with the results that already
  succeeded — preserved verbatim, with their provenance.
* :func:`finalize_evaluation` writes the verdict under a compare-and-set on
  ``(evaluation_generation, evaluation_lease_token)``. A late result from an old
  generation, a cancelled attempt or a lost lease therefore cannot overwrite the
  current results, and an already-frozen Snapshot is never touched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from .db import DatabaseManager
from .db_models import (
    EvaluationAttemptRecord,
    EvaluationResultRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from .evaluation_result_store import persist_typed_results, result_from_record
from .evaluator_binding import (
    EvaluatorBindingError,
    evaluate_frozen_item,
    resolve_execution_plan,
    summarize_typed_results,
)
from .execution_checkpoint import (
    CheckpointUnavailableError,
    load_recoverable_checkpoint,
)

# Evaluation-recovery lifecycle (independent of execution_status / eval_status).
EVALUATION_IDLE = "none"
EVALUATION_RUNNING = "evaluating"
EVALUATION_RECOVERED = "recovered"
EVALUATION_FAILED = "failed"

# Queue work types: the existing queue now distinguishes invocation from
# evaluation work instead of introducing a second scheduler.
WORK_TYPE_INVOCATION = "INVOCATION"
WORK_TYPE_EVALUATION = "EVALUATION"

# Eval statuses that mean "the evaluation needs to be (re)run".
RECOVERABLE_EVAL_STATUSES = ("failed", "partial", "skipped", "no_result")


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class RecoveryOutcome(dict):
    """Result of one evaluation-recovery run (plain dict for easy testing)."""


def claim_evaluation(
    db_mgr: DatabaseManager,
    *,
    item_id: str,
    evaluation_generation: int,
    worker_id: str,
    lease_seconds: int = 60,
) -> dict[str, Any] | None:
    """Take the fenced evaluation lease for one generation, or return ``None``.

    The compare-and-set only succeeds when the item is still ``evaluating`` at
    this exact generation and no live lease is held. A redelivered message, a
    second worker or a lost lease therefore yields at most one effective run.
    """
    now = _now()
    expires_at = now + timedelta(seconds=lease_seconds)
    token = str(uuid.uuid4())

    with db_mgr.get_session() as session:
        stmt = (
            update(ExperimentItemExecutionRecord)
            .where(
                ExperimentItemExecutionRecord.id == item_id,
                ExperimentItemExecutionRecord.evaluation_generation == evaluation_generation,
                ExperimentItemExecutionRecord.evaluation_status == EVALUATION_RUNNING,
                (ExperimentItemExecutionRecord.evaluation_lease_token.is_(None))
                | (ExperimentItemExecutionRecord.evaluation_lease_expires_at.is_(None))
                | (ExperimentItemExecutionRecord.evaluation_lease_expires_at <= now),
            )
            .values(
                evaluation_lease_owner=worker_id,
                evaluation_lease_token=token,
                evaluation_lease_expires_at=expires_at,
                updated_at=now,
            )
        )
        res = session.execute(stmt)
        if res.rowcount != 1:
            session.rollback()
            return None
        item = session.get(ExperimentItemExecutionRecord, item_id)
        launch_id = item.launch_id if item else None
        session.commit()
        return {
            "item_id": item_id,
            "launch_id": launch_id,
            "lease_token": token,
            "evaluation_generation": evaluation_generation,
            "lease_expires_at": expires_at,
        }


def renew_evaluation_lease(
    db_mgr: DatabaseManager,
    *,
    item_id: str,
    evaluation_generation: int,
    lease_token: str,
    extension_seconds: int = 60,
) -> bool:
    """Extend a live evaluation lease; ``False`` once it is lost."""
    now = _now()
    with db_mgr.get_session() as session:
        stmt = (
            update(ExperimentItemExecutionRecord)
            .where(
                ExperimentItemExecutionRecord.id == item_id,
                ExperimentItemExecutionRecord.evaluation_generation == evaluation_generation,
                ExperimentItemExecutionRecord.evaluation_lease_token == lease_token,
                ExperimentItemExecutionRecord.evaluation_status == EVALUATION_RUNNING,
                ExperimentItemExecutionRecord.evaluation_lease_expires_at > now,
            )
            .values(evaluation_lease_expires_at=now + timedelta(seconds=extension_seconds), updated_at=now)
        )
        res = session.execute(stmt)
        session.commit()
        return res.rowcount == 1


def _launch_cancelled(session, launch_id: str | None) -> bool:
    if not launch_id:
        return False
    launch = session.get(ExperimentLaunchRecord, launch_id)
    if not launch:
        return False
    return bool(launch.cancel_requested_at or launch.status in ("CANCELLING", "CANCELLED"))


def _close_attempt(
    session,
    *,
    item_id: str,
    evaluation_generation: int,
    status: str,
    error_type: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
    launch_id: str | None = None,
    target_bindings: list[str] | None = None,
    reused_output_digest: str = "",
    worker_id: str | None = None,
) -> None:
    """Close an evaluation attempt, inserting a minimal row if none exists.

    The upsert keeps the audit trail honest even when a result is discarded
    before (or without) the normal start-of-attempt insert — e.g. a late
    generation whose start was never recorded under this process.
    """
    now = now or _now()
    res = session.execute(
        update(EvaluationAttemptRecord)
        .where(
            EvaluationAttemptRecord.item_execution_id == item_id,
            EvaluationAttemptRecord.evaluation_generation == evaluation_generation,
        )
        .values(status=status, error_type=error_type, error_message=error_message, completed_at=now)
    )
    if res.rowcount == 0 and launch_id:
        session.add(
            EvaluationAttemptRecord(
                id=f"ea_{uuid.uuid4().hex[:24]}",
                item_execution_id=item_id,
                launch_id=launch_id,
                evaluation_generation=evaluation_generation,
                status=status,
                target_bindings=list(target_bindings or []),
                reused_output_digest=reused_output_digest or "",
                worker_id=worker_id,
                error_type=error_type,
                error_message=error_message,
                started_at=now,
                completed_at=now,
            )
        )


def _record_attempt_started(
    db_mgr: DatabaseManager,
    *,
    item_id: str,
    launch_id: str,
    evaluation_generation: int,
    target_bindings: list[str],
    reused_output_digest: str,
    worker_id: str,
) -> None:
    """Insert the running evaluation attempt so recovery is auditable."""
    now = _now()
    with db_mgr.get_session() as session:
        session.add(
            EvaluationAttemptRecord(
                id=f"ea_{uuid.uuid4().hex[:24]}",
                item_execution_id=item_id,
                launch_id=launch_id,
                evaluation_generation=evaluation_generation,
                status="running",
                target_bindings=list(target_bindings),
                reused_output_digest=reused_output_digest or "",
                worker_id=worker_id,
                lease_token=None,
                started_at=now,
            )
        )
        session.commit()


def _fail_without_results(
    db_mgr: DatabaseManager,
    claim: dict[str, Any],
    *,
    error_code: str,
    error_message: str,
    now: datetime | None = None,
) -> RecoveryOutcome:
    """Record a blocked / failed evaluation that produced no new results.

    The item keeps its previous verdict (typically UNKNOWN) — recovery never
    fabricates a conclusion, and it never falls back to calling the Agent. If
    the attempt is already superseded the item is left untouched and only the
    attempt row is marked ``discarded``.
    """
    now = now or _now()
    item_id = claim["item_id"]
    generation = claim["evaluation_generation"]
    token = claim["lease_token"]

    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        if not item:
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="ITEM_MISSING")
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
        ):
            _close_attempt(
                session,
                item_id=item_id,
                evaluation_generation=generation,
                status="discarded",
                error_type=error_code,
                error_message=error_message,
                now=now,
            )
            session.commit()
            return RecoveryOutcome(claimed=True, finalized=False, reason="SUPERSEDED_OR_LEASE_LOST")

        item.evaluation_status = EVALUATION_FAILED
        item.evaluation_error = f"{error_code}: {error_message}"
        item.evaluation_lease_owner = None
        item.evaluation_lease_token = None
        item.evaluation_lease_expires_at = None
        item.evaluation_completed_at = now
        item.updated_at = now
        _close_attempt(
            session,
            item_id=item_id,
            evaluation_generation=generation,
            status="failed",
            error_type=error_code,
            error_message=error_message,
            now=now,
        )
        session.commit()
        return RecoveryOutcome(
            claimed=True,
            finalized=True,
            reason=error_code,
            error=item.evaluation_error,
        )


def recover_evaluation(
    db_mgr: DatabaseManager,
    *,
    item_id: str,
    evaluation_generation: int,
    worker_id: str,
    lease_seconds: int = 60,
) -> RecoveryOutcome:
    """Re-judge one item's stored output. Never calls the Agent.

    Returns a :class:`RecoveryOutcome` describing whether the attempt was
    claimed, whether it was finalized under the fence, and (on success) the new
    verdict.
    """
    claim = claim_evaluation(
        db_mgr,
        item_id=item_id,
        evaluation_generation=evaluation_generation,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if claim is None:
        return RecoveryOutcome(claimed=False, finalized=False, reason="NOT_CLAIMABLE")
    launch_id = claim.get("launch_id")
    now = _now()

    with db_mgr.get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, item_id)
        if item is None:
            session.rollback()
            return RecoveryOutcome(claimed=True, finalized=False, reason="ITEM_MISSING")
        manifest = (session.get(ExperimentLaunchRecord, launch_id).manifest if launch_id else {}) or {}
        # Prefer the checkpoint's frozen input/expected output for the retry.
        try:
            checkpoint = load_recoverable_checkpoint(
                session,
                item_execution_id=item_id,
                dispatch_generation=item.dispatch_generation,
                manifest=manifest,
                now=now,
            )
        except CheckpointUnavailableError as exc:
            session.rollback()
            return _fail_without_results(
                db_mgr, claim, error_code=exc.code, error_message=exc.message
            )
        output = checkpoint.agent_output
        expected_output = checkpoint.expected_output
        output_digest = checkpoint.output_digest
        # Preserve already-successful results verbatim.
        existing_rows = list(
            session.scalars(
                select(EvaluationResultRecord).where(
                    EvaluationResultRecord.item_execution_id == item_id
                )
            )
        )
        existing_results = [result_from_record(row) for row in existing_rows]
        # Re-check cancellation under the current state before doing work.
        if _launch_cancelled(session, launch_id):
            session.rollback()
            return _fail_without_results(
                db_mgr,
                claim,
                error_code="EVALUATION_CANCELLED",
                error_message="评测任务已取消，未写入重评结果。",
            )

    # Resolve the frozen plan and decide which bindings still need a verdict.
    try:
        full_plan = [
            resolved
            for resolved in resolve_execution_plan(manifest)
            if resolved.binding.scope == "item"
        ]
    except EvaluatorBindingError as exc:
        return _fail_without_results(
            db_mgr, claim, error_code=exc.code, error_message=str(exc)
        )

    existing_by_key: dict[str, Any] = {}
    for result in existing_results:
        key = getattr(getattr(result, "provenance", None), "binding_id", None) or result.evaluator_id
        existing_by_key[str(key)] = result

    needed_ids: set[str] = set()
    for resolved in full_plan:
        binding = resolved.binding
        key = binding.binding_id or binding.evaluator_id
        current = existing_by_key.get(str(key))
        if current is None or getattr(current, "status", "") != "succeeded":
            needed_ids.add(binding.binding_id)

    # Record the running attempt (audit) before doing the work, so even a crash
    # leaves a trace and the target bindings are explicit.
    _record_attempt_started(
        db_mgr,
        item_id=item_id,
        launch_id=launch_id,
        evaluation_generation=evaluation_generation,
        target_bindings=sorted(needed_ids),
        reused_output_digest=output_digest,
        worker_id=worker_id,
    )

    # Re-judge ONLY the failed / missing bindings.
    partial = evaluate_frozen_item(
        manifest,
        output=output,
        expected_output=expected_output,
        only_binding_ids=needed_ids or None,
    )
    new_by_key: dict[str, Any] = {}
    for result in partial.typed_results:
        key = getattr(getattr(result, "provenance", None), "binding_id", None) or result.evaluator_id
        new_by_key[str(key)] = result

    # Merge: a re-judged result replaces the failed one; a still-successful
    # result is kept exactly as recorded. Every frozen binding ends up with
    # exactly one result so the truth table sees the full set.
    merged: list[Any] = []
    for resolved in full_plan:
        binding = resolved.binding
        key = str(binding.binding_id or binding.evaluator_id)
        if key in new_by_key:
            merged.append(new_by_key[key])
        elif key in existing_by_key:
            merged.append(existing_by_key[key])
        else:  # defensive: a binding with neither old nor new evidence
            from .evaluator_results import failed_result

            merged.append(
                failed_result(
                    binding,
                    error_code="EVALUATION_FAILED",
                    error_message="重评未产生该指标的测量结果。",
                )
            )

    # One shared truth table decides the new verdict (Issue #83 rules).
    summary = summarize_typed_results(full_plan, merged, manifest=manifest)

    finalized = finalize_evaluation(
        db_mgr,
        claim=claim,
        summary=summary,
        merged_results=merged,
        output_digest=output_digest,
        reused_bindings=sorted(
            str(r.binding.binding_id or r.binding.evaluator_id)
            for r in full_plan
            if str(r.binding.binding_id or r.binding.evaluator_id) not in needed_ids
        ),
    )
    return RecoveryOutcome(
        claimed=True,
        finalized=finalized,
        reason=None if finalized else "SUPERSEDED_OR_LEASE_LOST",
        eval_status=summary.eval_status,
        quality_conclusion=summary.quality_conclusion,
        launch_id=launch_id,
    )


def finalize_evaluation(
    db_mgr: DatabaseManager,
    *,
    claim: dict[str, Any],
    summary: Any,
    merged_results: list[Any],
    output_digest: str,
    reused_bindings: list[str],
) -> bool:
    """Commit a recovered verdict under the evaluation-generation / lease fence.

    Returns ``False`` (and marks the attempt ``discarded``) when this result is
    late: a newer generation already took over, the lease was lost, or the task
    was cancelled. In that case nothing about the current results changes.
    """
    is_pg = db_mgr.engine.dialect.name == "postgresql"
    item_id = claim["item_id"]
    generation = claim["evaluation_generation"]
    token = claim["lease_token"]
    launch_id = claim.get("launch_id")
    now = _now()

    with db_mgr.get_session() as session:
        # Follow the global Launch -> Item lock ordering: read cancellation
        # state before taking the item lock.
        if _launch_cancelled(session, launch_id):
            _close_attempt(
                session,
                item_id=item_id,
                evaluation_generation=generation,
                status="discarded",
                error_type="EVALUATION_CANCELLED",
                error_message="评测任务已取消，迟到结果未写入。",
                now=now,
                launch_id=launch_id,
                target_bindings=reused_bindings,
                reused_output_digest=output_digest,
            )
            session.commit()
            return False

        lock_stmt = select(ExperimentItemExecutionRecord).where(
            ExperimentItemExecutionRecord.id == item_id
        )
        if is_pg:
            lock_stmt = lock_stmt.with_for_update()
        item = session.scalars(lock_stmt).first()
        if not item:
            session.rollback()
            return False

        lease_exp = _as_utc(item.evaluation_lease_expires_at)
        if (
            item.evaluation_generation != generation
            or item.evaluation_lease_token != token
            or item.evaluation_status != EVALUATION_RUNNING
            or lease_exp is None
            or lease_exp <= now
        ):
            # Late / superseded / lost lease: drop this result on the floor and
            # leave the current results untouched.
            _close_attempt(
                session,
                item_id=item_id,
                evaluation_generation=generation,
                status="discarded",
                error_type="SUPERSEDED_OR_LEASE_LOST",
                error_message="旧评测结果已过期，未覆盖当前结果。",
                now=now,
                launch_id=launch_id,
                target_bindings=reused_bindings,
                reused_output_digest=output_digest,
            )
            session.commit()
            return False

        item.eval_status = summary.eval_status
        item.quality_conclusion = summary.quality_conclusion
        item.scores = summary.scores
        decision = getattr(summary, "quality_decision", None)
        if decision is not None:
            item.quality_evaluation = decision.payload()
        item.eval_error = summary.eval_error
        item.evaluation_status = (
            EVALUATION_RECOVERED if summary.eval_status in ("succeeded", "partial") else EVALUATION_FAILED
        )
        item.evaluation_error = summary.eval_error
        item.evaluation_reused_output_digest = output_digest
        item.evaluation_lease_owner = None
        item.evaluation_lease_token = None
        item.evaluation_lease_expires_at = None
        item.evaluation_completed_at = now
        item.updated_at = now

        if launch_id and merged_results:
            persist_typed_results(
                session,
                item_execution_id=item_id,
                launch_id=launch_id,
                results=merged_results,
            )

        _close_attempt(
            session,
            item_id=item_id,
            evaluation_generation=generation,
            status="succeeded" if summary.eval_status in ("succeeded", "partial") else "failed",
            error_type=None if summary.eval_error is None else "EVALUATION_FAILED",
            error_message=summary.eval_error,
            now=now,
        )
        session.commit()
        return True
