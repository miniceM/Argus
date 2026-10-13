"""不可变最小轨迹制品；事务由调用方拥有，不读取在线 Trace。"""

from collections import defaultdict
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..db_models import (
    AgentVersionRecord,
    ExecutionAttemptRecord,
    ExecutionCheckpointRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    TrajectoryEvidenceArtifactRecord,
)
from .assembler import trajectory_digest
from .causal import _cycle_nodes, _opaque
from .codec import canonical_dumps, canonical_loads
from .contract import TrajectoryContract
from .paths import compile_jsonpath
from .redaction import project_json
from .schema import StandardTrajectory, TrajectoryEvidenceRef, TrajectoryScope

_RANK = {"NONE": 0, "PARTIAL": 1, "COMPLETE": 2}
# 仅接纳当前归一化/图/因果/组装器发出的稳定诊断，不接纳来源自由文本。
_WARNINGS = frozenset({
    "SOURCE_ROW_LIMIT", "SOURCE_BYTE_LIMIT", "SOURCE_FIELD_INVALID", "SOURCE_FIELD_ALIAS_CONFLICT",
    "SOURCE_IDENTITY_INVALID", "SOURCE_TRACE_SCOPE_MISMATCH", "SOURCE_PARENT_UNAVAILABLE",
    "SOURCE_ROOT_NAME_INVALID", "SOURCE_PREDICATE_MISMATCH", "STEP_SEMANTICS_UNAVAILABLE",
    "TOOL_MAPPING_UNRESOLVED", "SOURCE_NAME_NOT_APPROVED", "STEP_TIME_INVALID", "STEP_DURATION_UNAVAILABLE",
    "ARGUMENTS_NOT_COLLECTED", "ARGUMENTS_INVALID", "OUTPUT_SUMMARY_UNAVAILABLE", "WORKFLOW_IDENTITY_UNAVAILABLE",
    "TERMINAL_INVENTORY_INVALID", "TERMINAL_INVENTORY_MISSING", "TERMINAL_INVENTORY_CONFLICT",
    "TERMINAL_INVENTORY_INCOMPLETE", "ARGUMENTS_PAIR_CONFLICT", "ACQUISITION_CONVERGENCE_UNPROVEN",
    "ACQUISITION_PAGING_INCOMPLETE", "ACQUISITION_QUIET_UNPROVEN", "TRAJECTORY_REQUIRED_CAPABILITY_UNAVAILABLE",
    "TRAJECTORY_ARTIFACT_LIMIT", "TRAJECTORY_CAUSAL_REFERENCE_TRUNCATED", "TRAJECTORY_PARENT_UNAVAILABLE",
    "GRAPH_PARENT_CYCLE", "GRAPH_ID_CONFLICT", "GRAPH_TRACE_SCOPE_UNAVAILABLE", "GRAPH_PARENT_UNAVAILABLE",
    "GRAPH_PARENT_MISSING", "GRAPH_TRACE_SCOPE_MISMATCH", "GRAPH_SOURCE_BYTES_INCOMPLETE",
    "GRAPH_IDENTITY_UNAVAILABLE", "GRAPH_CLOCK_SKEW", "GRAPH_STEP_LIMIT", "CAUSAL_CALL_CONFLICT",
    "CAUSAL_CALL_PAIR_UNAVAILABLE", "CAUSAL_CYCLE", "CAUSAL_EVENT_CONFLICT", "CAUSAL_EVENT_UNAVAILABLE",
    "CAUSAL_HUMAN_UNAVAILABLE", "CAUSAL_REFERENCE_UNAVAILABLE", "CAUSAL_SEQUENCE_CONFLICT",
    "CAUSAL_SOURCE_UNAVAILABLE", "CAUSAL_TIME_CONFLICT", "CAUSAL_TIME_UNAVAILABLE",
})


