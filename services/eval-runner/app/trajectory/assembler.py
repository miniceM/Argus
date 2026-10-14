"""只读冻结轨迹组装；能力/观测完整度与质量判定分离。"""

import logging
from collections import defaultdict
from copy import deepcopy
from time import perf_counter
from typing import get_args

from .causal import _opaque, validate_causal_links
from .codec import canonical_digest, canonical_dumps
from .contract import TrajectoryContract
from .normalizer import NormalizedObservations
from .schema import Capability, StandardTrajectory, TrajectoryScope, TrajectoryStep

logger = logging.getLogger(__name__)
_RANK = {"NONE": 0, "PARTIAL": 1, "COMPLETE": 2}


def trajectory_digest(trajectory: StandardTrajectory) -> str:
    """校验类型后计算完整获准语义身份，排除摘要自身。"""
    data = StandardTrajectory.model_validate(trajectory.model_dump()).model_dump()
    del data["provenance"]["trajectory_digest"]
    return canonical_digest(data)


def _terminal(observation):
    return observation.step.phase == "EVENT" and (
        observation.terminal_event_count is not None or "TERMINAL_INVENTORY_INVALID" in observation.warnings)


def _inventory(observations, warnings):
    groups = defaultdict(list)
    valid = True
    for observation in observations:
        step = observation.step
        if not _opaque(step.workflow_scope_id) or not _opaque(observation.event_id) or observation.sequence is None:
            warnings.add("WORKFLOW_IDENTITY_UNAVAILABLE")
            valid = False
        else:
            groups[step.workflow_scope_id].append(observation)
    if not groups:
        warnings.add("TERMINAL_INVENTORY_MISSING")
        return False
    for members in groups.values():
        terminals = [member for member in members if _terminal(member)]
        if len(terminals) != 1:
            warnings.add("TERMINAL_INVENTORY_CONFLICT" if terminals else "TERMINAL_INVENTORY_MISSING")
            valid = False
            continue
        terminal = terminals[0]
        ids = [member.event_id for member in members]
        sequences = [member.sequence for member in members]
        declared = terminal.terminal_event_ids
        if declared is None:
            warnings.add("TERMINAL_INVENTORY_INVALID")
            valid = False
        elif len(ids) != len(set(ids)) or len(sequences) != len(set(sequences)) or len(declared) != len(set(declared)):
            warnings.add("TERMINAL_INVENTORY_CONFLICT")
            valid = False
        elif (set(ids) != set(declared) or terminal.terminal_event_count != len(members)
              or terminal.terminal_final_sequence != len(members)
              or set(sequences) != set(range(1, len(members) + 1))
              or terminal.sequence != terminal.terminal_final_sequence):
            warnings.add("TERMINAL_INVENTORY_INCOMPLETE")
            valid = False
    return valid


def _arguments(causal, candidates, profile, warnings):
    """只将已验证同调用请求参数投影到未采集的 action，保留其固有不确定性。"""
    steps = {identity: observation.step.model_dump() for identity, observation in candidates.items()}
    coverage = {identity: observation.capabilities.get("tool_arguments", "NONE")
                for identity, observation in candidates.items()}
    for action, request in causal.request_by_execution_id.items():
        target, source = steps[action], steps[request]
        if target["arguments_redacted"] is None and not target["argument_visibility"] and (
            "ARGUMENTS_NOT_COLLECTED" in candidates[action].warnings
        ):
            target["arguments_redacted"] = deepcopy(source["arguments_redacted"])
            target["argument_visibility"] = dict(source["argument_visibility"])
            coverage[action] = min((coverage[request], profile.capabilities["tool_arguments"]), key=_RANK.get)
        elif target["argument_visibility"] and source["argument_visibility"] and (
            all(state in {"AVAILABLE", "MISSING"} for state in target["argument_visibility"].values())
            and all(state in {"AVAILABLE", "MISSING"} for state in source["argument_visibility"].values())
            and (target["argument_visibility"] != source["argument_visibility"]
                 or canonical_dumps(target["arguments_redacted"]) != canonical_dumps(source["arguments_redacted"]))
        ):
            warnings.add("ARGUMENTS_PAIR_CONFLICT")
            coverage[action] = "PARTIAL" if coverage[action] != "NONE" else "NONE"
    return steps, coverage


