from __future__ import annotations

from enum import StrEnum
from typing import Any


class LaunchStatus(StrEnum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILED = "PARTIAL_FAILED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"


class ItemStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    RETRY_WAIT = "retry_wait"
    CANCELLED = "cancelled"


# Valid transitions for Launch
LAUNCH_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"QUEUED", "FAILED", "CANCELLING", "CANCELLED"},
    "QUEUED": {"RUNNING", "CANCELLING", "CANCELLED", "FAILED"},
    "RUNNING": {"COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLING"},
    "CANCELLING": {"CANCELLED", "PARTIAL_FAILED", "FAILED"},
    "CANCELLED": {"QUEUED"},  # via Resume
    "PARTIAL_FAILED": {"QUEUED"},  # via Retry Failed
    "FAILED": {"QUEUED"},  # via Resume / Retry Failed
    "COMPLETED": {"QUEUED"},  # via Retry Failed if partial failed items exist
}

TERMINAL_LAUNCH_STATUSES: tuple[str, ...] = (
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "PARTIAL_FAILED",
)

# Valid transitions for ItemExecution
ITEM_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"QUEUED", "CANCELLED", "FAILED"},
    "QUEUED": {"RUNNING", "CANCELLED"},
    "RUNNING": {"SUCCEEDED", "RETRY_WAIT", "FAILED", "TIMED_OUT", "CANCELLED"},
    "RETRY_WAIT": {"QUEUED", "CANCELLED"},
    "SUCCEEDED": set(),  # Terminal: Succeeded items are never re-executed
    "FAILED": {"QUEUED"},  # via Retry Failed
    "TIMED_OUT": {"QUEUED"},  # via Retry Failed
    "CANCELLED": {"QUEUED"},  # via Resume
}


def transition_launch_status(current: str, target: str) -> str:
    curr_norm = current.upper()
    target_norm = target.upper()
    if curr_norm == target_norm:
        return target_norm

    allowed = LAUNCH_TRANSITIONS.get(curr_norm, set())
    if target_norm not in allowed:
        raise ValueError(
            f"Invalid Launch status transition from '{curr_norm}' to '{target_norm}'. Allowed: {sorted(allowed)}"
        )
    return target_norm


def transition_item_status(current: str, target: str) -> str:
    curr_norm = current.lower()
    target_norm = target.lower()
    if curr_norm == target_norm:
        return target_norm.upper()

    allowed = {s.lower() for s in ITEM_TRANSITIONS.get(curr_norm.upper(), set())}
    if not allowed and curr_norm in [s.lower() for s in ITEM_TRANSITIONS]:
        allowed = {s.lower() for s in ITEM_TRANSITIONS[curr_norm.upper()]}
    # Also check lower-case key
    for k, v in ITEM_TRANSITIONS.items():
        if k.lower() == curr_norm:
            allowed = {x.lower() for x in v}
            break

    if target_norm not in allowed:
        raise ValueError(
            f"Invalid Item status transition from '{curr_norm}' to '{target_norm}'. Allowed: {sorted(allowed)}"
        )
    return target_norm.upper()


def calculate_launch_progress(
    counts: dict[str, int], attempts_count: int = 0, retries_count: int = 0
) -> dict[str, Any]:
    # Normalize counts
    c = {k.lower(): v for k, v in counts.items()}
    pending = c.get("pending", 0)
    queued = c.get("queued", 0)
    running = c.get("running", 0)
    retry_wait = c.get("retry_wait", 0)
    succeeded = c.get("succeeded", 0)
    failed = c.get("failed", 0)
    timed_out = c.get("timed_out", 0)
    cancelled = c.get("cancelled", 0)

    total = pending + queued + running + retry_wait + succeeded + failed + timed_out + cancelled
    completed = succeeded + failed + timed_out + cancelled
    percentage = round((completed / total) * 100.0, 1) if total > 0 else 0.0

    return {
        "total": total,
        "pending": pending,
        "queued": queued,
        "running": running,
        "retry_wait": retry_wait,
        "succeeded": succeeded,
        "failed": failed,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "completed": completed,
        "percentage": percentage,
        "attempts": attempts_count,
        "retries": retries_count,
    }


class DomainConflictError(ValueError):
    """Domain-level state conflict error, representing invalid operations on current resource state."""
    pass