class TrajectoryEvidenceUnavailableError(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(code):
    raise TrajectoryEvidenceUnavailableError("TRAJECTORY_EVIDENCE_" + code) from None


def _contract(contract):
    if type(contract) is not TrajectoryContract:
        _fail("CONTRACT_INVALID")
    try:
        checked = TrajectoryContract.model_validate(contract.model_dump())
        profile = contract.source_profile
    except ValueError:
        _fail("CONTRACT_INVALID")
    # 组装器生成的 event→Step 映射是保留协议，不能同时作为业务投影路径。
    reserved = ("terminal_inventory", "observed_events")
    if any(tokens[:len(reserved)] == reserved or reserved[:len(tokens)] == tokens
           for tokens in (compile_jsonpath(path) for path in checked.redaction_policy.result_paths)):
        _fail("CONTRACT_INVALID")
    return checked, profile


def _inventory(step, steps, limit):
    value = step.result_summary
    if (step.phase != "EVENT" or step.workflow_scope_id is None or type(value) is not dict
        or type(value.get("terminal_inventory")) is not dict or "observed_events" not in value["terminal_inventory"]):
        return None
    inventory = value["terminal_inventory"]
    if (step.phase != "EVENT" or step.workflow_scope_id is None or set(value) != {"terminal_inventory"}
        or type(inventory) is not dict
        or set(inventory) != {"event_count", "event_ids", "final_sequence", "observed_events"}):
        _fail("INVENTORY_INVALID")
    count, ids, final, events = (inventory[key] for key in ("event_count", "event_ids", "final_sequence", "observed_events"))
    if (type(count) is not int or not 0 <= count <= limit or type(final) is not int or not 1 <= final <= limit
        or type(ids) is not list or len(ids) != count or any(not _opaque(identity) for identity in ids)
        or len(ids) != len(set(ids)) or type(events) is not list or len(events) > limit):
        _fail("INVENTORY_INVALID")
    seen = set()
    for event in events:
        if (type(event) is not dict or set(event) != {"step_id", "event_id", "sequence"}
            or type(event["step_id"]) is not str or event["step_id"] not in steps or event["step_id"] in seen
            or steps[event["step_id"]].workflow_scope_id != step.workflow_scope_id or not _opaque(event["event_id"])
            or type(event["sequence"]) is not int or not 1 <= event["sequence"] <= limit):
            _fail("INVENTORY_INVALID")
        seen.add(event["step_id"])
    return inventory


def inventory_complete(trajectory: StandardTrajectory, contract: TrajectoryContract) -> bool:
    """仅重算冻结的 inventory 覆盖；不推断原始采集/静默过程。"""
    _, profile = _contract(contract)
    return _inventory_complete(trajectory, profile)


def _structural(step, profile):
    return (any((step.type, step.raw_name) == (role.step_type, role.safe_name) for role in profile.structural_roles)
            and step.phase == "EVENT" and step.status == "UNKNOWN" and step.tool_identity is None
            and step.duration_ms is None and step.arguments_redacted is None and not step.argument_visibility
            and step.result_summary is None and step.error_code is None and not step.causal_predecessor_ids)


def _inventory_complete(trajectory, profile):
    steps = {step.step_id: step for step in trajectory.steps}
    workflows, terminals = defaultdict(set), defaultdict(list)
    for step in trajectory.steps:
        if step.step_id == trajectory.scope.root_observation_id:
            continue
        if step.workflow_scope_id is None:
            if _structural(step, profile):
                continue
            return False
        workflows[step.workflow_scope_id].add(step.step_id)
        inventory = _inventory(step, steps, 1000)
        if inventory is not None:
            terminals[step.workflow_scope_id].append((step, inventory))
    if not workflows:
        return False
    for workflow, expected in workflows.items():
        if len(terminals[workflow]) != 1:
            return False
        terminal, inventory = terminals[workflow][0]
        events = inventory["observed_events"]
        sequences = [event["sequence"] for event in events]
        ids = [event["event_id"] for event in events]
        own = next((event for event in events if event["step_id"] == terminal.step_id), None)
        if (set(event["step_id"] for event in events) != expected or len(ids) != len(set(ids))
            or set(ids) != set(inventory["event_ids"]) or inventory["event_count"] != len(expected)
            or inventory["final_sequence"] != len(expected) or set(sequences) != set(range(1, len(expected) + 1))
            or own is None or own["sequence"] != inventory["final_sequence"]):
            return False
    return True


def _approved(value, paths, limits):
    projected = project_json(value, paths, limits=limits)
    if canonical_dumps(projected.value) != canonical_dumps(value):
        _fail("REDACTION_INVALID")
    return projected


def _validate(trajectory, contract, profile):
    if type(trajectory) is not StandardTrajectory:
        _fail("INVALID")
    try:
        owned = StandardTrajectory.model_validate(trajectory.model_dump())
        provenance = owned.provenance
        expected = {
            "provider": profile.provider, "source_profile_id": profile.profile_id, "source_profile_version": profile.version,
            "schema_version": contract.schema_version, "assembler_version": contract.assembler_version,
            "tool_mapping_version": contract.tool_mapping.version, "tool_mapping_digest": contract.tool_mapping.digest,
            "redaction_policy_version": contract.redaction_policy.version, "redaction_policy_digest": contract.redaction_policy.digest,
            "trajectory_contract_digest": contract.digest,
        }
        if any(getattr(provenance, field) != value for field, value in expected.items()):
            _fail("PROVENANCE_MISMATCH")
        policy, limits = contract.redaction_policy, contract.limits
        payload = canonical_dumps(owned, max_bytes=limits.max_artifact_bytes)
        # 当前来源合同尚未定义 error_code 映射；启用前禁止非空值。
        if not set(owned.completeness.warnings) <= _WARNINGS or any(step.error_code is not None for step in owned.steps):
            _fail("REASON_INVALID")
        _approved(owned.input_redacted, policy.input_paths, limits)
        _approved(owned.output_redacted, policy.output_paths, limits)
        steps = {step.step_id: step for step in owned.steps}
        root = steps[owned.scope.root_observation_id]
        names = set(profile.safe_names.values()) | {role.safe_name for role in profile.structural_roles} | {"unapproved-name"}
        mappings = [entry for entry in contract.tool_mapping.entries
                    if (entry.source_profile_id, entry.source_profile_version) == (profile.profile_id, profile.version)]
        names.update(entry.raw_name for entry in mappings)
        tools = {entry.tool_identity for entry in mappings}
        if root.raw_name != "remote-agent-http" or root.type != "CUSTOM" or root.phase != "EXECUTED":
            _fail("REDACTION_INVALID")
        if (canonical_dumps(owned.input_redacted) != canonical_dumps(root.arguments_redacted)
            or canonical_dumps(owned.output_redacted) != canonical_dumps(root.result_summary)):
            _fail("REDACTION_INVALID")
        if len(steps) > limits.max_steps or [step.index for step in owned.steps] != list(range(len(steps))):
            _fail("INVALID")
        parent_edges, causal_edges = {}, {}
        for step in owned.steps:
            if (step.raw_name not in names or step.tool_identity is not None and step.tool_identity not in tools
                or step.workflow_scope_id is not None and not _opaque(step.workflow_scope_id)):
                _fail("REDACTION_INVALID")
            paths = policy.input_paths if step.step_id == root.step_id else policy.argument_paths
            projected = _approved(step.arguments_redacted, paths, limits)
            for path, visibility in step.argument_visibility.items():
                if path not in paths or (visibility in {"AVAILABLE", "MISSING"} and projected.visibility.get(path) != visibility
                                        or visibility in {"REDACTED", "INVALID"} and projected.visibility.get(path) == "AVAILABLE"):
                    _fail("REDACTION_INVALID")
            inventory = _inventory(step, steps, limits.max_steps)
            if inventory is None:
                _approved(step.result_summary, policy.output_paths if step.step_id == root.step_id else policy.result_paths, limits)
            references = set(step.causal_predecessor_ids)
            if not references <= steps.keys() or step.step_id == root.step_id and references:
                _fail("REFERENCE_INVALID")
            for ref in references:
                before = steps[ref]
                if (before.workflow_scope_id is None or before.workflow_scope_id != step.workflow_scope_id
                    or any(value is None for value in (before.started_at, before.ended_at, step.started_at, step.ended_at))
                    or before.ended_at < before.started_at or step.ended_at < step.started_at or before.ended_at > step.started_at):
                    _fail("REFERENCE_INVALID")
            causal_edges[step.step_id] = references
            parent = step.parent_step_id
            if step.step_id == root.step_id:
                if parent in steps:
                    _fail("REFERENCE_INVALID")
                parent_edges[step.step_id] = set()
            else:
                if parent is not None and parent not in steps:
                    _fail("REFERENCE_INVALID")
                parent_edges[step.step_id] = {parent} if parent is not None else set()
        if _cycle_nodes(parent_edges) or _cycle_nodes(causal_edges):
            _fail("REFERENCE_INVALID")
        completeness = owned.completeness
        if any(_RANK[value] > _RANK[profile.capabilities[key]] for key, value in completeness.capabilities.items()):
            _fail("COMPLETENESS_INVALID")
        if completeness.status == "OBSERVED_COMPLETE" and (
            profile.completion_protocol != "workflow-inventory-v1" or not _inventory_complete(owned, profile)
            or any(completeness.capabilities[key] != "COMPLETE" for key in contract.required_capabilities)
            or any(step.step_id != root.step_id and step.parent_step_id is None for step in owned.steps)
        ):
            _fail("INVENTORY_INVALID")
        if completeness.status == "ROOT_ONLY" and (len(owned.steps) != 1 or any(
            value != "NONE" for value in completeness.capabilities.values())):
            _fail("COMPLETENESS_INVALID")
        if trajectory_digest(owned) != provenance.trajectory_digest:
            _fail("DIGEST_MISMATCH")
    except TrajectoryEvidenceUnavailableError:
        raise
    except ValueError as error:
        _fail("BYTE_LIMIT" if str(error) == "TRAJECTORY_JSON_BYTE_LIMIT" else "INVALID")
    return owned, payload


def _db_scope(session, scope, profile, *, writing):
    item, attempt, launch, version = ExperimentItemExecutionRecord, ExecutionAttemptRecord, ExperimentLaunchRecord, AgentVersionRecord
    query = select(item.launch_id, item.dataset_item_id, item.dispatch_generation,
                   attempt.dispatch_generation.label("attempt_generation"), version.trajectory_source_profile_id).join(
        attempt, attempt.item_execution_id == item.id).join(launch, item.launch_id == launch.id).join(
        version, launch.agent_version_id == version.id).where(item.id == scope.item_execution_id, attempt.id == scope.attempt_id)
    if writing:
        query = query.with_for_update(of=(item, attempt))
    row = session.execute(query).one_or_none()
    if row is None or (row.launch_id, row.dataset_item_id, row.attempt_generation) != (
        scope.launch_id, scope.dataset_item_id, scope.dispatch_generation):
        _fail("SCOPE_MISMATCH")
    if row.trajectory_source_profile_id != profile.profile_id:
        _fail("PROVENANCE_MISMATCH")
    if writing:
        if row.dispatch_generation != scope.dispatch_generation:
            _fail("GENERATION_STALE")
        checkpoint = session.execute(select(ExecutionCheckpointRecord.launch_id, ExecutionCheckpointRecord.dataset_item_id,
            ExecutionCheckpointRecord.final_attempt_id, ExecutionCheckpointRecord.trace_id, ExecutionCheckpointRecord.observation_id
        ).where(ExecutionCheckpointRecord.item_execution_id == scope.item_execution_id,
                ExecutionCheckpointRecord.dispatch_generation == scope.dispatch_generation).with_for_update()).one_or_none()
        if checkpoint is None or tuple(checkpoint) != (scope.launch_id, scope.dataset_item_id, scope.attempt_id,
                                                       scope.trace_id, scope.root_observation_id):
            _fail("SCOPE_MISMATCH")


def _row_query():
    record = TrajectoryEvidenceArtifactRecord
    return select(record.id, record.launch_id, record.item_execution_id, record.attempt_id,
                  record.trajectory_contract_digest, record.trajectory_digest, record.schema_version, record.payload_json)


def _restore(row, ref, scope, contract, profile):
    if row is None:
        _fail("MISSING")
    if (row.id, row.trajectory_digest, row.trajectory_contract_digest, row.schema_version) != (
        ref.artifact_id, ref.trajectory_digest, ref.trajectory_contract_digest, ref.schema_version):
        _fail("REF_MISMATCH")
    if (row.launch_id, row.item_execution_id, row.attempt_id) != (scope.launch_id, scope.item_execution_id, scope.attempt_id):
        _fail("SCOPE_MISMATCH")
    try:
        value = StandardTrajectory.model_validate(canonical_loads(row.payload_json, max_bytes=contract.limits.max_artifact_bytes))
    except ValueError:
        _fail("CORRUPT")
    value, payload = _validate(value, contract, profile)
    if value.scope != scope:
        _fail("SCOPE_MISMATCH")
    if payload != row.payload_json:
        _fail("CORRUPT")
    if (value.provenance.trajectory_digest, value.provenance.trajectory_contract_digest, value.schema_version) != (
        row.trajectory_digest, row.trajectory_contract_digest, row.schema_version):
        _fail("REF_MISMATCH")
    return value


def persist_trajectory_evidence(session: Session, trajectory: StandardTrajectory, contract: TrajectoryContract) -> TrajectoryEvidenceRef:
    """校验冻结 DB 身份后 insert-only；重复命中读取校验，不更新旧 payload。"""
    checked, profile = _contract(contract)
    value, payload = _validate(trajectory, checked, profile)
    dialect = session.get_bind().dialect.name
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}.get(dialect)
    if insert is None:
        _fail("DIALECT_UNSUPPORTED")
    record = TrajectoryEvidenceArtifactRecord
    scope, provenance = value.scope, value.provenance
    with session.no_autoflush:
        try:
            _db_scope(session, scope, profile, writing=True)
            session.execute(insert(record).values(
                id="te_" + uuid4().hex, launch_id=scope.launch_id, item_execution_id=scope.item_execution_id,
                attempt_id=scope.attempt_id, trajectory_contract_digest=provenance.trajectory_contract_digest,
                trajectory_digest=provenance.trajectory_digest, schema_version=value.schema_version, payload_json=payload,
            ).on_conflict_do_nothing(index_elements=["item_execution_id", "attempt_id", "trajectory_contract_digest", "trajectory_digest"]))
            row = session.execute(_row_query().where(record.item_execution_id == scope.item_execution_id,
                record.attempt_id == scope.attempt_id, record.trajectory_contract_digest == provenance.trajectory_contract_digest,
                record.trajectory_digest == provenance.trajectory_digest)).one()
            ref = TrajectoryEvidenceRef(artifact_id=row.id, trajectory_digest=row.trajectory_digest,
                                       trajectory_contract_digest=row.trajectory_contract_digest, schema_version=row.schema_version)
            _restore(row, ref, scope, checked, profile)
        except SQLAlchemyError:
            _fail("STORAGE_UNAVAILABLE")  # 由调用方 rollback；不传播 SQL/参数或内部异常文本。
    return ref


def load_trajectory_evidence(
    session: Session, ref: TrajectoryEvidenceRef, *, scope: TrajectoryScope, contract: TrajectoryContract
) -> StandardTrajectory:
    """仅读取冻结制品；不依赖 checkpoint TTL，不读最新 Trace，不调用 Agent。"""
    checked, profile = _contract(contract)
    if type(ref) is not TrajectoryEvidenceRef or type(scope) is not TrajectoryScope:
        _fail("INVALID")
    try:
        ref = TrajectoryEvidenceRef.model_validate(ref.model_dump())
        scope = TrajectoryScope.model_validate(scope.model_dump())
    except ValueError:
        _fail("INVALID")
    if ref.trajectory_contract_digest != checked.digest or ref.schema_version != checked.schema_version:
        _fail("PROVENANCE_MISMATCH")
    with session.no_autoflush:
        try:
            _db_scope(session, scope, profile, writing=False)
            row = session.execute(_row_query().where(TrajectoryEvidenceArtifactRecord.id == ref.artifact_id)).one_or_none()
            return _restore(row, ref, scope, checked, profile)
        except SQLAlchemyError:
            _fail("STORAGE_UNAVAILABLE")
