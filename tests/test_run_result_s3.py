from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app.baselines import BaselineConflictError, get_baseline, set_baseline  # noqa: E402
from app.db_models import ExecutionAttemptRecord, ExperimentItemExecutionRecord, ExperimentLaunchRecord  # noqa: E402
from app.result_snapshots import create_result_snapshot, latest_result_snapshot  # noqa: E402


def _create_completed_launch(db_mgr):
    launch_id = str(uuid.uuid4())
    manifest = {
        "schema_version": "1.1",
        "dataset": {
            "dataset_name": "golden",
            "dataset_version": "v1",
            "snapshot_digest": "dataset-digest",
            "items": [
                {"id": "case-1", "input": {"q": "a"}, "expected_output": {"a": 1}, "metadata": {}},
                {"id": "case-2", "input": {"q": "b"}, "expected_output": {"a": 2}, "metadata": {}},
            ],
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "agent-digest"},
        "evaluators": [{"id": "correctness", "version": "1.0.0", "threshold": 0.8, "critical": True}],
        "quality_policy": {"mode": "all_selected_must_pass"},
        "runner": {"runner_version": "0.2.0", "build_id": "test"},
        "comparison": {"environment": "production", "comparison_policy_version": "comparison-v1"},
    }
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="baseline",
            status="COMPLETED",
            quality_conclusion="fail",
            dataset_name="golden",
            dataset_version="v1",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=manifest,
            completed_at=datetime.now(UTC),
        )
        session.add(launch)
        session.flush()
        for case_id, score, quality in (("case-1", 1.0, "pass"), ("case-2", 0.0, "fail")):
            session.add(ExperimentItemExecutionRecord(
                id=str(uuid.uuid4()),
                launch_id=launch_id,
                dataset_item_id=case_id,
                execution_status="succeeded",
                eval_status="succeeded",
                quality_conclusion=quality,
                scores={"correctness": score},
                dispatch_generation=1,
            ))
        session.commit()
    return launch_id


def test_result_snapshot_is_idempotent_and_retry_creates_new_immutable_revision(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        first = create_result_snapshot(session, launch)
        session.commit()
        first_id = first.id
        assert first.summary["pass_rate"] == 0.5
        assert first.summary["evaluation_coverage"] == 1.0
        assert first.summary["critical_failure_count"] == 1

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        same = create_result_snapshot(session, launch)
        session.commit()
        assert same.id == first_id

    with db_mgr.get_session() as session:
        changed = session.scalar(
            __import__("sqlalchemy").select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == launch_id,
                ExperimentItemExecutionRecord.dataset_item_id == "case-2",
            )
        )
        changed.scores = {"correctness": 1.0}
        changed.quality_conclusion = "pass"
        changed.dispatch_generation = 2
        session.commit()

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        second = create_result_snapshot(session, launch)
        session.commit()
        assert second.revision == 2
        assert second.id != first_id
        assert second.summary["pass_rate"] == 1.0
        old = session.get(type(second), first_id)
        assert old.summary["pass_rate"] == 0.5
        assert latest_result_snapshot(session, launch_id).id == second.id


def test_baseline_binding_uses_compare_and_swap_and_allows_quality_fail(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        snapshot = create_result_snapshot(session, session.get(ExperimentLaunchRecord, launch_id))
        session.commit()
        snapshot_id = snapshot.id

    binding = set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="Production",
        result_snapshot_id=snapshot_id,
        expected_revision=0,
        updated_by="test-user",
    )
    assert binding.environment == "production"
    assert binding.revision == 1

    current = get_baseline(db_mgr, "test-agent", "production")
    assert current is not None
    assert current[0].result_snapshot_id == snapshot_id
    with pytest.raises(BaselineConflictError, match="revision conflict"):
        set_baseline(
            db_mgr,
            agent_id="test-agent",
            environment="production",
            result_snapshot_id=snapshot_id,
            expected_revision=0,
        )


def test_new_launch_freezes_the_current_environment_baseline(setup_runtime, monkeypatch):
    from app.manifest import LaunchService
    from app.registry import AgentRegistry

    db_mgr, _, _, _, _, _ = setup_runtime
    baseline_launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        baseline = create_result_snapshot(session, session.get(ExperimentLaunchRecord, baseline_launch_id))
        session.commit()
        baseline_id = baseline.id
    set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=baseline_id,
        expected_revision=0,
    )

    monkeypatch.setenv("ARGUS_DATASET_SOURCE", "seed")
    service = LaunchService(db_mgr, AgentRegistry(db_mgr), runner_version="0.2.0")
    candidate = service.create_launch(
        agent_id="test-agent",
        agent_version="v1",
        dataset_name="banking-agent-regression",
        environment="Production",
        evaluator_ids=["intent_match"],
    )
    assert candidate.manifest["comparison"]["environment"] == "production"
    assert candidate.manifest["comparison"]["baseline_snapshot_id"] == baseline_id
    assert candidate.manifest["comparison"]["baseline_binding_revision"] == 1
    assert candidate.manifest["comparison"]["baseline_resolution"] == "automatic"
    assert candidate.manifest["runner"]["runner_version"] == "0.2.0"
    assert candidate.manifest["runner"]["build_id"]

    # Changing the current pointer does not rewrite an already-created Launch manifest.
    next_baseline_launch_id = _create_completed_launch(db_mgr)
    with db_mgr.get_session() as session:
        next_snapshot = create_result_snapshot(session, session.get(ExperimentLaunchRecord, next_baseline_launch_id))
        session.commit()
        next_baseline_id = next_snapshot.id
    set_baseline(
        db_mgr,
        agent_id="test-agent",
        environment="production",
        result_snapshot_id=next_baseline_id,
        expected_revision=1,
    )
    with db_mgr.get_session() as session:
        frozen = session.get(ExperimentLaunchRecord, candidate.id)
        assert frozen.manifest["comparison"]["baseline_snapshot_id"] == baseline_id