def _capabilities(profile, business, arguments, *, closed, invalid_ordering, clock_skew):
    result = dict(profile.capabilities)
    for capability in get_args(Capability):
        if result[capability] == "NONE":
            continue
        relevant = []
        for observation in business:
            step = observation.step
            if (capability == "tool_requests" and step.type == "TOOL" and step.phase == "REQUESTED"
                or capability in {"tool_executions", "timing"} and step.type == "TOOL" and step.phase == "EXECUTED"
                or capability == "tool_arguments" and step.type == "TOOL" and step.phase in {"REQUESTED", "EXECUTED", "BLOCKED"}
                or capability == "human_events" and step.type == "HUMAN"
                or capability == "ordering" and not _terminal(observation)):
                relevant.append(arguments[step.step_id] if capability == "tool_arguments"
                                else observation.capabilities.get(capability, "NONE"))
        if relevant:
            observed = "NONE" if all(value == "NONE" for value in relevant) else (
                "COMPLETE" if all(value == "COMPLETE" for value in relevant) else "PARTIAL")
            result[capability] = min((result[capability], observed), key=_RANK.get)
        if not closed or capability == "ordering" and (invalid_ordering or clock_skew):
            if result[capability] == "COMPLETE":
                result[capability] = "PARTIAL"
    return result


