"""Assembler 的冻结 Root 拓扑阶段；不证明因果、来源收敛或完整度。"""

import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, get_args

from pydantic import Field

from .codec import canonical_dumps
from .contract import TrajectoryContract
from .normalizer import NormalizedObservation, NormalizedObservations, NormalizedRecord
from .schema import Capability, Digest, Identity, ReasonCode, TrajectoryScope, _ContractModel


class RootObservationGraph(_ContractModel):
    scope: TrajectoryScope
    trajectory_contract_digest: Digest
    source_profile_id: Identity
    source_profile_version: Identity
    records: list[NormalizedRecord] = Field(max_length=10000)
    depth_by_id: dict[Identity, Annotated[int, Field(ge=0, le=10000)]]
    warnings: list[ReasonCode] = Field(max_length=1000)
    deduped: int = Field(ge=0, le=10000)
    truncated: bool


@dataclass
class _Node:
    record: NormalizedRecord
    variants: list[NormalizedRecord]
    conflict: bool
    lineage_known: bool


def _trace_known(value):
    return value is not None and re.fullmatch(r"[0-9a-f]{32}", value) is not None and value != "0" * 32


def _comparison_key(record):
    value = record.model_dump()
    observation = value["observation"]
    observation["warnings"] = sorted(set(observation["warnings"]))
    observation["causal_event_ids"] = sorted(set(observation["causal_event_ids"]))
    if observation["terminal_event_ids"] is not None:
        observation["terminal_event_ids"] = sorted(observation["terminal_event_ids"])
    if observation["step"] is not None:
        observation["step"]["index"] = 0  # 临时展示位置不是观测内容。
    return canonical_dumps(value, max_bytes=8 * 1024 * 1024)


def _rejected(record, reason):
    return NormalizedRecord(
        observation_id=record.observation_id,
        trace_id=record.trace_id,
        parent_step_id=record.parent_step_id,
        parent_known=record.parent_known,
        observation=NormalizedObservation(
            step=None, capabilities={key: "NONE" for key in get_args(Capability)}, warnings=[reason]
        ),
    )


def _nodes(records):
    grouped, anonymous = {}, []
    for record in records:
        if record.observation_id is None:
            anonymous.append(record)
        else:
            grouped.setdefault(record.observation_id, {}).setdefault(_comparison_key(record), []).append(record)
    nodes, deduped = {}, 0
    for identity, variants in grouped.items():
        representatives = [variants[key][0] for key in sorted(variants)]
        deduped += sum(len(duplicates) - 1 for duplicates in variants.values())
        first = representatives[0]
        conflict = len(representatives) > 1
        lineage = {(record.trace_id, record.parent_known, record.parent_step_id) for record in representatives}
        nodes[identity] = _Node(
            _rejected(first, "GRAPH_ID_CONFLICT") if conflict else first,
            representatives,
            conflict,
            len(lineage) == 1 and first.parent_known,
        )
    return nodes, anonymous, deduped


def _root_anchors(nodes, scope):
    root = nodes.get(scope.root_observation_id)
    if root is None:
        raise ValueError("TRAJECTORY_ROOT_UNAVAILABLE")
    if root.conflict:
        raise ValueError("TRAJECTORY_ROOT_CONFLICT")
    if root.record.observation.step is None or root.record.trace_id != scope.trace_id or not root.lineage_known:
        raise ValueError("TRAJECTORY_ROOT_UNAVAILABLE")
    anchors, seen = set(), {scope.root_observation_id}
    parent = root.record.parent_step_id
    while parent is not None:
        if parent == scope.root_observation_id:
            raise ValueError("TRAJECTORY_ROOT_PARENT_CYCLE")
        if parent in seen:
            break  # Root 之外的父链不能改变指定 Root 的闭包。
        seen.add(parent)
        anchors.add(parent)
        node = nodes.get(parent)
        if node is None or not node.lineage_known or node.record.trace_id != scope.trace_id:
            break
        parent = node.record.parent_step_id
    return anchors


def _outside_closure(nodes, root, anchors):
    # 多个 parent 变体全部落在外部锚点/树时，才能隔离冲突诊断；不选赢家。
    needs, dependents, outside = {}, {}, set()
    for identity, node in nodes.items():
        if identity == root or any(not record.parent_known for record in node.variants):
            continue
        parents = {record.parent_step_id for record in node.variants} - anchors - {None}
        needs[identity] = parents
        if not parents:
            outside.add(identity)
        for parent in parents:
            dependents.setdefault(parent, set()).add(identity)
    ready = deque(outside)
    while ready:
        parent = ready.popleft()
        for child in dependents.get(parent, ()):
            needs[child].discard(parent)
            if not needs[child] and child not in outside:
                outside.add(child)
                ready.append(child)
    return outside


