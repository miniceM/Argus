from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.state_machine import (  # noqa: E402
    calculate_launch_progress,
    determine_allowed_actions,
    transition_item_status,
    transition_launch_status,
)


def test_launch_status_valid_transitions():
    assert transition_launch_status("PENDING", "QUEUED") == "QUEUED"
    assert transition_launch_status("QUEUED", "RUNNING") == "RUNNING"
    assert transition_launch_status("RUNNING", "COMPLETED") == "COMPLETED"
    assert transition_launch_status("RUNNING", "PARTIAL_FAILED") == "PARTIAL_FAILED"
    assert transition_launch_status("RUNNING", "FAILED") == "FAILED"
    assert transition_launch_status("RUNNING", "CANCELLING") == "CANCELLING"
    assert transition_launch_status("CANCELLING", "CANCELLED") == "CANCELLED"
    # Resume / Retry Failed
    assert transition_launch_status("CANCELLED", "QUEUED") == "QUEUED"
    assert transition_launch_status("PARTIAL_FAILED", "QUEUED") == "QUEUED"
    assert transition_launch_status("FAILED", "QUEUED") == "QUEUED"


def test_launch_status_invalid_transition_raises():
    with pytest.raises(ValueError, match="Invalid Launch status transition"):
        transition_launch_status("COMPLETED", "RUNNING")
    with pytest.raises(ValueError, match="Invalid Launch status transition"):
        transition_launch_status("PENDING", "COMPLETED")


def test_item_status_valid_transitions():
    assert transition_item_status("PENDING", "QUEUED") == "QUEUED"
    assert transition_item_status("QUEUED", "RUNNING") == "RUNNING"
    assert transition_item_status("RUNNING", "SUCCEEDED") == "SUCCEEDED"
    assert transition_item_status("RUNNING", "RETRY_WAIT") == "RETRY_WAIT"
    assert transition_item_status("RETRY_WAIT", "QUEUED") == "QUEUED"
    assert transition_item_status("RUNNING", "FAILED") == "FAILED"
    assert transition_item_status("RUNNING", "TIMED_OUT") == "TIMED_OUT"
    assert transition_item_status("QUEUED", "CANCELLED") == "CANCELLED"
    assert transition_item_status("FAILED", "QUEUED") == "QUEUED"
    assert transition_item_status("TIMED_OUT", "QUEUED") == "QUEUED"


def test_calculate_launch_progress_and_allowed_actions():
    counts = {
        "pending": 0,
        "queued": 2,
        "running": 1,
        "retry_wait": 1,
        "succeeded": 4,
        "failed": 1,
        "timed_out": 1,
        "cancelled": 0,
    }
    progress = calculate_launch_progress(counts, attempts_count=12, retries_count=3)
    assert progress["total"] == 10
    assert progress["queued"] == 2
    assert progress["running"] == 1
    assert progress["retry_wait"] == 1
    assert progress["succeeded"] == 4
    assert progress["failed"] == 1
    assert progress["timed_out"] == 1
    # completed = succeeded(4) + failed(1) + timed_out(1) + cancelled(0) = 6
    assert progress["completed"] == 6
    assert progress["percentage"] == 60.0
    assert progress["attempts"] == 12
    assert progress["retries"] == 3

    # Check allowed_actions when RUNNING
    actions = determine_allowed_actions(
        launch_status="RUNNING",
        cancel_requested_at=None,
        counts=counts,
    )
    assert "cancel" in actions["allowed"]
    assert "resume" not in actions["allowed"]
    assert "retry_failed" not in actions["allowed"]

    # Check allowed_actions when CANCELLED
    cancel_counts = dict(counts, queued=0, running=0, retry_wait=0, cancelled=4)
    actions_cancelled = determine_allowed_actions(
        launch_status="CANCELLED",
        cancel_requested_at="2026-09-21T08:00:00Z",
        counts=cancel_counts,
    )
    assert "resume" in actions_cancelled["allowed"]

    # Check allowed_actions when PARTIAL_FAILED
    actions_failed = determine_allowed_actions(
        launch_status="PARTIAL_FAILED",
        cancel_requested_at=None,
        counts=dict(counts, queued=0, running=0, retry_wait=0),
    )
    assert "retry_failed" in actions_failed["allowed"]


def test_allowed_actions_pending_allows_cancel_and_run():
    counts = {"pending": 5, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0}
    actions = determine_allowed_actions(
        launch_status="PENDING",
        cancel_requested_at=None,
        counts=counts,
    )
    assert "run" in actions["allowed"]
    assert "cancel" in actions["allowed"]

