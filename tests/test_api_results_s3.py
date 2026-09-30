from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app import api_results, main  # noqa: E402
from app.aggregation import aggregate_run  # noqa: E402
from app.db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord, RunResultSnapshotRecord  # noqa: E402
from app.result_snapshots import create_result_snapshot  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _launch(
    db_mgr,
    quality: str,
    score: float,
    baseline_snapshot_id: str | None = None,
    *,
    dataset_id="dataset-golden",
    source="seed",
    observation_id=None,
    case_id="case-1",
):
    launch_id = str(uuid.uuid4())
    manifest = {
        "schema_version": "1.1",
        "dataset": {
            "source": source,
            "dataset_id": dataset_id,
            "dataset_name": "golden",
            "dataset_version": "sha256:same",
            "snapshot_digest": "same",
            "items": [{"id": case_id, "input": {"q": "same"}, "expected_output": {"a": 1}, "metadata": {}}],
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "digest"},
        "evaluators": [{"id": "correctness", "version": "1.0.0", "threshold": 0.8, "direction": "higher_is_better"}],
        "runner": {"runner_version": "0.2.0", "build_id": "test"},
        "comparison": {
            "environment": "production",
            "baseline_snapshot_id": baseline_snapshot_id,
            "baseline_binding_revision": 1 if baseline_snapshot_id else None,
            "comparison_policy_version": "comparison-v1",
        },
    }
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="test",
            status="COMPLETED",
            quality_conclusion=quality,
            dataset_name="golden",
            dataset_version="sha256:same",
            agent_id="test-agent",
            agent_version="v1",
            agent_version_id="test-agent-v1",
            manifest=manifest,
            langfuse_experiment_url=f"https://langfuse.example/runs/{launch_id}",
        )
        session.add(launch)
        session.flush()
        session.add(ExperimentItemExecutionRecord(
            id=str(uuid.uuid4()),
            launch_id=launch_id,
            dataset_item_id=case_id,
            execution_status="succeeded",
            eval_status="succeeded",
            quality_conclusion=quality,
            scores={"correctness": score},
            trace_id=f"trace-{launch_id}",
            observation_id=observation_id or f"obs-{launch_id}",
            langfuse_trace_url=f"https://langfuse.example/project/project-1/traces/trace-{launch_id}",
            dispatch_generation=1,
        ))
        session.commit()
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        return launch_id, snapshot.id


def test_summary_and_comparison_use_frozen_baseline_and_trace_links(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    baseline_launch, baseline_snapshot = _launch(db_mgr, "pass", 1.0)
    candidate_launch, _ = _launch(db_mgr, "fail", 0.2, baseline_snapshot)
    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)

    summary = api_results.get_run_summary(candidate_launch)
    assert summary.versions["dataset"]["version"] == "sha256:same"
    assert summary.summary.total_cases == 1

    comparison = api_results.get_launch_comparison(candidate_launch, classification=None, limit=50, cursor=0)
    assert comparison.baseline_snapshot_id == baseline_snapshot
    assert comparison.classification_counts == {"REGRESSION": 1}
    assert comparison.items[0]["baseline_trace_url"].endswith(f"trace-{baseline_launch}")
    assert comparison.items[0]["candidate_trace_url"].endswith(f"trace-{candidate_launch}")