def determine_allowed_actions(
    launch_status: str,
    cancel_requested_at: Any,
    counts: dict[str, int],
) -> dict[str, Any]:
    st = launch_status.upper()
    c = {k.lower(): v for k, v in counts.items()}
    # Active in-flight items that are actually executing or awaiting retry delay
    in_flight_count = c.get("running", 0) + c.get("retry_wait", 0)
    failed_count = c.get("failed", 0) + c.get("timed_out", 0)
    cancelled_count = c.get("cancelled", 0)

    allowed: list[str] = []
    reasons: dict[str, str] = {}

    # 0. Run
    if st == "PENDING":
        allowed.append("run")
    else:
        reasons["run"] = f"当前状态 '{st}' 已启动或无法再次直接启动"

    # 1. Cancel
    if st in ("PENDING", "QUEUED", "RUNNING") and not cancel_requested_at:
        allowed.append("cancel")
    else:
        if cancel_requested_at:
            reasons["cancel"] = "取消请求已在处理中"
        elif st in ("COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"):
            reasons["cancel"] = f"评测任务已处于终态 '{st}'，无法取消"
        else:
            reasons["cancel"] = f"当前状态 '{st}' 不支持取消"

    # 2. Resume: allowed in CANCELLED or PARTIAL_FAILED when no in-flight items and cancelled_count > 0
    if cancelled_count > 0 and in_flight_count == 0:
        allowed.append("resume")
    else:
        if in_flight_count > 0:
            reasons["resume"] = "任务尚有未完成用例处于排队、准备或运行中"
        elif cancelled_count == 0:
            reasons["resume"] = "没有被取消的用例可供恢复"
        else:
            reasons["resume"] = f"当前状态 '{st}' 无法恢复执行"

    # 3. Retry Failed: allowed when no in-flight items and failed_count > 0
    if failed_count > 0 and in_flight_count == 0:
        allowed.append("retry_failed")
    else:
        if in_flight_count > 0:
            reasons["retry_failed"] = "任务尚有未完成用例处于排队、准备或运行中"
        elif failed_count == 0:
            reasons["retry_failed"] = "当前评测没有失败或超时的用例"
        else:
            reasons["retry_failed"] = f"当前状态 '{st}' 无法重试失败用例"

    return {
        "allowed": allowed,
        "reasons": reasons,
    }


def validate_launch_action_allowed(
    launch_status: str,
    cancel_requested_at: Any,
    counts: dict[str, int],
    action: str,
) -> None:
    """Validates whether a specific action (run, cancel, resume, retry_failed) is permitted.
    Raises DomainConflictError if the action violates lifecycle contracts or has active items.
    """
    actions_info = determine_allowed_actions(launch_status, cancel_requested_at, counts)
    if action not in actions_info["allowed"]:
        reason = actions_info["reasons"].get(action, f"Action '{action}' is not permitted in current state '{launch_status}'")
        raise DomainConflictError(reason)


def aggregate_quality_conclusion(
    quality_counts: dict[str, int] | None,
    total_items: int,
    terminal_fails: int = 0,
) -> str:
    """Strictly independent quality conclusion aggregation according to objective truth table:
    1. Any explicit FAIL or terminal execution failure -> 'fail'
    2. No FAIL, and ALL necessary items are PASS (denominator not shrunk) -> 'pass'
    3. Any UNKNOWN, missing, skipped or cancelled -> 'unknown'
    """
    if total_items == 0:
        return "unknown"

    qc = {k.lower(): v for k, v in (quality_counts or {}).items()}
    if qc.get("fail", 0) > 0 or terminal_fails > 0:
        return "fail"
    if qc.get("pass", 0) == total_items and total_items > 0:
        return "pass"
    return "unknown"


def aggregate_launch_status_from_items(
    counts: dict[str, int],
    quality_counts: dict[str, int] | None = None,
    is_cancelling: bool = False,
) -> tuple[str, str]:
    """Calculate terminal status and quality conclusion based on item execution counts and quality outcomes.
    Execution status and quality conclusion are completely decoupled.
    """
    c = {k.lower(): v for k, v in counts.items()}
    total = sum(c.values())
    succeeded = c.get("succeeded", 0)
    failed = c.get("failed", 0)
    timed_out = c.get("timed_out", 0)
    cancelled = c.get("cancelled", 0)

    terminal_fails = failed + timed_out

    # 1. Execution status aggregation
    if total == 0:
        term_status = "FAILED"
    elif cancelled == total and total > 0:
        term_status = "CANCELLED"
    elif succeeded > 0 and cancelled > 0 and terminal_fails == 0:
        term_status = "CANCELLED"
    elif terminal_fails > 0 and cancelled > 0:
        term_status = "PARTIAL_FAILED"
    elif succeeded > 0 and terminal_fails > 0 and cancelled == 0:
        term_status = "PARTIAL_FAILED"
    elif terminal_fails == total and total > 0:
        term_status = "FAILED"
    elif succeeded == total and total > 0:
        term_status = "COMPLETED"
    else:
        term_status = "FAILED"

    # 2. Quality conclusion aggregation (Completely independent)
    term_quality = aggregate_quality_conclusion(quality_counts, total, terminal_fails)

    return term_status, term_quality

