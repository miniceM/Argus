"""采集账本事务边界：先预留读取额度，再以租约/轮次 CAS 发布安全制品。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db_models import (
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
)
from ..evaluator_binding import canonical_digest
from ..execution_checkpoint import binding_provenance_from_manifest, canonical_output_digest
from .acquisition import _PERMANENT, _RETRYABLE
from .evidence import (
    TrajectoryEvidenceUnavailableError,
    _contract,
    _db_scope,
    load_trajectory_evidence,
    persist_trajectory_evidence,
)
from .schema import (
    Digest,
    Identity,
    StandardTrajectory,
    Timestamp,
    TrajectoryAcquisition,
    TrajectoryEvidenceRef,
    TrajectoryScope,
    _ContractModel,
)

_REASONS = _PERMANENT | _RETRYABLE | {
    "TRAJECTORY_ACQUISITION_PENDING", "TRAJECTORY_ROOT_UNAVAILABLE", "TRAJECTORY_SOURCE_BYTE_LIMIT",
    "TRAJECTORY_EVIDENCE_STORAGE_TIMEOUT",
} | {"TRAJECTORY_EVIDENCE_" + suffix for suffix in (
    "CONTRACT_INVALID", "INVENTORY_INVALID", "REDACTION_INVALID", "INVALID", "PROVENANCE_MISMATCH",
    "REASON_INVALID", "REFERENCE_INVALID", "COMPLETENESS_INVALID", "DIGEST_MISMATCH", "SCOPE_MISMATCH",
    "GENERATION_STALE", "MISSING", "REF_MISMATCH", "CORRUPT", "DIALECT_UNSUPPORTED", "STORAGE_UNAVAILABLE", "BYTE_LIMIT")}


class TrajectoryCheckpointError(ValueError):
    def __init__(self, code):
        self.reason_code = "TRAJECTORY_CHECKPOINT_" + code
        super().__init__(self.reason_code)


class _Continuation(_ContractModel):
    version: Literal["1"] = "1"
    contract_digest: Digest
    checkpoint_digest: Digest
    started_at: Timestamp
    charged_bytes: Annotated[int, Field(ge=0)] = 0
    revision: Annotated[int, Field(ge=0)] = 0
    round_token: Identity | None = None
    round_lease_token: Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,128}$")] | None = None
    round_bytes_before: Annotated[int, Field(ge=0)] | None = None
    candidate_ref: TrajectoryEvidenceRef | None = None


@dataclass(frozen=True)
class TrajectoryRoundClaim:
    checkpoint_id: str
    acquisition: TrajectoryAcquisition
    started_at: datetime
    source_bytes_used: int
    revision: int
    round_token: str | None
    candidate_ref: TrajectoryEvidenceRef | None
    candidate: StandardTrajectory | None


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now(value):
    if value is not None and (type(value) is not datetime or value.tzinfo is None):
        raise TrajectoryCheckpointError("CLOCK_INVALID")
    return value.astimezone(UTC) if value is not None else datetime.now(UTC)


def _rows(session, scope, *, locking):
    try:
        if type(scope) is not TrajectoryScope:
            raise ValueError
        scope = TrajectoryScope.model_validate(scope.model_dump())
    except ValueError:
        raise TrajectoryCheckpointError("SCOPE_MISMATCH") from None
    if locking and any(isinstance(row, (ExperimentLaunchRecord, ExperimentItemExecutionRecord,
            ExecutionAttemptRecord, ExecutionCheckpointRecord)) for row in session.dirty | session.deleted):
        raise TrajectoryCheckpointError("TRANSACTION_DIRTY")
    # 与 Worker 的全局顺序一致，避免在持有 checkpoint 锁时再取 Launch/Item。
    def get(model, identity):
        query = select(model).where(model.id == identity)
        if locking:
            query = query.with_for_update().execution_options(populate_existing=True)
        return session.scalar(query)
    launch = get(ExperimentLaunchRecord, scope.launch_id)
    item = get(ExperimentItemExecutionRecord, scope.item_execution_id)
    attempt = get(ExecutionAttemptRecord, scope.attempt_id)
    query = select(ExecutionCheckpointRecord).where(
        ExecutionCheckpointRecord.item_execution_id == scope.item_execution_id,
        ExecutionCheckpointRecord.dispatch_generation == scope.dispatch_generation)
    if locking:
        query = query.with_for_update().execution_options(populate_existing=True)
    checkpoint = session.scalar(query)
    if (any(row is None for row in (launch, item, attempt, checkpoint))
        or (item.launch_id, item.dataset_item_id, attempt.item_execution_id, attempt.dispatch_generation) != (
            scope.launch_id, scope.dataset_item_id, scope.item_execution_id, scope.dispatch_generation)
        or (checkpoint.launch_id, checkpoint.dataset_item_id, checkpoint.final_attempt_id,
            checkpoint.trace_id, checkpoint.observation_id) != (
            scope.launch_id, scope.dataset_item_id, scope.attempt_id, scope.trace_id, scope.root_observation_id)):
        raise TrajectoryCheckpointError("SCOPE_MISMATCH")
    return launch, item, checkpoint


def _fence(launch, item, scope, lease_token, now):
    if launch.cancel_requested_at or launch.status in {"CANCELLING", "CANCELLED"}:
        raise TrajectoryCheckpointError("CANCELLED")
    if item.dispatch_generation != scope.dispatch_generation:
        raise TrajectoryCheckpointError("GENERATION_STALE")
    if (item.execution_status != "running" or item.active_attempt_id != scope.attempt_id
        or not lease_token or item.lease_token != lease_token or item.lease_expires_at is None
        or _utc(item.lease_expires_at) <= now):
        raise TrajectoryCheckpointError("LEASE_LOST")


def _source_digest(launch, checkpoint):
    if (type(launch.manifest) is not dict or type(launch.manifest.get("dataset")) is not dict
        or type(launch.manifest.get("evaluators", [])) is not list
        or any(type(entry) is not dict for entry in launch.manifest.get("evaluators", []))
        or type(launch.manifest["dataset"].get("items")) is not list):
        raise TrajectoryCheckpointError("SOURCE_INVALID")
    if checkpoint.agent_output is None or canonical_output_digest(checkpoint.agent_output) != checkpoint.output_digest:
        raise TrajectoryCheckpointError("OUTPUT_INVALID")
    if (canonical_digest(launch.manifest) != checkpoint.manifest_digest
        or canonical_digest(binding_provenance_from_manifest(launch.manifest)) != canonical_digest(checkpoint.binding_provenance)):
        raise TrajectoryCheckpointError("MANIFEST_MISMATCH")
    items = launch.manifest.get("dataset", {}).get("items", [])
    matching = [item for item in items if isinstance(item, dict) and str(item.get("id")) == checkpoint.dataset_item_id]
    if len(matching) != 1:
        raise TrajectoryCheckpointError("INPUT_MISMATCH")
    original_input = matching[0].get("input", {})
    if (canonical_digest({"input": original_input if original_input is not None else {},
            "expected": matching[0].get("expected_output", {})}) != canonical_digest(
            {"input": checkpoint.input_payload, "expected": checkpoint.expected_output})):
        raise TrajectoryCheckpointError("INPUT_MISMATCH")
    return canonical_digest({"output_digest": checkpoint.output_digest, "input": checkpoint.input_payload,
        "expected_output": checkpoint.expected_output, "manifest_digest": checkpoint.manifest_digest,
        "binding_provenance": checkpoint.binding_provenance})


def _state(session, launch, checkpoint, scope, contract, *, initialize=False):
    _, profile = _contract(contract)
    try:
        _db_scope(session, scope, profile, writing=False)
    except TrajectoryEvidenceUnavailableError:
        raise TrajectoryCheckpointError("SOURCE_INVALID") from None
    try:
        digest = _source_digest(launch, checkpoint)
    except TrajectoryCheckpointError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise TrajectoryCheckpointError("SOURCE_INVALID") from None
    state_json, continuation_json = checkpoint.trajectory_acquisition_json, checkpoint.trajectory_runtime_json
    if state_json is None and continuation_json is None:
        if not initialize:
            raise TrajectoryCheckpointError("MISSING")
        continuation = _Continuation(contract_digest=contract.digest, checkpoint_digest=digest,
                                     started_at=_utc(checkpoint.created_at))
        state = TrajectoryAcquisition(scope=scope, state="PENDING", completeness=None, evidence_ref=None,
            reason_code="TRAJECTORY_ACQUISITION_PENDING", retryable=True,
            deadline_at=continuation.started_at + timedelta(seconds=float(contract.limits.deadline_seconds)))
        return state, continuation
    try:
        state = TrajectoryAcquisition.model_validate(state_json)
        continuation = _Continuation.model_validate(continuation_json)
        deadline = continuation.started_at + timedelta(seconds=float(contract.limits.deadline_seconds))
        if (state.scope != scope or continuation.contract_digest != contract.digest
            or continuation.checkpoint_digest != digest or continuation.started_at != _utc(checkpoint.created_at)
            or state.deadline_at != deadline or continuation.charged_bytes > contract.limits.max_source_bytes
            or (state.reason_code is not None and state.reason_code not in _REASONS)
            or (continuation.round_token is None) != (continuation.round_lease_token is None)
            or (continuation.round_token is None) != (continuation.round_bytes_before is None)
            or (continuation.round_bytes_before is not None and continuation.round_bytes_before > continuation.charged_bytes)):
            raise ValueError
        if continuation.round_token is not None and continuation.charged_bytes != contract.limits.max_source_bytes:
            raise ValueError
    except ValueError:
        raise TrajectoryCheckpointError("STATE_INVALID") from None
    return state, continuation


def _view(session, checkpoint, state, continuation, contract, *, bytes_before=None):
    candidate_ref = state.evidence_ref if state.state == "READY" else continuation.candidate_ref
    candidate = None
    if candidate_ref is not None and state.state != "UNAVAILABLE":
        try:
            candidate = load_trajectory_evidence(session, candidate_ref, scope=state.scope, contract=contract)
            if (state.completeness != candidate.completeness
                or (state.state == "PENDING" and candidate.completeness.status == "OBSERVED_COMPLETE")):
                raise TrajectoryCheckpointError("STATE_INVALID")
        except TrajectoryEvidenceUnavailableError as error:
            state = state.model_copy(update={"state": "UNAVAILABLE", "completeness": None, "evidence_ref": None,
                "reason_code": error.reason_code, "retryable": False})
            candidate_ref = None
    elif state.completeness is not None:
        raise TrajectoryCheckpointError("STATE_INVALID")
    return TrajectoryRoundClaim(checkpoint.id, state, continuation.started_at,
        continuation.charged_bytes if bytes_before is None else bytes_before,
        continuation.revision, continuation.round_token, candidate_ref, candidate)


def _write(checkpoint, state, continuation):
    checkpoint.trajectory_acquisition_json = state.model_dump(mode="json")
    checkpoint.trajectory_runtime_json = continuation.model_dump(mode="json")


def load_trajectory_checkpoint(session: Session, scope, contract) -> TrajectoryRoundClaim:
    """只读恢复轨迹指针，不变更历史状态，不依赖输出 checkpoint TTL。"""
    with session.no_autoflush:
        launch, _, checkpoint = _rows(session, scope, locking=False)
        state, continuation = _state(session, launch, checkpoint, scope, contract)
        return _view(session, checkpoint, state, continuation, contract)


def begin_trajectory_round(session: Session, scope, contract, *, lease_token, now=None) -> TrajectoryRoundClaim:
    """调用方先提交此事务再读取；崩溃后不退还未知在途字节的预留。"""
    with session.no_autoflush:
        launch, item, checkpoint = _rows(session, scope, locking=True)
        current = _now(now)  # 生产时钟在获取锁之后读取。
        _fence(launch, item, scope, lease_token, current)
        state, continuation = _state(session, launch, checkpoint, scope, contract, initialize=True)
        restored = _view(session, checkpoint, state, continuation, contract)
        current = _now(now)
        _fence(launch, item, scope, lease_token, current)
        if continuation.started_at > current:
            raise TrajectoryCheckpointError("CLOCK_INVALID")
        if restored.acquisition.state != "PENDING":
            return restored
        if state.deadline_at <= current:
            state = state.model_copy(update={"state": "READY", "evidence_ref": restored.candidate_ref,
                "completeness": restored.candidate.completeness, "reason_code": None, "retryable": False}) if restored.candidate else (
                state.model_copy(update={"state": "UNAVAILABLE", "completeness": None, "evidence_ref": None,
                    "reason_code": "TRAJECTORY_ROOT_UNAVAILABLE", "retryable": False}))
            continuation = continuation.model_copy(update={"revision": continuation.revision + 1,
                "round_token": None, "round_lease_token": None, "round_bytes_before": None})
            _write(checkpoint, state, continuation)
            return _view(session, checkpoint, state, continuation, contract)
        if continuation.round_token and continuation.round_lease_token == lease_token:
            raise TrajectoryCheckpointError("ROUND_BUSY")
        before = continuation.charged_bytes
        continuation = _Continuation.model_validate(continuation.model_dump() | {
            "revision": continuation.revision + 1, "round_token": uuid4().hex, "round_lease_token": lease_token,
            "round_bytes_before": before, "charged_bytes": contract.limits.max_source_bytes})
        _write(checkpoint, state, continuation)
        return _view(session, checkpoint, state, continuation, contract, bytes_before=before)


def _claim(session, claim, contract, lease_token, now):
    scope = claim.acquisition.scope
    launch, item, checkpoint = _rows(session, scope, locking=True)
    _fence(launch, item, scope, lease_token, _now(now))
    state, continuation = _state(session, launch, checkpoint, scope, contract)
    if (state.state != "PENDING" or claim.checkpoint_id != checkpoint.id or not claim.round_token
        or (continuation.revision, continuation.round_token, continuation.round_lease_token,
            continuation.round_bytes_before) != (claim.revision, claim.round_token, lease_token, claim.source_bytes_used)):
        raise TrajectoryCheckpointError("ROUND_STALE")
    return checkpoint, state, continuation, launch, item


def publish_trajectory_round(session: Session, claim, contract, *, lease_token, result,
                             source_bytes_used, candidate: StandardTrajectory | None, now=None) -> TrajectoryRoundClaim:
    """候选制品和最终状态同事务；不 commit，不抹调用方其它 ORM 改动。"""
    with session.no_autoflush:
        checkpoint, previous, continuation, launch, item = _claim(session, claim, contract, lease_token, now)
        if (type(source_bytes_used) is not int or not claim.source_bytes_used <= source_bytes_used <= contract.limits.max_source_bytes):
            raise TrajectoryCheckpointError("BUDGET_INVALID")
        try:
            result = TrajectoryAcquisition.model_validate(result.model_dump())
            if (result.scope != previous.scope or result.deadline_at != previous.deadline_at
                or (result.reason_code is not None and result.reason_code not in _REASONS)):
                raise ValueError
        except ValueError:
            raise TrajectoryCheckpointError("STATE_INVALID") from None
        ref = None
        if result.state == "PENDING":
            if result.deadline_at <= _now(now):
                raise TrajectoryCheckpointError("DEADLINE_EXPIRED")
            if candidate is not None:
                if candidate.scope != previous.scope:
                    raise TrajectoryCheckpointError("SCOPE_MISMATCH")
                if candidate.completeness.status == "OBSERVED_COMPLETE":
                    raise TrajectoryCheckpointError("STATE_INVALID")
                ref = persist_trajectory_evidence(session, candidate, contract)
            result = result.model_copy(update={"completeness": candidate.completeness if candidate else None})
        else:
            if candidate is not None:
                raise TrajectoryCheckpointError("STATE_INVALID")
            if result.state == "READY":
                restored = load_trajectory_evidence(session, result.evidence_ref, scope=previous.scope, contract=contract)
                if restored.completeness != result.completeness:
                    raise TrajectoryCheckpointError("STATE_INVALID")
                ref = result.evidence_ref
        continuation = _Continuation.model_validate(continuation.model_dump() | {
            "charged_bytes": source_bytes_used, "revision": continuation.revision + 1,
            "round_token": None, "round_lease_token": None, "round_bytes_before": None, "candidate_ref": ref})
        restored = _view(session, checkpoint, result, continuation, contract)
        _fence(launch, item, previous.scope, lease_token, _now(now))
        if result.state == "PENDING" and result.deadline_at <= _now(now):
            raise TrajectoryCheckpointError("DEADLINE_EXPIRED")
        _write(checkpoint, result, continuation)
        return restored


def abort_trajectory_round(session: Session, claim, contract, *, lease_token, now=None) -> bool:
    """不发布采集结果，不退已预留额度；过期/被替换的轮次不可写入。"""
    with session.no_autoflush:
        checkpoint, state, continuation, launch, item = _claim(session, claim, contract, lease_token, now)
        continuation = continuation.model_copy(update={"revision": continuation.revision + 1,
            "round_token": None, "round_lease_token": None, "round_bytes_before": None})
        _fence(launch, item, state.scope, lease_token, _now(now))
        _write(checkpoint, state, continuation)
        return True
