from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "eval-runner"))

from app import main, result_outputs  # noqa: E402
from app.db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord  # noqa: E402
from app.result_snapshots import create_result_snapshot  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _launch(db_mgr, quality, score, *, baseline_snapshot_id=None, observation_id=None):

    launch_id = str(uuid.uuid4())
    manifest = {
        "schema_version": "1.1",
        "dataset": {
            "source": "langfuse",
            "dataset_id": "dataset-outputs",
            "dataset_name": "golden",
            "dataset_version": "v1",
            "snapshot_digest": "same",
            "items": [{"id": "case-1", "input": {"q": "same"}, "expected_output": {"a": 1}, "metadata": {}}],
        },
        "agent": {"agent_id": "test-agent", "version": "v1", "spec_digest": "digest"},
        "evaluators": [{"id": "correctness", "version": "1.0.0", "threshold": 0.8, "direction": "higher_is_better"}],
        "runner": {"runner_version": "0.1.0", "build_id": "test-build-001", "mapping_engine_version": "mapping-v1"},
        "comparison": {"baseline_snapshot_id": baseline_snapshot_id},
    }
    with db_mgr.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=launch_id,
            name="test",
            status="COMPLETED",
            quality_conclusion=quality,
            dataset_name="golden",
            dataset_version="v1",
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
            dataset_item_id="case-1",
            execution_status="succeeded",
            eval_status="succeeded",
            quality_conclusion=quality,
            scores={"correctness": score},
            trace_id=f"trace-{launch_id}",
            observation_id=observation_id or f"obs-{launch_id}",
            langfuse_trace_url=f"https://langfuse.example/project/p/traces/trace-{launch_id}",
            dispatch_generation=1,
        ))
        session.commit()
    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        snapshot = create_result_snapshot(session, launch)
        session.commit()
        return launch_id, snapshot.id


class FakeObservations:
    def __init__(self, *, fail_trace_id=None, output_by_observation=None, records=None):
        self.fail_trace_id = fail_trace_id
        self.output_by_observation = output_by_observation or {}
        self.records = records
        self.calls = []

    def get_many(self, **kwargs):
        self.calls.append(kwargs)
        filters = json.loads(kwargs["filter"])
        observation_id = next(f["value"] for f in filters if f["column"] == "id")
        trace_id = next(f["value"] for f in filters if f["column"] == "traceId")
        if trace_id == self.fail_trace_id:
            raise TimeoutError("secret or upstream response must not leak")
        records = self.records
        if records is None:
            records = [SimpleNamespace(
                id=observation_id,
                trace_id=trace_id,
                output=self.output_by_observation.get(observation_id, "{}"),
            )]
        return SimpleNamespace(data=records)


def test_case_output_api_uses_only_frozen_observation_refs_and_sides_fail_independently(setup_runtime, monkeypatch):
    db_mgr, _, _, _, _, _ = setup_runtime
    _, baseline_snapshot_id = _launch(db_mgr, "pass", 1.0, observation_id="saved-baseline-observation")
    candidate_launch_id, candidate_snapshot_id = _launch(
        db_mgr, "fail", 0.2,
        baseline_snapshot_id=baseline_snapshot_id,
        observation_id="saved-candidate-observation",
    )
    observations = FakeObservations(
        fail_trace_id=f"trace-{candidate_launch_id}",
        output_by_observation={"saved-baseline-observation": '{"answer": "baseline"}'},
    )
    monkeypatch.setattr(result_outputs, "get_langfuse_client_safe", lambda: SimpleNamespace(api=SimpleNamespace(observations=observations)))
    monkeypatch.setattr(main, "db_manager", db_mgr)
    client = TestClient(main.app)

    response = client.get(
        f"/api/v1/experiment-launches/{candidate_launch_id}/comparison/case",
        params={
            "snapshot_id": candidate_snapshot_id,
            "dataset_item_id": "case-1",
            "observation_id": "attacker-controlled-id",
            "trace_id": "attacker-controlled-trace",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["classification"] == "REGRESSION"
    assert body["baseline"]["output_status"] == "AVAILABLE"
    assert body["baseline"]["output"] == {"answer": "baseline"}
    assert body["candidate"]["output_status"] == "FETCH_FAILED"
    assert body["candidate"]["reason"] == "UPSTREAM_ERROR"
    assert "secret" not in response.text
    assert len(observations.calls) == 2
    requested_refs = {
        filter_item["value"]
        for call in observations.calls
        for filter_item in json.loads(call["filter"])
    }
    assert "saved-baseline-observation" in requested_refs
    assert "saved-candidate-observation" in requested_refs
    assert "attacker-controlled-id" not in requested_refs
    assert "attacker-controlled-trace" not in requested_refs
    assert all(call["fields"] == "core,io" and call["limit"] == 2 for call in observations.calls)
    assert all(call["request_options"] == {"timeout_in_seconds": 15, "max_retries": 2} for call in observations.calls)


def test_cached_output_bypasses_langfuse_call():
    observations = FakeObservations()
    client = SimpleNamespace(api=SimpleNamespace(observations=observations))
    cached = result_outputs.fetch_observation_output(
        {"observation_id": "obs", "trace_id": "trace", "cached_output": {"hello": "world"}}, client=client
    )
    assert cached.output_status == "AVAILABLE"
    assert cached.output == {"hello": "world"}
    assert len(observations.calls) == 0


def test_output_reference_and_returned_observation_ids_are_verified():
    observations = FakeObservations(records=[SimpleNamespace(id="another-observation", trace_id="another-trace", output="{}")] )
    client = SimpleNamespace(api=SimpleNamespace(observations=observations))
    missing_ref = result_outputs.fetch_observation_output(None, client=client)
    assert missing_ref.output_status == "NO_REFERENCE"

    wrong_ids = result_outputs.fetch_observation_output(
        {"observation_id": "saved-id", "trace_id": "saved-trace"}, client=client
    )
    assert wrong_ids.output_status == "NOT_FOUND"
    assert len(observations.calls) == 1


def test_json_null_output_is_available_and_large_outputs_are_truncated():
    null_client = SimpleNamespace(api=SimpleNamespace(observations=FakeObservations(output_by_observation={"obs": "null"})))
    available_null = result_outputs.fetch_observation_output(
        {"observation_id": "obs", "trace_id": "trace"}, client=null_client
    )
    assert available_null.output_status == "AVAILABLE"
    assert available_null.output is None

    large_client = SimpleNamespace(api=SimpleNamespace(observations=FakeObservations(output_by_observation={"obs": "x" * 25_000})))
    large = result_outputs.fetch_observation_output(
        {"observation_id": "obs", "trace_id": "trace"}, client=large_client
    )
    assert large.output_status == "AVAILABLE"
    assert large.truncated is True
    assert len(large.output) <= result_outputs.MAX_OUTPUT_CHARS + 1
