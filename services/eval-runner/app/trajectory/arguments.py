"""精确 JSON 参数比较，只读取已脱敏的获准叶值，不输出参数或指纹。"""

from decimal import Decimal

from .codec import canonical_loads


def json_kind(value):
    if type(value) in (int, Decimal):
        return "number"
    return {str: "string", bool: "boolean", type(None): "null"}.get(type(value))


def compare(actual, expected, operator):
    if operator == "one_of":
        return any(compare(actual, candidate, "eq") for candidate in expected)
    if operator == "contains" and type(actual) is list:
        return any(compare(candidate, expected, "eq") for candidate in actual)
    if json_kind(actual) is None or json_kind(actual) != json_kind(expected):
        return False
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "contains":
        return type(actual) is str and expected in actual
    if json_kind(actual) != "number":
        return False
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    if operator == "lt":
        return actual < expected
    return actual <= expected


def evaluate_arguments(steps, coverage, params):
    matching = [step for step in steps if step.type == "TOOL" and step.phase == params.phase and step.tool_identity == params.tool_identity]
    calls_complete = coverage["tool_requests" if params.phase == "REQUESTED" else "tool_executions"] == "COMPLETE"
    if not matching:
        return (not params.require_call, "ARGUMENT_CALL_ABSENT", []) if calls_complete else (None, "ARGUMENT_COVERAGE_INCOMPLETE", [])
    expected = canonical_loads(params.expected_json)
    violations, unknowns, proofs = [], [], []
    for step in matching:
        state = step.argument_visibility.get(params.path)
        if coverage["tool_arguments"] == "NONE" or state not in {"AVAILABLE", "MISSING"}:
            unknowns.append(step.step_id)
        elif state == "MISSING":
            violations.append(step.step_id)
        else:
            actual = step.arguments_redacted
            for token in params.path_tokens:
                if (type(token) is str and type(actual) is dict and token in actual
                    or type(token) is int and type(actual) is list and token < len(actual)):
                    actual = actual[token]
                else:
                    unknowns.append(step.step_id)
                    break
            else:
                (proofs if compare(actual, expected, params.operator) else violations).append(step.step_id)
    if violations:
        return False, "ARGUMENT_CONSTRAINT_VIOLATED", violations
    if unknowns or not calls_complete:
        return None, "ARGUMENT_COVERAGE_INCOMPLETE", unknowns
    return True, "ARGUMENT_CONSTRAINT_SATISFIED", proofs