def _closure(nodes, scope, anchors, warnings):
    root = scope.root_observation_id
    status = {identity: "OUT" for identity in _outside_closure(nodes, root, anchors)}
    status[root] = "IN"
    depths = {root: 0}
    # 使用迭代父链，最大分页预算下不依赖 Python 递归深度。
    for identity in sorted(nodes):
        if identity in status:
            continue
        path, positions = [], {}
        current = identity
        while current not in status:
            if current in positions:
                warnings.add("GRAPH_PARENT_CYCLE")
                status[current] = "UNKNOWN"
                break
            positions[current] = len(path)
            path.append(current)
            node = nodes[current]
            record = node.record
            if node.conflict and not node.lineage_known:
                warnings.add("GRAPH_ID_CONFLICT")
                status[current] = "UNKNOWN"
                break
            if not _trace_known(record.trace_id):
                outside = node.lineage_known and (record.parent_step_id is None or record.parent_step_id in anchors)
                status[current] = "OUT" if outside else "UNKNOWN"
                if not outside:
                    warnings.add("GRAPH_TRACE_SCOPE_UNAVAILABLE")
                break
            if record.trace_id != scope.trace_id:
                status[current] = "OUT"
                break
            if not node.lineage_known:
                warnings.add("GRAPH_PARENT_UNAVAILABLE")
                status[current] = "UNKNOWN"
                break
            parent = record.parent_step_id
            if parent is None or parent in anchors:
                status[current] = "OUT"
                break
            if parent not in nodes:
                warnings.add("GRAPH_PARENT_MISSING")
                status[current] = "UNKNOWN"
                break
            if nodes[parent].record.trace_id != scope.trace_id:
                warnings.add("GRAPH_TRACE_SCOPE_MISMATCH")
                status[current] = "UNKNOWN"
                break
            current = parent
        for child in reversed(path):
            if child in status:
                continue
            parent = nodes[child].record.parent_step_id
            status[child] = status[parent]
            if status[child] == "IN":
                depths[child] = depths[parent] + 1
    # 外部 trace 不进入图；若声称直接/间接指向当前闭包，保留范围矛盾诊断。
    for node in nodes.values():
        if node.record.trace_id != scope.trace_id and node.record.parent_step_id in depths:
            warnings.add("GRAPH_TRACE_SCOPE_MISMATCH" if _trace_known(node.record.trace_id)
                         else "GRAPH_TRACE_SCOPE_UNAVAILABLE")
    return depths, status


def _sort_key(record, depths):
    step = record.observation.step
    started = step.started_at if step is not None else None
    return (started is None, started or datetime.max.replace(tzinfo=UTC),
            depths[record.observation_id], record.observation_id)


def assemble_root_graph(
    scope: TrajectoryScope, observations: NormalizedObservations, contract: TrajectoryContract
) -> RootObservationGraph:
    """仅输出可证明位于指定 Root 闭包的记录；异常组件不会抹去独立证据。

    拒绝行及内容冲突只在无争议父链可归属时保留安全身份。未知组件降低
    后续完整性，确定在兄弟树的行诊断隔离。此中间图不发布 StandardTrajectory。
    """
    scope = TrajectoryScope.model_validate(scope.model_dump())
    batch = NormalizedObservations.model_validate(observations.model_dump())
    checked = TrajectoryContract.model_validate(contract.model_dump())
    profile = contract.source_profile
    if batch.scope != scope:
        raise ValueError("TRAJECTORY_GRAPH_SCOPE_MISMATCH")
    if batch.trajectory_contract_digest != checked.digest:
        raise ValueError("TRAJECTORY_GRAPH_CONTRACT_MISMATCH")
    if (batch.source_profile_id, batch.source_profile_version) != (profile.profile_id, profile.version):
        raise ValueError("TRAJECTORY_GRAPH_SOURCE_MISMATCH")
    limits = checked.limits
    if len(batch.records) > limits.page_size * limits.max_pages or batch.source_bytes > limits.max_source_bytes:
        raise ValueError("TRAJECTORY_GRAPH_SOURCE_LIMIT")
    nodes, anonymous, deduped = _nodes(batch.records)
    warnings = set(batch.warnings)
    if not batch.source_bytes_complete:
        warnings.add("GRAPH_SOURCE_BYTES_INCOMPLETE")
    anchors = _root_anchors(nodes, scope)
    depths, status = _closure(nodes, scope, anchors, warnings)
    records = [nodes[identity].record for identity in depths]
    for record in anonymous:
        if _trace_known(record.trace_id) and record.trace_id != scope.trace_id:
            continue
        parent = record.parent_step_id
        if record.parent_known and (parent is None or parent in anchors or status.get(parent) == "OUT"):
            continue
        warnings.add("GRAPH_IDENTITY_UNAVAILABLE")
    for record in records:
        warnings.update(record.observation.warnings)
        step = record.observation.step
        parent = nodes.get(record.parent_step_id)
        parent_step = parent.record.observation.step if parent is not None else None
        if step is not None and parent_step is not None and record.observation_id != scope.root_observation_id:
            if (step.started_at is not None and parent_step.started_at is not None
                    and step.started_at < parent_step.started_at) or (
                step.ended_at is not None and parent_step.ended_at is not None and step.ended_at > parent_step.ended_at
            ):
                warnings.add("GRAPH_CLOCK_SKEW")
    records.sort(key=lambda record: _sort_key(record, depths))
    candidates = [record for record in records if record.observation.step is not None]
    retained = {scope.root_observation_id}
    for record in candidates:
        if len(retained) >= limits.max_steps:
            break
        retained.add(record.observation_id)
    # 不借到达顺序丢失 Root；只在去重与闭包完成后消费 Step 预算。
    truncated = batch.truncated or len(candidates) > limits.max_steps
    if len(candidates) > limits.max_steps:
        warnings.add("GRAPH_STEP_LIMIT")
        records = [record if record.observation.step is None or record.observation_id in retained
                   else _rejected(record, "GRAPH_STEP_LIMIT") for record in records]
    return RootObservationGraph(
        scope=scope, trajectory_contract_digest=checked.digest,
        source_profile_id=profile.profile_id, source_profile_version=profile.version,
        records=records, depth_by_id=depths, warnings=sorted(warnings), deduped=deduped, truncated=truncated,
    )