def test_baseline_http_api_uses_revision_cas(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id, snapshot_id = _launch(db_mgr, "pass", 1.0)
    monkeypatch.setattr(main, "db_manager", db_mgr)
    client = TestClient(main.app)
    path = "/api/v1/agents/test-agent/baselines"

    created = client.post(
        path,
        headers={"X-User": "release-manager"},
        json={
            "environment": "Production",
            "result_snapshot_id": snapshot_id,
            "expected_revision": 0,
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"] == "production"
    assert body["launch_id"] == launch_id
    assert body["revision"] == 1
    assert body["updated_by"] == "release-manager"

    fetched = client.get(path, params={"environment": "production"})
    assert fetched.status_code == 200
    assert fetched.json()["result_snapshot_id"] == snapshot_id

    stale_update = client.post(
        path,
        json={
            "environment": "production",
            "result_snapshot_id": snapshot_id,
            "expected_revision": 0,
        },
    )
    assert stale_update.status_code == 409


def test_explicit_snapshot_reads_history_and_rejects_cross_launch_or_missing_snapshot(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    launch_id, old_snapshot_id = _launch(db_mgr, "pass", 1.0)
    with db_mgr.get_session() as session:
        item = session.query(ExperimentItemExecutionRecord).filter_by(launch_id=launch_id).one()
        item.scores = {"correctness": 0.1}
        launch = session.get(ExperimentLaunchRecord, launch_id)
        new_snapshot = create_result_snapshot(session, launch)
        session.commit()
        new_snapshot_id = new_snapshot.id
    assert new_snapshot_id != old_snapshot_id
    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)

    old = api_results.get_run_summary(launch_id, snapshot_id=old_snapshot_id)
    new = api_results.get_run_summary(launch_id, snapshot_id=new_snapshot_id)
    assert old.snapshot_id == old_snapshot_id
    assert new.snapshot_id == new_snapshot_id
    assert old.summary.score_means["correctness"] == 1.0
    assert new.summary.score_means["correctness"] == 0.1

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as missing:
        api_results.get_run_summary(launch_id, snapshot_id="not-a-snapshot")
    assert missing.value.status_code == 404

    other_launch_id, _ = _launch(db_mgr, "pass", 1.0)
    with pytest.raises(HTTPException) as cross_launch:
        api_results.get_run_summary(other_launch_id, snapshot_id=old_snapshot_id)
    assert cross_launch.value.status_code == 404


def test_different_or_unknown_dataset_identity_disables_case_comparison(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    _, baseline_snapshot = _launch(db_mgr, "pass", 1.0, dataset_id="dataset-a")
    candidate_launch, _ = _launch(db_mgr, "fail", 0.1, baseline_snapshot, dataset_id="dataset-b")
    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)

    mismatch = api_results.get_launch_comparison(candidate_launch, classification=None, limit=50, cursor=0)
    assert mismatch.summary.comparable_case_count == 0
    assert mismatch.items[0]["classification"] == "NOT_COMPARABLE"
    assert mismatch.items[0]["reason"] == "DATASET_IDENTITY_MISMATCH"
    assert mismatch.summary.pass_rate_delta is None
    assert mismatch.summary.score_mean_deltas == {}

    _, unknown_baseline = _launch(db_mgr, "pass", 1.0, dataset_id=None)
    unknown_candidate, _ = _launch(db_mgr, "fail", 0.1, unknown_baseline, dataset_id=None)
    unknown = api_results.get_launch_comparison(unknown_candidate, classification=None, limit=50, cursor=0)
    assert unknown.items[0]["reason"] == "DATASET_IDENTITY_UNKNOWN"
    assert unknown.summary.comparable_case_count == 0


def test_comparison_case_details_support_baseline_only_candidate_only_and_missing_cases(
    setup_runtime, monkeypatch
):
    from fastapi import HTTPException

    db_mgr, _, _, _, _, _ = setup_runtime
    baseline_launch, baseline_snapshot = _launch(db_mgr, "pass", 1.0, case_id="case-1")
    candidate_launch, candidate_snapshot = _launch(
        db_mgr,
        "pass",
        0.9,
        baseline_snapshot,
        case_id="case-2",
    )
    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)

    baseline_observation_id = f"obs-{baseline_launch}"
    baseline_trace_id = f"trace-{baseline_launch}"
    candidate_observation_id = f"obs-{candidate_launch}"
    candidate_trace_id = f"trace-{candidate_launch}"

    class Observations:
        def get_many(self, **_kwargs):
            return SimpleNamespace(data=[
                SimpleNamespace(
                    id=baseline_observation_id,
                    trace_id=baseline_trace_id,
                    output={"answer": "baseline-only output"},
                ),
                SimpleNamespace(
                    id=candidate_observation_id,
                    trace_id=candidate_trace_id,
                    output={"answer": "candidate-only output"},
                ),
            ])

    monkeypatch.setattr(
        "app.result_outputs.get_langfuse_client_safe",
        lambda: SimpleNamespace(api=SimpleNamespace(observations=Observations())),
    )

    baseline_only = api_results.get_comparison_case(
        candidate_launch,
        snapshot_id=candidate_snapshot,
        dataset_item_id="case-1",
    )
    assert baseline_only.classification == "NOT_COMPARABLE"
    assert baseline_only.reason == "CASE_MISSING"
    assert baseline_only.baseline.output_status == "AVAILABLE"
    assert baseline_only.baseline.output == {"answer": "baseline-only output"}
    assert baseline_only.baseline.scores == {"correctness": 1.0}
    assert baseline_only.candidate.output_status == "NO_REFERENCE"
    assert baseline_only.candidate.reason == "NO_OUTPUT_REFERENCE"
    assert baseline_only.candidate.scores == {}

    candidate_only = api_results.get_comparison_case(
        candidate_launch,
        snapshot_id=candidate_snapshot,
        dataset_item_id="case-2",
    )
    assert candidate_only.classification == "NOT_COMPARABLE"
    assert candidate_only.reason == "CASE_MISSING"
    assert candidate_only.baseline.output_status == "NO_REFERENCE"
    assert candidate_only.candidate.output_status == "AVAILABLE"
    assert candidate_only.candidate.output == {"answer": "candidate-only output"}
    assert candidate_only.candidate.scores == {"correctness": 0.9}

    with pytest.raises(HTTPException) as missing:
        api_results.get_comparison_case(
            candidate_launch,
            snapshot_id=candidate_snapshot,
            dataset_item_id="case-3",
        )
    assert missing.value.status_code == 404


def test_comparison_health_uses_full_snapshots_and_quality_uses_common_cases(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime

    def create_ten_case_launch(*, case_results, baseline_snapshot_id=None):
        launch_id = str(uuid.uuid4())
        dataset_items = [
            {"id": case_id, "input": {"query": case_id}, "expected_output": {"answer": case_id}, "metadata": {}}
            for case_id, *_ in case_results
        ]
        manifest = {
            "schema_version": "1.1",
            "dataset": {
                "source": "seed",
                "dataset_id": "dataset-golden",
                "dataset_name": "golden",
                "dataset_version": "v1",
                "snapshot_digest": "ten-cases",
                "items": dataset_items,
            },
            "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "digest"},
            "evaluators": [{"id": "correctness", "version": "1.0.0", "critical": True, "threshold": 0.8}],
            "runner": {"runner_version": "0.2.0", "build_id": "test"},
            "comparison": {"baseline_snapshot_id": baseline_snapshot_id},
        }
        with db_mgr.get_session() as session:
            launch = ExperimentLaunchRecord(
                id=launch_id,
                name="ten-case-report",
                status="COMPLETED",
                dataset_name="golden",
                dataset_version="v1",
                agent_id="test-agent",
                agent_version="v1",
                agent_version_id="test-agent-v1",
                manifest=manifest,
            )
            session.add(launch)
            session.flush()
            for case_id, execution_status, eval_status, quality, score in case_results:
                session.add(ExperimentItemExecutionRecord(
                    id=str(uuid.uuid4()),
                    launch_id=launch_id,
                    dataset_item_id=case_id,
                    execution_status=execution_status,
                    eval_status=eval_status,
                    quality_conclusion=quality,
                    scores={"correctness": score} if score is not None else {},
                    dispatch_generation=1,
                ))
            session.commit()
        with db_mgr.get_session() as session:
            launch = session.get(ExperimentLaunchRecord, launch_id)
            snapshot = create_result_snapshot(session, launch)
            session.commit()
            return launch_id, snapshot.id

    baseline_cases = [
        (f"case-{index}", "succeeded", "succeeded", "pass", 1.0)
        for index in range(10)
    ]
    candidate_cases = [
        (f"case-{index}", "succeeded", "succeeded", "pass", 1.0)
        for index in range(8)
    ] + [
        ("case-8", "timed_out", "skipped", "unknown", None),
        ("case-9", "succeeded", "failed", "unknown", None),
    ]
    _, baseline_snapshot_id = create_ten_case_launch(case_results=baseline_cases)
    candidate_launch_id, candidate_snapshot_id = create_ten_case_launch(
        case_results=candidate_cases,
        baseline_snapshot_id=baseline_snapshot_id,
    )
    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)

    comparison = api_results.get_launch_comparison(
        candidate_launch_id,
        snapshot_id=candidate_snapshot_id,
        classification=None,
        limit=50,
        cursor=0,
    )

    assert comparison.candidate_snapshot_id == candidate_snapshot_id
    assert comparison.summary.candidate.total_cases == 10
    assert comparison.summary.candidate.evaluated_cases == 8
    assert comparison.summary.candidate.evaluation_coverage == 0.8
    assert comparison.summary.candidate.execution_error_count == 1
    assert comparison.summary.candidate.evaluator_error_count == 1
    assert comparison.summary.comparable_case_count == 8
    assert comparison.summary.comparable_cohort.candidate.total_cases == 8


def test_real_retry_failed_keeps_old_snapshot_readable_and_pins_comparison(setup_runtime, monkeypatch):
    from app.result_snapshots import create_result_snapshot
    from app.runner_identity import current_runner_identity

    db_mgr, queue, _, orchestrator, worker, _ = setup_runtime
    launch_id, _ = _launch(db_mgr, "pass", 1.0)
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        item = session.query(ExperimentItemExecutionRecord).filter_by(launch_id=launch_id).one()
        launch.status = "FAILED"
        launch.manifest = {**launch.manifest, "runner": current_runner_identity().model_dump()}
        launch.quality_conclusion = "unknown"
        item.execution_status = "failed"
        item.eval_status = "skipped"
        item.quality_conclusion = "unknown"
        item.scores = {}
        failed_snapshot = create_result_snapshot(session, launch)
        session.commit()
        failed_snapshot_id = failed_snapshot.id

    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)
    retried_launch = orchestrator.retry_failed_items(launch_id)
    assert retried_launch.status == "QUEUED"
    with db_mgr.get_session() as session:
        item = session.query(ExperimentItemExecutionRecord).filter_by(launch_id=launch_id).one()
        generation = item.dispatch_generation
        item_id = item.id
    claim = worker.claim_item(item_id, generation)
    assert claim
    assert worker.finalize_item(
        item_id=item_id,
        generation=generation,
        lease_token=claim["lease_token"],
        status="SUCCEEDED",
        eval_status="succeeded",
        quality_conclusion="pass",
        scores={"correctness": 0.95},
    )

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        launch.status = "COMPLETED"
        item = session.query(ExperimentItemExecutionRecord).filter_by(launch_id=launch_id).one()
        item.execution_status = "succeeded"
        new_snapshot = create_result_snapshot(session, launch)
        session.commit()
        new_snapshot_id = new_snapshot.id

    old_summary = api_results.get_run_summary(launch_id, snapshot_id=failed_snapshot_id)
    new_summary = api_results.get_run_summary(launch_id, snapshot_id=new_snapshot_id)
    old_comparison = api_results.get_launch_comparison(
        launch_id,
        snapshot_id=failed_snapshot_id,
        classification=None,
        limit=50,
        cursor=0,
    )
    assert old_summary.snapshot_id == failed_snapshot_id
    assert new_summary.snapshot_id == new_snapshot_id
    assert old_summary.summary.evaluated_cases == 0
    assert new_summary.summary.evaluated_cases == 1
    assert old_comparison.candidate_snapshot_id == failed_snapshot_id
    assert old_comparison.items[0]["reason"] == "BASELINE_NOT_BOUND"


def test_cost_comparison_uses_frozen_cohort_and_requires_complete_compatible_costs(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    _, baseline_snapshot_id = _launch(db_mgr, "pass", 1.0)
    candidate_launch, candidate_snapshot_id = _launch(db_mgr, "pass", 1.0, baseline_snapshot_id)

    def set_snapshot_cost(snapshot_id, amount, currency="USD", *, complete=True):
        with db_mgr.get_session() as session:
            snapshot = session.get(RunResultSnapshotRecord, snapshot_id)
            items = [dict(item) for item in snapshot.items]
            items[0]["cost"] = {
                "amount": str(amount) if complete else None,
                "currency": currency if complete else None,
                "complete": complete,
                "source": "provider_reported" if complete else None,
                "scope": "launch_case_total" if complete else None,
                "policy_version": "case-cost-v1" if complete else None,
                "unavailable_reason": None if complete else "COST_NOT_RECORDED",
            }
            snapshot.items = items
            snapshot.summary = aggregate_run(items, snapshot.manifest.get("evaluators", []))
            session.commit()

    monkeypatch.setattr(api_results, "_db_manager", lambda: db_mgr)
    set_snapshot_cost(baseline_snapshot_id, "0.02")
    set_snapshot_cost(candidate_snapshot_id, "0.015")
    comparison = api_results.get_launch_comparison(candidate_launch, classification=None, limit=50, cursor=0)
    cost = comparison.summary.cost_comparison
    assert cost.status == "COMPARABLE"
    assert cost.baseline_cost_per_case == 0.02
    assert cost.candidate_cost_per_case == 0.015
    assert cost.delta == pytest.approx(-0.005)

    set_snapshot_cost(candidate_snapshot_id, "0", complete=False)
    partial = api_results.get_launch_comparison(candidate_launch, classification=None, limit=50, cursor=0)
    assert partial.summary.cost_comparison.delta is None
    assert partial.summary.cost_comparison.reason == "COST_NOT_RECORDED"

    set_snapshot_cost(candidate_snapshot_id, "0.015", currency="EUR")
    mixed = api_results.get_launch_comparison(candidate_launch, classification=None, limit=50, cursor=0)
    assert mixed.summary.cost_comparison.delta is None
    assert mixed.summary.cost_comparison.reason == "COST_CURRENCY_MISMATCH"
