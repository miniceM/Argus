"""仅装载冻结的本地证据；不采集来源、不创建质量或 Snapshot 资格。"""

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import select

from ..db_models import ExecutionCheckpointRecord, RunResultSnapshotRecord
from ..execution_checkpoint import CheckpointUnavailableError, canonical_output_digest, validate_checkpoint_manifest
from .checkpoint import _REASONS, TrajectoryCheckpointError, load_trajectory_checkpoint
from .evidence import TrajectoryEvidenceUnavailableError, load_trajectory_evidence
from .manifest import TrajectoryManifestError, load_trajectory_contract
from .schema import StandardTrajectory, TrajectoryAcquisition, TrajectoryEvidenceRef, TrajectoryScope


@dataclass(frozen=True)
class EvaluationContext:
    output: object
    expected_output: object
    input_payload: object
    trajectory: StandardTrajectory | None
    trajectory_evidence_ref: TrajectoryEvidenceRef | None


@dataclass(frozen=True)
class FrozenTrajectoryInput:
    trajectory_contract_digest: str | None
    acquisition: TrajectoryAcquisition | None = None
    trajectory: StandardTrajectory | None = None
    reason_code: str | None = None

    @property
    def trajectory_evidence_ref(self):
        return self.acquisition.evidence_ref if self.trajectory is not None else None

    def to_payload(self):
        return {"version": "1.0", "trajectory_contract_digest": self.trajectory_contract_digest,
            "acquisition": self.acquisition.model_dump(mode="json") if self.acquisition else None,
            "reason_code": self.reason_code}


def load_checkpoint_input(session, checkpoint, manifest):
    if manifest.get("schema_version") != "1.3":
        return None
    try:
        contract = load_trajectory_contract(manifest)
    except TrajectoryManifestError:
        return FrozenTrajectoryInput(None, reason_code="TRAJECTORY_INPUT_MANIFEST_INVALID")
    if checkpoint is None:
        return FrozenTrajectoryInput(contract.digest, reason_code="TRAJECTORY_INPUT_CHECKPOINT_MISSING")
    try:
        validate_checkpoint_manifest(checkpoint, manifest, session=session)
        if checkpoint.agent_output is None or canonical_output_digest(checkpoint.agent_output) != checkpoint.output_digest:
            raise CheckpointUnavailableError("CHECKPOINT_CORRUPT")
    except CheckpointUnavailableError:
        return FrozenTrajectoryInput(contract.digest, reason_code="TRAJECTORY_INPUT_CHECKPOINT_INVALID")
    try:
        scope = TrajectoryScope(launch_id=checkpoint.launch_id, item_execution_id=checkpoint.item_execution_id,
            dataset_item_id=checkpoint.dataset_item_id, attempt_id=checkpoint.final_attempt_id,
            dispatch_generation=checkpoint.dispatch_generation, trace_id=checkpoint.trace_id,
            root_observation_id=checkpoint.observation_id)
    except ValueError:
        return FrozenTrajectoryInput(contract.digest, reason_code="TRAJECTORY_INPUT_SCOPE_UNAVAILABLE")
    try:
        saved = load_trajectory_checkpoint(session, scope, contract)
    except TrajectoryCheckpointError:
        return FrozenTrajectoryInput(contract.digest, reason_code="TRAJECTORY_INPUT_CHECKPOINT_INVALID")
    state = saved.acquisition
    trajectory = saved.candidate if state.state == "READY" else None
    reason = None if trajectory is not None else state.reason_code or "TRAJECTORY_ACQUISITION_PENDING"
    return FrozenTrajectoryInput(contract.digest, state, trajectory, reason)


def make_evaluation_context(session, checkpoint, manifest):
    loaded = load_checkpoint_input(session, checkpoint, manifest)
    if loaded is None or checkpoint is None or loaded.reason_code in {
        "TRAJECTORY_INPUT_MANIFEST_INVALID", "TRAJECTORY_INPUT_CHECKPOINT_INVALID"}:
        return None
    return EvaluationContext(deepcopy(checkpoint.agent_output), deepcopy(checkpoint.expected_output),
        deepcopy(checkpoint.input_payload), loaded.trajectory, loaded.trajectory_evidence_ref)