def assemble_trajectory(
    scope: TrajectoryScope, observations: NormalizedObservations, contract: TrajectoryContract
) -> StandardTrajectory:
    """真实 Root 图、显式因果与完成协议形成最小脱敏轨迹，不运行规则。"""
    began = perf_counter()
    causal = validate_causal_links(scope, observations, contract)
    graph = causal.root_graph
    batch = NormalizedObservations.model_validate(observations.model_dump())
    profile, limits = contract.source_profile, contract.limits
    candidates = {record.observation_id: record.observation for record in graph.records if record.observation.step is not None}
    business = [observation for identity, observation in candidates.items()
                if identity != scope.root_observation_id and not observation.structural]
    warnings = set(graph.warnings)
    for reasons in causal.warnings_by_id.values():
        warnings.update(reasons)
    inventory = _inventory(business, warnings) if profile.completion_protocol == "workflow-inventory-v1" else False
    proof = batch.convergence
    converged = proof is not None
    if proof is None:
        warnings.add("ACQUISITION_CONVERGENCE_UNPROVEN")
    else:
        if not proof.paging_complete:
            warnings.add("ACQUISITION_PAGING_INCOMPLETE")
            converged = False
        if proof.quiet_window_seconds < limits.quiet_window_seconds:
            warnings.add("ACQUISITION_QUIET_UNPROVEN")
            converged = False
    structural_gap = any(reason.startswith(("GRAPH_", "NORMALIZED_", "SOURCE_BATCH_")) and reason != "GRAPH_CLOCK_SKEW"
                         for reason in graph.warnings) or any(record.observation.step is None for record in graph.records)
    closed = inventory and converged and not graph.truncated and batch.source_bytes_complete and not structural_gap
    step_data, arguments = _arguments(causal, candidates, profile, warnings)
    for observation in business:
        if _terminal(observation) and observation.terminal_event_ids is not None:
            events = [{"step_id": member.step.step_id, "event_id": member.event_id, "sequence": member.sequence}
                      for member in business if member.step.workflow_scope_id == observation.step.workflow_scope_id
                      and _opaque(member.event_id) and member.sequence is not None]
            step_data[observation.step.step_id]["result_summary"]["terminal_inventory"]["observed_events"] = sorted(
                events, key=lambda event: (event["sequence"], event["step_id"]))
    caps = _capabilities(profile, business, arguments, closed=closed,
                         invalid_ordering=bool(causal.invalid_ordering_ids), clock_skew="GRAPH_CLOCK_SKEW" in warnings)
    if profile.capabilities["human_events"] != "NONE":
        from .human import closed_human_workflows
        _, human_closed, invalid_human = closed_human_workflows([observation.step for observation in candidates.values()], scope,
            profile.human_authority)
        workflows = {observation.step.workflow_scope_id for observation in business}
        if not workflows or None in workflows or not workflows <= human_closed or invalid_human:
            if caps["human_events"] == "COMPLETE":
                caps["human_events"] = "PARTIAL"
            warnings.add("HUMAN_INVENTORY_MISMATCH" if human_closed or invalid_human or any(
                type(observation.step.result_summary) is dict and "human_receipt" in observation.step.result_summary
                and observation.step.raw_name == "human.inventory" for observation in candidates.values()) else "HUMAN_INVENTORY_UNAVAILABLE")
    required_complete = all(caps[key] == "COMPLETE" for key in contract.required_capabilities)
    if not required_complete:
        warnings.add("TRAJECTORY_REQUIRED_CAPABILITY_UNAVAILABLE")
    root = candidates[scope.root_observation_id].step
    ordered = [record.observation_id for record in graph.records if record.observation.step is not None]
    provenance = {
        "provider": profile.provider, "source_profile_id": profile.profile_id, "source_profile_version": profile.version,
        "schema_version": contract.schema_version, "assembler_version": contract.assembler_version,
        "tool_mapping_version": contract.tool_mapping.version, "tool_mapping_digest": contract.tool_mapping.digest,
        "redaction_policy_version": contract.redaction_policy.version, "redaction_policy_digest": contract.redaction_policy.digest,
        "trajectory_contract_digest": graph.trajectory_contract_digest, "trajectory_digest": "sha256:" + "0" * 64,
    }

    def materialize(retained, artifact_limited):
        reasons = set(warnings)
        local_caps = dict(caps)
        if artifact_limited:
            reasons.add("TRAJECTORY_ARTIFACT_LIMIT")
            local_caps = {key: "PARTIAL" if value == "COMPLETE" else value for key, value in local_caps.items()}
        ids = set(retained)
        steps = []
        for index, identity in enumerate(retained):
            data = deepcopy(step_data[identity])
            data["index"] = index
            summary = data["result_summary"]
            if _terminal(candidates[identity]) and type(summary) is dict and "terminal_inventory" in summary:
                inventory = summary["terminal_inventory"]
                if "observed_events" in inventory:
                    inventory["observed_events"] = [event for event in inventory["observed_events"] if event["step_id"] in ids]
            references = causal.predecessors_by_id[identity]
            data["causal_predecessor_ids"] = [ref for ref in references if ref in ids]
            if len(data["causal_predecessor_ids"]) != len(references):
                reasons.add("TRAJECTORY_CAUSAL_REFERENCE_TRUNCATED")
                if local_caps["ordering"] == "COMPLETE":
                    local_caps["ordering"] = "PARTIAL"
            if identity != scope.root_observation_id and data["parent_step_id"] not in ids:
                data["parent_step_id"] = None
                reasons.add("TRAJECTORY_PARENT_UNAVAILABLE")
                local_caps = {key: "PARTIAL" if value == "COMPLETE" else value for key, value in local_caps.items()}
            steps.append(TrajectoryStep.model_validate(data))
        root_display = len(steps) == 1
        root_only = root_display and len(graph.records) == 1 and not (
            artifact_limited or graph.truncated or structural_gap)
        if root_display:
            local_caps = {key: "NONE" for key in local_caps}
        complete = closed and required_complete and not artifact_limited and not root_display and all(
            local_caps[key] == "COMPLETE" for key in contract.required_capabilities)
        return StandardTrajectory(
            schema_version=contract.schema_version, scope=graph.scope,
            input_redacted=root.arguments_redacted, output_redacted=root.result_summary,
            completeness={"status": "ROOT_ONLY" if root_only else "OBSERVED_COMPLETE" if complete else "PARTIAL",
                          "capabilities": local_caps, "warnings": sorted(reasons)},
            steps=steps, provenance=provenance,
        )

    def fits(result):
        try:
            canonical_dumps(result, max_bytes=limits.max_artifact_bytes)
            return True
        except ValueError as error:
            if str(error) != "TRAJECTORY_JSON_BYTE_LIMIT":
                raise
            return False

    result = materialize(ordered, False)
    if not fits(result):
        children = [identity for identity in ordered if identity != scope.root_observation_id]

        def prefix(count):
            ids = set(children[:count]) | {scope.root_observation_id}
            return materialize([identity for identity in ordered if identity in ids], True)

        result = prefix(0)
        if not fits(result):
            raise ValueError("TRAJECTORY_ARTIFACT_ROOT_LIMIT")
        low, high = 0, len(children)
        while low + 1 < high:
            mid = (low + high) // 2
            candidate = prefix(mid)
            if fits(candidate):
                low, result = mid, candidate
            else:
                high = mid
    data = result.model_dump()
    data["provenance"]["trajectory_digest"] = trajectory_digest(result)
    result = StandardTrajectory.model_validate(data)
    logger.debug("trajectory assembled_steps=%d deduped=%d warnings=%d assemble_latency_ms=%.3f",
                 len(result.steps), graph.deduped, len(result.completeness.warnings), (perf_counter() - began) * 1000)
    return result
