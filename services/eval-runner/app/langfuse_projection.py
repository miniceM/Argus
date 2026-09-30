"""One-way typed projection of a frozen Argus result into Langfuse.

Argus owns the release result. Langfuse is an analysis projection: it is
written from the frozen Snapshot and never read back to change a local quality
or comparison conclusion.

The single rule that shapes this module: *a type Langfuse cannot represent is
reported as NOT_APPLICABLE with a reason*. It is never coerced to 0, never
silently dropped, and never presented as a synced score.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Langfuse scores are numeric. These typed results have an exact numeric
# representation; everything else must be explained rather than forced.
_NUMERIC_RESULT_TYPES = {"numeric", "number"}

NOT_APPLICABLE_TEXT_REASON = "TEXT_RESULT_NOT_SUPPORTED_AS_NUMERIC_SCORE"
NOT_APPLICABLE_UNORDERED_REASON = "CATEGORY_HAS_NO_FROZEN_NUMERIC_MAPPING"
NOT_APPLICABLE_FAILED_REASON = "EVALUATION_RESULT_NOT_SUCCEEDED"
NOT_APPLICABLE_MISSING_VALUE_REASON = "RESULT_VALUE_MISSING"

PROJECTION_SOURCE = "ARGUS_FROZEN_SNAPSHOT"


@dataclass(frozen=True)
class ProjectedScore:
    """One Langfuse score derived from one frozen typed result."""

    evaluator_id: str
    score_id: str
    applicable: bool
    value: float | None = None
    comment: str | None = None
    evidence: dict[str, Any] | None = None
    not_applicable_reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "evaluator_id": self.evaluator_id,
            "score_id": self.score_id,
            "applicable": self.applicable,
            "value": self.value,
            "comment": self.comment,
            "evidence": self.evidence,
            "not_applicable_reason": self.not_applicable_reason,
        }


def stable_score_id(*, item_id: str, generation: int, evaluator_id: str) -> str:
    """The idempotency key of one logical score.

    It covers the logical result identity (item + evaluator) *and* the
    evaluation revision (dispatch generation). Re-delivering the same revision
    recomputes the same id, so Langfuse upserts instead of duplicating; a
    re-evaluation produces a new id, so the old revision's projection is kept.
    """
    return f"score:{item_id}:gen{generation}:{evaluator_id}"


def _evidence(
    result: Mapping[str, Any],
    *,
    snapshot_id: str | None,
    revision: int | None,
    policy_digest: str | None,
    definition_digest: str | None,
    attempt: int | None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "source": PROJECTION_SOURCE,
        "original_value": result.get("value"),
        "result_type": result.get("result_type"),
        "result_status": result.get("status"),
        "snapshot_id": snapshot_id,
        "revision": revision,
        "policy_digest": policy_digest,
        "definition_digest": definition_digest,
        "attempt": attempt,
    }
    provenance = result.get("provenance")
    if isinstance(provenance, Mapping):
        evidence["binding_id"] = provenance.get("binding_id")
        evidence["contract_status"] = provenance.get("contract_status")
        evidence["evaluator_version"] = provenance.get("evaluator_version")
    return evidence


def project_typed_result(
    result: Mapping[str, Any],
    *,
    item_id: str,
    generation: int,
    snapshot_id: str | None = None,
    revision: int | None = None,
    policy_digest: str | None = None,
    definition_digest: str | None = None,
    attempt: int | None = None,
) -> ProjectedScore:
    """Map one typed result onto a Langfuse score, or explain why it cannot."""
    evaluator_id = str(result.get("evaluator_id") or "unknown")
    score_id = stable_score_id(item_id=item_id, generation=generation, evaluator_id=evaluator_id)
    evidence = _evidence(
        result,
        snapshot_id=snapshot_id,
        revision=revision,
        policy_digest=policy_digest,
        definition_digest=definition_digest,
        attempt=attempt,
    )

    def inapplicable(reason: str, comment: str) -> ProjectedScore:
        return ProjectedScore(
            evaluator_id=evaluator_id,
            score_id=score_id,
            applicable=False,
            value=None,
            comment=comment,
            evidence=evidence,
            not_applicable_reason=reason,
        )

    status = str(result.get("status") or "").lower()
    if status != "succeeded":
        return inapplicable(
            NOT_APPLICABLE_FAILED_REASON,
            f"评测结果状态为 {status or 'unknown'}，未投影为 Score。原始证据保留在 Argus。",
        )

    result_type = str(result.get("result_type") or "").lower()

    # A frozen normalization rule is the only sanctioned way to order a value.
    if result.get("normalized_value") is not None:
        try:
            value = float(result["normalized_value"])
        except (TypeError, ValueError):
            return inapplicable(
                NOT_APPLICABLE_UNORDERED_REASON,
                "冻结归一化映射产生了非数值，未投影为 Score。",
            )
        return ProjectedScore(
            evaluator_id=evaluator_id,
            score_id=score_id,
            applicable=True,
            value=value,
            comment=f"由冻结归一化映射投影（原始值 {result.get('value')!r}）。",
            evidence=evidence,
        )

    if result_type in _NUMERIC_RESULT_TYPES:
        raw = result.get("value")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return inapplicable(
                NOT_APPLICABLE_MISSING_VALUE_REASON,
                "数值结果缺少可用数值，未投影为 Score。",
            )
        return ProjectedScore(
            evaluator_id=evaluator_id,
            score_id=score_id,
            applicable=True,
            value=float(raw),
            comment=None,
            evidence=evidence,
        )

    if result_type == "boolean":
        raw = result.get("value")
        if not isinstance(raw, bool):
            return inapplicable(
                NOT_APPLICABLE_MISSING_VALUE_REASON,
                "布尔结果缺少可用取值，未投影为 Score。",
            )
        return ProjectedScore(
            evaluator_id=evaluator_id,
            score_id=score_id,
            applicable=True,
            value=1.0 if raw else 0.0,
            comment=f"布尔结果投影为 1/0（原始值 {str(raw).lower()}），原始证据见 metadata。",
            evidence=evidence,
        )

    if result_type == "categorical":
        return inapplicable(
            NOT_APPLICABLE_UNORDERED_REASON,
            f"分类结果 {result.get('value')!r} 没有冻结的有序映射，未投影为数值 Score。",
        )

    if result_type == "text":
        return inapplicable(
            NOT_APPLICABLE_TEXT_REASON,
            "文本结果无法作为数值 Score 投影，已作为证据保留在 Argus。",
        )

    return inapplicable(
        NOT_APPLICABLE_MISSING_VALUE_REASON,
        f"未知结果类型 {result_type or 'unknown'}，未投影为 Score。",
    )


_STATUS_MAP = {
    "SYNCED": "SYNCED",
    "PENDING": "PENDING",
    "PROCESSING": "PENDING",
    "FAILED": "FAILED",
    "SKIPPED": "NOT_APPLICABLE",
    "NOT_APPLICABLE": "NOT_APPLICABLE",
}


def projection_status_for(
    status: str | None,
    *,
    attempts: int | None = None,
    max_attempts: int | None = None,
    has_applicable_projection: bool | None = None,
) -> str:
    """The user-facing state of one sync scope.

    ``RETRY_EXHAUSTED`` is deliberately distinct from ``FAILED``: a scope that
    ran out of attempts will never progress on its own, while a ``FAILED``
    scope may still be retried.
    """
    normalized = (status or "").upper()
    state = _STATUS_MAP.get(normalized, "PENDING")

    # Nothing applicable to write: converge to NOT_APPLICABLE instead of
    # waiting forever for a score that will never exist.
    if has_applicable_projection is False:
        return "NOT_APPLICABLE"

    if state == "FAILED" and max_attempts and attempts is not None and attempts >= max_attempts:
        return "RETRY_EXHAUSTED"
    return state


# ---------------------------------------------------------------------------
# Per-scope sync state (Issue #87)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeState:
    """The sync state of one independently reportable scope."""

    status: str
    reason: str | None = None
    task_count: int = 0
    failed_count: int = 0
    pending_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "task_count": self.task_count,
            "failed_count": self.failed_count,
            "pending_count": self.pending_count,
        }


# The worse state wins. NOT_APPLICABLE is the *best* outcome: a scope with
# nothing to write is healthy, so it never drags a synced scope down.
_SEVERITY = ("RETRY_EXHAUSTED", "FAILED", "PENDING", "SYNCED", "NOT_APPLICABLE")


def scope_state_from_tasks(
    tasks: list[Mapping[str, Any]],
    *,
    max_attempts: int = 5,
) -> ScopeState:
    """Fold outbox task rows into one user-facing scope state.

    A scope with no tasks converges to NOT_APPLICABLE so it never waits forever,
    and a scope that used up its attempts reads RETRY_EXHAUSTED rather than
    looking like a transient failure that might still succeed on its own.
    """
    if not tasks:
        return ScopeState(status="NOT_APPLICABLE", reason="没有需要同步的任务。")

    statuses = [str(task.get("status") or "").upper() for task in tasks]
    failed = [task for task in tasks if str(task.get("status") or "").upper() == "FAILED"]
    pending = [
        task
        for task in tasks
        if str(task.get("status") or "").upper() in ("PENDING", "PROCESSING")
    ]
    count = len(tasks)

    if failed:
        exhausted = [
            task
            for task in failed
            if max_attempts and (task.get("attempts") or 0) >= max_attempts
        ]
        if exhausted:
            reason = next(
                (task.get("last_error") for task in exhausted if task.get("last_error")),
                "重试次数已用尽。",
            )
            return ScopeState(
                status="RETRY_EXHAUSTED",
                reason=reason,
                task_count=count,
                failed_count=len(failed),
                pending_count=len(pending),
            )
        reason = next((task.get("last_error") for task in failed if task.get("last_error")), "同步失败。")
        return ScopeState(
            status="FAILED",
            reason=reason,
            task_count=count,
            failed_count=len(failed),
            pending_count=len(pending),
        )

    if pending:
        return ScopeState(
            status="PENDING",
            reason="正在同步到 Langfuse。",
            task_count=count,
            failed_count=0,
            pending_count=len(pending),
        )

    if all(status == "SYNCED" for status in statuses):
        return ScopeState(status="SYNCED", task_count=count)

    # Nothing was projectable: converge instead of waiting for a score that will
    # never be written.
    if all(status in ("SKIPPED", "NOT_APPLICABLE") for status in statuses):
        return ScopeState(
            status="NOT_APPLICABLE",
            reason="没有可投影到 Langfuse 的数值结果。",
            task_count=count,
        )

    if all(status in ("SYNCED", "SKIPPED") for status in statuses):
        return ScopeState(status="SYNCED", task_count=count)

    return ScopeState(
        status="NOT_APPLICABLE",
        reason="没有可投影的结果。",
        task_count=count,
    )


@dataclass(frozen=True)
class CombinedSyncState:
    overall: str
    item_trace: ScopeState
    run_score: ScopeState

    def to_payload(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "item_trace": self.item_trace.to_payload(),
            "run_score": self.run_score.to_payload(),
        }


def combine_scope_states(*, item_trace: ScopeState, run_score: ScopeState) -> CombinedSyncState:
    """The overall state, never hiding a broken scope behind a synced one.

    A synced Item/Trace scope with a failed Run Score scope is *not* SYNCED:
    the report must show exactly which part is still behind.
    """
    candidates = [item_trace.status, run_score.status]
    overall = min(candidates, key=lambda status: _SEVERITY.index(status))
    return CombinedSyncState(overall=overall, item_trace=item_trace, run_score=run_score)