def test_invalid_environment_is_rejected_before_dataset_resolution(setup_runtime, monkeypatch):
    from app.manifest import LaunchService
    from app.registry import AgentRegistry

    db_mgr, _, _, _, _, _ = setup_runtime
    monkeypatch.setattr("app.manifest.DatasetResolver.resolve", lambda *args, **kwargs: pytest.fail("dataset should not be resolved"))
    service = LaunchService(db_mgr, AgentRegistry(db_mgr))
    with pytest.raises(ValueError, match="environment"):
        service.create_launch(
            agent_id="test-agent",
            agent_version="v1",
            dataset_name="banking-agent-regression",
            environment="prod/unsafe",
            evaluator_ids=["intent_match"],
        )


def test_reconciler_materializes_terminal_result_snapshot_once(setup_runtime):
    from app.db_models import LangfuseRunScoreTaskRecord, RunResultSnapshotRecord

    db_mgr, _, _, _, _, reconciler = setup_runtime
    launch_id = _create_completed_launch(db_mgr)

    assert reconciler.reconcile_launch_states() == 0
    with db_mgr.get_session() as session:
        snapshots = session.scalars(
            __import__("sqlalchemy").select(RunResultSnapshotRecord).where(
                RunResultSnapshotRecord.launch_id == launch_id
            )
        ).all()
        tasks = session.scalars(
            __import__("sqlalchemy").select(LangfuseRunScoreTaskRecord).where(
                LangfuseRunScoreTaskRecord.launch_id == launch_id
            )
        ).all()
        assert len(snapshots) == 1
        assert snapshots[0].summary["total_cases"] == 2
        assert len(tasks) == 1
        assert tasks[0].snapshot_id == snapshots[0].id

    # A later reconciliation cycle is idempotent and must not enqueue duplicate Run scores.
    reconciler.reconcile_launch_states()
    with db_mgr.get_session() as session:
        assert session.scalar(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(RunResultSnapshotRecord).where(
                RunResultSnapshotRecord.launch_id == launch_id
            )
        ) == 1
        assert session.scalar(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(LangfuseRunScoreTaskRecord).where(
                LangfuseRunScoreTaskRecord.launch_id == launch_id
            )
        ) == 1


def test_snapshot_freezes_launch_case_retry_cost_across_generations_and_preserves_history(setup_runtime):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id = _create_completed_launch(db_mgr)

    with db_mgr.get_session() as session:
        items = list(session.query(ExperimentItemExecutionRecord)
                     .filter_by(launch_id=launch_id).order_by(ExperimentItemExecutionRecord.dataset_item_id))
        first = items[0]
        attempts = [
            ExecutionAttemptRecord(
                id="attempt-old-generation", item_execution_id=first.id, attempt_no=0,
                status="COMPLETED", dispatch_generation=0, latency_ms=10,
                usage_cost={"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                            "cost": {"amount": "0.002", "currency": "USD", "source": "provider_reported",
                                     "measurement_scope": "agent_invocation_total", "unavailable_reason": None}},
            ),
            ExecutionAttemptRecord(
                id="attempt-retry-1", item_execution_id=first.id, attempt_no=1,
                status="FAILED", dispatch_generation=1, latency_ms=10,
                usage_cost={"usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                            "cost": {"amount": "0.003", "currency": "USD", "source": "provider_reported",
                                     "measurement_scope": "agent_invocation_total", "unavailable_reason": None}},
            ),
            ExecutionAttemptRecord(
                id="attempt-retry-2", item_execution_id=first.id, attempt_no=2,
                status="COMPLETED", dispatch_generation=1, latency_ms=20,
                usage_cost={"usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
                            "cost": {"amount": "0.007", "currency": "USD", "source": "provider_reported",
                                     "measurement_scope": "agent_invocation_total", "unavailable_reason": None}},
            ),
        ]
        session.add_all(attempts)
        session.flush()
        first.final_attempt_id = "attempt-retry-2"
        session.commit()

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        snapshot_id = snapshot.id
        first_item = next(item for item in snapshot.items if item["dataset_item_id"] == "case-1")
        assert first_item["cost"]["amount"] == "0.012"
        assert first_item["cost"]["complete"] is True
        assert first_item["cost_evidence"]["attempt_count"] == 3
        assert first_item["usage"]["total_tokens"] == 11
        assert snapshot.summary["total_cost"] == 0.012
        assert snapshot.summary["cost_per_case"] == 0.012
        assert snapshot.summary["cost_coverage"] == 0.5

    with db_mgr.get_session() as session:
        retry = session.get(ExecutionAttemptRecord, "attempt-retry-2")
        retry.usage_cost["cost"]["amount"] = "0.020"
        retry.usage_cost = dict(retry.usage_cost)
        session.commit()
        launch = session.get(ExperimentLaunchRecord, launch_id)
        new_snapshot = create_result_snapshot(session, launch)
        session.commit()
        old_snapshot = session.get(type(new_snapshot), snapshot_id)
        assert new_snapshot.revision == 2
        assert new_snapshot.summary["total_cost"] == 0.025
        assert old_snapshot.summary["total_cost"] == 0.012
        old_first = next(item for item in old_snapshot.items if item["dataset_item_id"] == "case-1")
        assert old_first["cost"]["amount"] == "0.012"