def context_for_item(session, *, item_id, generation, manifest):
    if manifest.get("schema_version") != "1.3":
        return None
    checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(
        ExecutionCheckpointRecord.item_execution_id == item_id,
        ExecutionCheckpointRecord.dispatch_generation == generation))
    return make_evaluation_context(session, checkpoint, manifest)


def freeze_trajectory_input(session, *, item, manifest):
    if manifest.get("schema_version") != "1.3":
        return None
    with session.no_autoflush:
        checkpoint = session.scalar(select(ExecutionCheckpointRecord).where(
            ExecutionCheckpointRecord.item_execution_id == item.id,
            ExecutionCheckpointRecord.dispatch_generation == item.dispatch_generation))
        return load_checkpoint_input(session, checkpoint, manifest).to_payload()


def load_snapshot_input(session, *, snapshot_id, dataset_item_id):
    """历史引用只使用 Snapshot 自己的合同，不查 CP/在线 Trace 或当前最新配置。"""
    from ..result_snapshots import _canonical_digest
    snapshot = session.get(RunResultSnapshotRecord, snapshot_id)
    if snapshot is None:
        return FrozenTrajectoryInput(None, reason_code="TRAJECTORY_INPUT_SNAPSHOT_MISSING")
    if type(snapshot.manifest) is not dict:
        return FrozenTrajectoryInput(None, reason_code="TRAJECTORY_INPUT_SNAPSHOT_INVALID")
    try:
        contract = load_trajectory_contract(snapshot.manifest)
        if contract is None:
            return None
        if (snapshot.manifest_digest != _canonical_digest(snapshot.manifest)
            or snapshot.source_result_digest != _canonical_digest(snapshot.items)):
            raise ValueError
        matches = [row for row in snapshot.items if row.get("dataset_item_id") == dataset_item_id]
        if len(matches) != 1:
            raise ValueError
        row = matches[0]
        payload = row.get("trajectory_input")
        if (type(payload) is not dict or set(payload) != {"version", "trajectory_contract_digest", "acquisition", "reason_code"}
            or payload["version"] != "1.0" or payload["trajectory_contract_digest"] != contract.digest):
            raise ValueError
        reason = payload["reason_code"]
        if payload["acquisition"] is None:
            if reason not in {"TRAJECTORY_INPUT_MANIFEST_INVALID", "TRAJECTORY_INPUT_CHECKPOINT_MISSING",
                "TRAJECTORY_INPUT_CHECKPOINT_INVALID", "TRAJECTORY_INPUT_SCOPE_UNAVAILABLE"}:
                raise ValueError
            return FrozenTrajectoryInput(contract.digest, reason_code=reason)
        acquisition = TrajectoryAcquisition.model_validate(payload["acquisition"])
        scope = acquisition.scope
        if ((scope.launch_id, scope.dataset_item_id, scope.attempt_id, scope.dispatch_generation,
                scope.trace_id, scope.root_observation_id) != (snapshot.launch_id, dataset_item_id,
                row["final_attempt_id"], row["dispatch_generation"], row["trace_id"], row["observation_id"])
            or (reason is not None and reason not in _REASONS)
            or (acquisition.reason_code is not None and acquisition.reason_code not in _REASONS)
            or (acquisition.state == "READY" and reason is not None)):
            raise ValueError
        if acquisition.state != "READY":
            return FrozenTrajectoryInput(contract.digest, acquisition, reason_code=reason or acquisition.reason_code)
        trajectory = load_trajectory_evidence(session, acquisition.evidence_ref, scope=scope, contract=contract)
        if trajectory.completeness != acquisition.completeness:
            raise ValueError
        return FrozenTrajectoryInput(contract.digest, acquisition, trajectory)
    except TrajectoryEvidenceUnavailableError as exc:
        return FrozenTrajectoryInput(contract.digest, acquisition, reason_code=exc.reason_code)
    except (ValueError, TypeError, KeyError, AttributeError):
        return FrozenTrajectoryInput(None, reason_code="TRAJECTORY_INPUT_SNAPSHOT_INVALID")
