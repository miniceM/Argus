"""冻结字段中的显式因果校验；不派生全序、不计算完整度或质量结论。"""

import re
from collections import defaultdict
from typing import Annotated

from pydantic import Field

from .contract import TrajectoryContract
from .graph import RootObservationGraph, assemble_root_graph
from .normalizer import NormalizedObservations
from .schema import Identity, ReasonCode, TrajectoryScope, _ContractModel


class CausalObservationGraph(_ContractModel):
    root_graph: RootObservationGraph
    predecessors_by_id: dict[Identity, Annotated[list[Identity], Field(max_length=1000)]]
    request_by_execution_id: dict[Identity, Identity]
    warnings_by_id: dict[Identity, Annotated[list[ReasonCode], Field(max_length=1000)]]
    invalid_ordering_ids: list[Identity] = Field(max_length=1000)


def _opaque(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _cycle_nodes(edges):
    """迭代 Kosaraju：拒绝完整原始环，不选时间/序号一致的半边当真。"""
    visited, postorder = set(), []
    for start in sorted(edges):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(edges[start])))]
        while stack:
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                postorder.append(current)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(sorted(edges[child]))))
    reverse = {identity: set() for identity in edges}
    for current, predecessors in edges.items():
        for predecessor in predecessors:
            reverse[predecessor].add(current)
    visited, cyclic = set(), set()
    for start in reversed(postorder):
        if start in visited:
            continue
        visited.add(start)
        component, stack = {start}, [start]
        while stack:
            for child in reverse[stack.pop()]:
                if child not in visited:
                    visited.add(child)
                    component.add(child)
                    stack.append(child)
        if len(component) > 1 or start in edges[start]:
            cyclic.update(component)
    return cyclic


def _time_reason(predecessor, current):
    before, after = predecessor.step, current.step
    if any(time is None for time in (before.started_at, before.ended_at, after.started_at, after.ended_at)):
        return "CAUSAL_TIME_UNAVAILABLE"
    if before.ended_at < before.started_at or after.ended_at < after.started_at or before.ended_at > after.started_at:
        return "CAUSAL_TIME_CONFLICT"
    return None


