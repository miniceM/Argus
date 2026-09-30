"""Recoverable execution checkpoints (Argus Issue #84).

When an Agent answers with HTTP 200, its response is persisted *before*
evaluation runs. That artifact — the original output, a content digest, the
dataset input/expected output and the frozen Binding provenance — is what makes
an evaluation-only retry possible: the Agent is never called again.

The checkpoint is deliberately small. It stores the business response and the
inputs needed to re-judge it, with an explicit retention window, and it is
verifiable and parsable locally. It is **not** a second copy of the Langfuse
Trace and never a substitute for one: recovery works even when Langfuse is
unavailable or has not synced.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db_models import ExecutionCheckpointRecord

# Stable, user-facing reasons why an evaluation-only retry is blocked. They are
# never silently downgraded to "call the Agent again".
CHECKPOINT_MISSING = "CHECKPOINT_MISSING"
CHECKPOINT_EXPIRED = "CHECKPOINT_EXPIRED"
CHECKPOINT_CORRUPT = "CHECKPOINT_CORRUPT"
CHECKPOINT_OUTPUT_MISSING = "CHECKPOINT_OUTPUT_MISSING"
CHECKPOINT_DISABLED = "CHECKPOINT_DISABLED"
CHECKPOINT_BINDING_MISMATCH = "CHECKPOINT_BINDING_MISMATCH"

CHECKPOINT_ERROR_MESSAGES: dict[str, str] = {
    CHECKPOINT_MISSING: "该用例没有可复用的 Agent 输出检查点，无法仅重试评测。",
    CHECKPOINT_EXPIRED: "Agent 输出检查点已过保留期，无法仅重试评测。",
    CHECKPOINT_CORRUPT: "Agent 输出检查点已损坏或摘要不匹配，无法仅重试评测。",
    CHECKPOINT_OUTPUT_MISSING: "Agent 输出检查点缺少原始输出，无法仅重试评测。",
    CHECKPOINT_DISABLED: "当前部署未启用 Agent 输出检查点保留，无法仅重试评测。",
    CHECKPOINT_BINDING_MISMATCH: "Agent 输出检查点的冻结 Binding 与当前 Manifest 不一致，无法仅重试评测。",
}

RECOVERY_HINTS: dict[str, str] = {
    CHECKPOINT_MISSING: "重新执行该用例会再次调用 Agent，请确认业务可接受重复调用后再操作。",
    CHECKPOINT_EXPIRED: "重新执行该用例会再次调用 Agent，请确认业务可接受重复调用后再操作。",
    CHECKPOINT_CORRUPT: "请检查数据库完整性；系统不会退化为重新调用 Agent。",
    CHECKPOINT_OUTPUT_MISSING: "请检查数据库完整性；系统不会退化为重新调用 Agent。",
    CHECKPOINT_DISABLED: "如需仅重试评测，请开启 ARGUS_EXECUTION_CHECKPOINT_TTL_SECONDS。",
    CHECKPOINT_BINDING_MISMATCH: "评测身份已变化，请创建新的 Launch 以保证可复现性。",
}


class CheckpointUnavailableError(RuntimeError):
    """Raised when an evaluation-only retry cannot reuse a stored output.

    Carries a stable ``code`` plus a user-facing message and recovery hint, so
    the API can explain exactly why recovery is blocked instead of quietly
    re-invoking the Agent.
    """

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or CHECKPOINT_ERROR_MESSAGES.get(code, code)
        self.hint = RECOVERY_HINTS.get(code)
        super().__init__(self.message)

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def canonical_output_digest(output: Any) -> str:
    """Content digest of an Agent response, stable across key order.

    A missing output is a distinct, explicit state — never an empty digest that
    could collide with a real response.
    """
    if output is None:
        return ""
    canonical = json.dumps(output, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def checkpoint_retention_enabled() -> bool:
    return settings.execution_checkpoint_ttl_seconds > 0


def checkpoint_expiry(now: datetime | None = None) -> datetime:
    base = now or datetime.now(UTC)
    return base + timedelta(seconds=settings.execution_checkpoint_ttl_seconds)


def binding_provenance_from_manifest(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The frozen Binding identity a stored output must be judged under.

    Recovery re-uses the *same* frozen Manifest, so this is recorded on the
    checkpoint to detect a mismatch instead of silently re-judging an output
    under a different evaluator contract.
    """
    from .evaluator_binding import EvaluatorBinding

    entries = (manifest or {}).get("evaluators") or []
    provenance: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        binding = EvaluatorBinding.from_payload(dict(entry))
        provenance.append(
            {
                "evaluator_id": binding.evaluator_id,
                "version": binding.version,
                "binding_id": binding.binding_id,
                "definition_digest": binding.definition_digest or None,
                "executor_type": binding.executor_type,
                "scope": binding.scope,
                "contract_status": binding.contract_status,
            }
        )
    return provenance


