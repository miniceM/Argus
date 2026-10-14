"""在受验证事件清单上证明逻辑调用相邻；序号本身不制造因果边。"""

from .arguments import json_kind
from .codec import canonical_dumps


def _identity(step, params):
    identity = []
    for path, tokens in zip(params.identity_paths, params.identity_tokens, strict=True):
        if step.argument_visibility.get(path) != "AVAILABLE":
            return None
        value = step.arguments_redacted
        for token in tokens:
            if type(token) is str and type(value) is dict and token in value:
                value = value[token]
            elif type(token) is int and type(value) is list and token < len(value):
                value = value[token]
            else:
                return None
        if json_kind(value) is None and not (type(value) is list and len(value) <= 1000
            and all(json_kind(member) is not None for member in value)):
            return None
        # 规范值仅在本次有界测量内比较，不进入 evidence、hash 或持久化出口。
        identity.append(canonical_dumps(value))
    return tuple(identity)


def _events(steps, workflow):
    inventories = [step.result_summary["terminal_inventory"] for step in steps
        if step.workflow_scope_id == workflow and step.phase == "EVENT" and type(step.result_summary) is dict
        and "terminal_inventory" in step.result_summary]
    if len(inventories) != 1 or type(inventories[0]) is not dict:
        return None
    inventory = inventories[0]
    events, declared = inventory.get("observed_events"), inventory.get("event_ids")
    count, final = inventory.get("event_count"), inventory.get("final_sequence")
    if (type(events) is not list or len(events) > 1000 or type(declared) is not list
        or type(count) is not int or type(final) is not int or final != count
        or not 1 <= count <= 1000 or len(declared) != count or len(events) > count
        or any(type(identity) is not str for identity in declared) or len(set(declared)) != count):
        return None
    nodes = {step.step_id: step for step in steps}
    result, event_ids = {}, set()
    for event in events:
        if type(event) is not dict or set(event) != {"step_id", "event_id", "sequence"}:
            return None
        identity, event_id, sequence = event["step_id"], event["event_id"], event["sequence"]
        if (type(identity) is not str or type(event_id) is not str or type(sequence) is not int
            or not 1 <= sequence <= count or sequence in result or event_id not in declared or event_id in event_ids
            or identity not in nodes or nodes[identity].workflow_scope_id != workflow):
            return None
        result[sequence] = nodes[identity]
        event_ids.add(event_id)
    if len({step.step_id for step in result.values()}) != len(result):
        return None
    return result, set(result) == set(range(1, count + 1)) and event_ids == set(declared)


def _adjacent(first, after, first_sequence, after_sequence, events, index):
    if first_sequence >= after_sequence:
        return False
    previous = first
    for sequence in range(first_sequence + 1, after_sequence + 1):
        current = events.get(sequence)
        if (current is None or previous.step_id not in current.causal_predecessor_ids
            or not index.before(previous, current)):
            return False
        previous = current
    return previous.step_id == after.step_id


def evaluate_repeats(steps, coverage, params, index):
    matching = [step for step in steps if step.type == "TOOL" and step.phase == params.phase
        and step.tool_identity == params.tool_identity]
    calls_complete = coverage["tool_requests" if params.phase == "REQUESTED" else "tool_executions"] == "COMPLETE"
    if not matching:
        return (True if calls_complete else None, "TOOL_REPEAT_LIMIT_SATISFIED" if calls_complete else "TOOL_REPEAT_UNPROVEN", [], 0)
    identities = {step.step_id: _identity(step, params) if coverage["tool_arguments"] != "NONE" else None for step in matching}
    unknown = coverage["ordering"] == "NONE" or any(step.workflow_scope_id is None or identities[step.step_id] is None
        or step.step_id not in index.ready for step in matching)
    maximum, proof, adjacency_unknown = 0, [], False
    # 单个可见调用也是长度 1 的连续序列；max_repeats 包含首个调用。
    for step in matching:
        if step.workflow_scope_id is not None and identities[step.step_id] is not None and maximum == 0:
            maximum, proof = 1, [step.step_id]
    workflows = sorted({step.workflow_scope_id for step in matching if step.workflow_scope_id is not None})
    for workflow in workflows:
        inventory = _events(steps, workflow)
        selected = [step for step in steps if step.type == "TOOL" and step.phase == params.phase and step.workflow_scope_id == workflow]
        if inventory is None:
            unknown = True
            continue
        events, complete = inventory
        if not {step.step_id for step in selected} <= {step.step_id for step in events.values()}:
            unknown = True
        if not complete:
            unknown = True
        previous, previous_sequence, run = None, None, []
        for sequence, step in sorted(events.items()):
            if step.type != "TOOL" or step.phase != params.phase:
                continue
            contiguous = previous is None or _adjacent(previous, step, previous_sequence, sequence, events, index)
            if not contiguous:
                adjacency_unknown = True
            key = identities.get(step.step_id)
            if step.tool_identity != params.tool_identity or key is None:
                run = []
            elif (contiguous and previous is not None and previous.tool_identity == params.tool_identity
                and identities.get(previous.step_id) == key and coverage["ordering"] != "NONE"):
                run.append(step.step_id)
            else:
                run = [step.step_id]
            if len(run) > maximum:
                maximum, proof = len(run), list(run)
            previous, previous_sequence = step, sequence
    if maximum > params.max_repeats:
        return False, "TOOL_REPEAT_LIMIT_EXCEEDED", proof, maximum
    # 完整清单中仅一个目标调用，其重复上界为 1；无需证明不同工具间的相邻关系。
    # 多个目标调用仍须相邻证明，身份、库存、scope 或 timing 不足仍为 UNKNOWN。
    if unknown or adjacency_unknown and len(matching) > 1 or not calls_complete or coverage["ordering"] != "COMPLETE":
        return None, "TOOL_REPEAT_UNPROVEN", [], maximum
    return True, "TOOL_REPEAT_LIMIT_SATISFIED", proof, maximum