def validate_causal_links(
    scope: TrajectoryScope, observations: NormalizedObservations, contract: TrajectoryContract
) -> CausalObservationGraph:
    """将同 Root/workflow 的可信 event 引用映射成 observation/step ID。

    只保留独立验证的显式关系；源序号用于校验，不生成隐式边。返回配对
    仅证明同一已观测调用，参数仍由后续投影按来源 visibility 决定。
    """
    graph = assemble_root_graph(scope, observations, contract)
    profile = contract.source_profile
    candidates = {record.observation_id: record.observation for record in graph.records
                  if record.observation.step is not None}
    predecessors = {identity: set() for identity in candidates}
    warnings, invalid = defaultdict(set), set()

    def warn(identity, reason):
        warnings[identity].add(reason)
        invalid.add(identity)

    eligible, indexed, events, sequences = {}, {}, defaultdict(list), defaultdict(list)
    for identity, observation in candidates.items():
        step = observation.step
        if identity == scope.root_observation_id or observation.structural:
            continue
        if step.phase == "EVENT" and (
            observation.terminal_event_count is not None or "TERMINAL_INVENTORY_INVALID" in observation.warnings
        ):
            if (_opaque(step.workflow_scope_id) and _opaque(observation.event_id)
                    and observation.sequence is not None and observation.sequence <= contract.limits.max_steps):
                events[(step.workflow_scope_id, observation.event_id)].append(identity)
                sequences[(step.workflow_scope_id, observation.sequence)].append(identity)
            continue  # 已确认的终结计数由 inventory 阶段核对，不当作工具因果节点。
        if (not _opaque(step.workflow_scope_id) or not _opaque(observation.event_id)
                or observation.sequence is None or observation.sequence > contract.limits.max_steps):
            warn(identity, "CAUSAL_EVENT_UNAVAILABLE")
            continue
        if step.type == "HUMAN" and (profile.capabilities["human_events"] == "NONE" or profile.human_authority_id is None):
            warn(identity, "CAUSAL_HUMAN_UNAVAILABLE")
            continue
        indexed[identity] = observation
        events[(step.workflow_scope_id, observation.event_id)].append(identity)
        sequences[(step.workflow_scope_id, observation.sequence)].append(identity)
        if (profile.capabilities["ordering"] == "NONE"
                or observation.capabilities.get("ordering") not in {"COMPLETE", "PARTIAL"}):
            warn(identity, "CAUSAL_SOURCE_UNAVAILABLE")
            continue
        if any(not _opaque(event) for event in observation.causal_event_ids):
            warn(identity, "CAUSAL_EVENT_UNAVAILABLE")
            continue
        eligible[identity] = observation
    conflicting_events, conflicting_sequences = set(), set()
    for members in events.values():
        if len(members) > 1:
            conflicting_events.update(members)
    for members in sequences.values():
        if len(members) > 1:
            conflicting_sequences.update(members)
    for identity in conflicting_events:
        warn(identity, "CAUSAL_EVENT_CONFLICT")
    for identity in conflicting_sequences:
        warn(identity, "CAUSAL_SEQUENCE_CONFLICT")

    raw_edges = {identity: set() for identity in eligible}
    for identity, observation in eligible.items():
        for event in sorted(set(observation.causal_event_ids)):
            members = events.get((observation.step.workflow_scope_id, event), [])
            if not members:
                warn(identity, "CAUSAL_REFERENCE_UNAVAILABLE")
            elif len(members) != 1:
                warn(identity, "CAUSAL_EVENT_CONFLICT")
            elif members[0] not in eligible:
                warn(identity, "CAUSAL_REFERENCE_UNAVAILABLE")
            else:
                raw_edges[identity].add(members[0])
    cyclic = _cycle_nodes(raw_edges)
    for identity in cyclic:
        warn(identity, "CAUSAL_CYCLE")
    for identity, references in raw_edges.items():
        for predecessor in references:
            if identity in cyclic or predecessor in cyclic:
                warn(identity, "CAUSAL_CYCLE")
                continue
            if identity in conflicting_events or predecessor in conflicting_events:
                warn(identity, "CAUSAL_EVENT_CONFLICT")
                continue
            before, after = eligible[predecessor], eligible[identity]
            if (identity in conflicting_sequences or predecessor in conflicting_sequences
                    or before.sequence >= after.sequence):
                warn(identity, "CAUSAL_SEQUENCE_CONFLICT")
                continue
            reason = _time_reason(before, after)
            if reason is not None:
                warn(identity, reason)
                continue
            predecessors[identity].add(predecessor)

    calls = defaultdict(lambda: {"REQUESTED": [], "ACTION": []})
    for identity, observation in indexed.items():
        step = observation.step
        if step.type != "TOOL" or step.phase not in {"REQUESTED", "EXECUTED", "BLOCKED"}:
            continue
        if not _opaque(observation.call_id):
            warn(identity, "CAUSAL_CALL_PAIR_UNAVAILABLE")
            continue
        role = "REQUESTED" if step.phase == "REQUESTED" else "ACTION"
        calls[(step.workflow_scope_id, observation.call_id)][role].append(identity)
    pairs = {}
    for roles in calls.values():
        requests, actions = roles["REQUESTED"], roles["ACTION"]
        for action in actions:
            if action not in eligible:
                continue
            if len(requests) != 1 or len(actions) != 1:
                warn(action, "CAUSAL_CALL_CONFLICT" if len(requests) > 1 or len(actions) > 1
                     else "CAUSAL_CALL_PAIR_UNAVAILABLE")
                continue
            request = requests[0]
            request_step, action_step = indexed[request].step, eligible[action].step
            if (request not in predecessors[action] or request_step.tool_identity is None
                    or request_step.tool_identity != action_step.tool_identity):
                warn(action, "CAUSAL_CALL_PAIR_UNAVAILABLE")
                continue
            pairs[action] = request
    return CausalObservationGraph(
        root_graph=graph,
        predecessors_by_id={identity: sorted(predecessors[identity]) for identity in sorted(predecessors)},
        request_by_execution_id=dict(sorted(pairs.items())),
        warnings_by_id={identity: sorted(warnings[identity]) for identity in sorted(warnings)},
        invalid_ordering_ids=sorted(invalid),
    )