def write_execution_checkpoint(
    session: Session,
    *,
    item_execution_id: str,
    launch_id: str,
    dataset_item_id: str,
    dispatch_generation: int,
    output: Any,
    input_payload: Any,
    expected_output: Any,
    manifest: dict[str, Any],
    final_attempt_id: str | None = None,
    trace_id: str | None = None,
    observation_id: str | None = None,
    langfuse_trace_url: str | None = None,
    now: datetime | None = None,
) -> ExecutionCheckpointRecord | None:
    """Persist (or refresh) the recoverable output of one execution generation.

    Idempotent per ``(item_execution_id, dispatch_generation)``: a re-execution
    of the same generation refreshes the record instead of creating duplicates.
    Returns ``None`` when retention is disabled, which simply means
    evaluation-only recovery is unavailable for this deployment.
    """
    if not checkpoint_retention_enabled():
        return None

    from .evaluator_binding import canonical_digest

    now = now or datetime.now(UTC)
    digest = canonical_output_digest(output)

    existing = session.scalars(
        select(ExecutionCheckpointRecord).where(
            ExecutionCheckpointRecord.item_execution_id == item_execution_id,
            ExecutionCheckpointRecord.dispatch_generation == dispatch_generation,
        )
    ).first()

    values = {
        "launch_id": launch_id,
        "dataset_item_id": dataset_item_id,
        "output_digest": digest,
        "agent_output": output,
        "input_payload": input_payload if input_payload is not None else {},
        "expected_output": expected_output,
        "binding_provenance": binding_provenance_from_manifest(manifest),
        "manifest_digest": canonical_digest(manifest or {}),
        "final_attempt_id": final_attempt_id,
        "trace_id": trace_id,
        "observation_id": observation_id,
        "langfuse_trace_url": langfuse_trace_url,
        "expires_at": checkpoint_expiry(now),
        "updated_at": now,
    }

    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        return existing

    record = ExecutionCheckpointRecord(
        id=f"cp_{uuid.uuid4().hex[:24]}",
        item_execution_id=item_execution_id,
        dispatch_generation=dispatch_generation,
        created_at=now,
        **values,
    )
    session.add(record)
    return record


def load_recoverable_checkpoint(
    session: Session,
    *,
    item_execution_id: str,
    dispatch_generation: int,
    manifest: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ExecutionCheckpointRecord:
    """Load and fully validate the checkpoint for an evaluation-only retry.

    Every failure raises :class:`CheckpointUnavailableError` with a stable code,
    so a missing / expired / corrupt artifact blocks the retry with a clear
    reason and never silently degrades into re-invoking the Agent.
    """
    if not checkpoint_retention_enabled():
        raise CheckpointUnavailableError(CHECKPOINT_DISABLED)

    record = session.scalars(
        select(ExecutionCheckpointRecord).where(
            ExecutionCheckpointRecord.item_execution_id == item_execution_id,
            ExecutionCheckpointRecord.dispatch_generation == dispatch_generation,
        )
    ).first()
    if record is None:
        raise CheckpointUnavailableError(CHECKPOINT_MISSING)

    now = now or datetime.now(UTC)
    expires_at = record.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at is not None and expires_at <= now:
        raise CheckpointUnavailableError(CHECKPOINT_EXPIRED)

    if record.agent_output is None:
        raise CheckpointUnavailableError(CHECKPOINT_OUTPUT_MISSING)

    # The stored artifact must still hash to its recorded digest; this is what
    # makes "reusing the original output" a verifiable claim.
    actual = canonical_output_digest(record.agent_output)
    if not record.output_digest or actual != record.output_digest:
        raise CheckpointUnavailableError(CHECKPOINT_CORRUPT)

    # The frozen Binding provenance must still match the Manifest, otherwise the
    # output would be judged under a different evaluator contract.
    if manifest is not None:
        expected_provenance = binding_provenance_from_manifest(manifest)
        stored = record.binding_provenance or []
        if _provenance_key(stored) != _provenance_key(expected_provenance):
            raise CheckpointUnavailableError(CHECKPOINT_BINDING_MISMATCH)

    return record


def _provenance_key(provenance: list[dict[str, Any]] | None) -> list[tuple[Any, ...]]:
    return sorted(
        (
            str(entry.get("evaluator_id") or ""),
            str(entry.get("version") or ""),
            str(entry.get("binding_id") or ""),
            str(entry.get("definition_digest") or ""),
        )
        for entry in (provenance or [])
        if isinstance(entry, dict)
    )
