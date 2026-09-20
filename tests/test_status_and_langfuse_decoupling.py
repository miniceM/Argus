from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.executor import aggregate_launch_status  # noqa: E402


def test_status_aggregation_all_pass():
    items = [
        {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass"},
        {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass"},
    ]
    status, quality = aggregate_launch_status(items)
    assert status == "SUCCEEDED"
    assert quality == "pass"


def test_status_aggregation_quality_fail_is_technical_success():
    items = [
        {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass"},
        {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "fail"},
    ]
    status, quality = aggregate_launch_status(items)
    assert status == "SUCCEEDED"
    assert quality == "fail"


def test_status_aggregation_evaluator_error():
    items = [
        {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass"},
        {"execution_status": "succeeded", "eval_status": "failed", "quality_conclusion": "unknown"},
    ]
    status, quality = aggregate_launch_status(items)
    assert status == "FAILED"
    assert quality == "unknown"


def test_status_aggregation_execution_error():
    items = [
        {"execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "pass"},
        {"execution_status": "failed", "eval_status": "skipped", "quality_conclusion": "unknown"},
    ]
    status, quality = aggregate_launch_status(items)
    assert status == "FAILED"
    assert quality == "fail"
