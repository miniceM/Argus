"""受控 Score 采纳：远程读取、严格关联、不可变存证及幂等。"""

from __future__ import annotations

import copy
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/eval-runner"))

from app.db_models import EvaluationResultRecord, ExperimentItemExecutionRecord, ManagedScoreEvidenceRecord  # noqa: E402
from app.execution_checkpoint import write_execution_checkpoint  # noqa: E402
from app.langfuse_managed_scores import ManagedScoreCollector, ScoresReadError, ScoresV3Client  # noqa: E402
from app.manifest import LaunchService  # noqa: E402
from test_issue_215_managed_evaluation import managed  # noqa: E402
from test_launch_manifest_freeze import setup_db  # noqa: E402


@pytest.fixture
def context(tmp_path):
    db, registry = setup_db(tmp_path)
    launch = managed(LaunchService(db, registry))
    now = datetime.now(UTC)
    with db.get_session() as session:
        item = ExperimentItemExecutionRecord(
            id="item216",
            launch_id=launch.id,
            dataset_item_id="dataset-item",
            execution_status="succeeded",
            dispatch_generation=1,
            trace_id="trace216",
            observation_id="obs216",
        )
        session.add(item)
        session.flush()
        cp = write_execution_checkpoint(
            session,
            item_execution_id=item.id,
            launch_id=launch.id,
            dataset_item_id=item.dataset_item_id,
            dispatch_generation=1,
            output={"answer": "hello"},
            input_payload={"question": "hello"},
            expected_output=None,
            manifest=launch.manifest,
            trace_id=item.trace_id,
            observation_id=item.observation_id,
            now=now,
        )
        assert cp
    observation = {
        "id": "obs216",
        "traceId": "trace216",
        "projectId": "project-215",
        "name": "remote-agent-http",
        "type": "TOOL",
        "environment": "production",
        "startTime": now.isoformat(),
        "input": {"agent": "banking-agent", "request": {"question": "hello"}},
        "output": {"answer": "hello"},
    }
    return db, launch, observation


def score(**patch):
    row = dict(
        id="score216",
        projectId="project-215",
        name="correctness",
        dataType="BOOLEAN",
        value=True,
        source="EVAL",
        environment="production",
        timestamp=datetime.now(UTC).isoformat(),
        createdAt=datetime.now(UTC).isoformat(),
        updatedAt=datetime.now(UTC).isoformat(),
        configId=None,
        subject={"kind": "observation", "id": "obs216", "traceId": "trace216"},
        metadata={
            "evaluation_rule_id": "rule-215",
            "evaluator_id": "judge-215",
            "job_execution_id": "job216",
            "target_trace_id": "trace216",
            "target_observation_id": "obs216",
        },
    )
    row.update(patch)
    return row


class Remote:
    def __init__(self, observation, scores):
        self.observation, self.scores, self.calls = observation, scores, []

    def observations(self, trace_id):
        self.calls.append(("observations", trace_id))
        return [copy.deepcopy(self.observation)]

    def scores_for(self, trace_id, observation_id, name):
        self.calls.append(("scores", trace_id, observation_id, name))
        return copy.deepcopy(self.scores)


def collect(context, rows, remote=None):
    db, launch, obs = context
    return ManagedScoreCollector(db, remote or Remote(obs, rows)).collect(launch.id, "item216")


def records(db, model):
    with db.get_session() as session:
        return list(session.scalars(select(model)))


def test_verified_score_is_typed_immutable_and_idempotent(context):
    first = collect(context, [score()])
    row = records(context[0], EvaluationResultRecord)[0]
    assert first["quality_conclusion"] == "unknown" and first["releasable"] is False
    assert row.value is True and row.result_type == "boolean" and row.status == "succeeded"
    assert row.executor_type == "langfuse_managed" and row.evaluator_version is None
    assert row.evidence["score"]["id"] == "score216"
    assert row.evidence["dispatch_generation"] == 1
    assert row.evidence["acceptance_digest"] == context[1].manifest["managed_evaluation"]["acceptance_digest"]
    second = collect(context, [copy.deepcopy(row.evidence["score"])])
    assert second["decisions"][0]["decision"] == "ALREADY_ADOPTED"
    assert len(records(context[0], EvaluationResultRecord)) == 1
    assert len(records(context[0], ManagedScoreEvidenceRecord)) == 1
    assert records(context[0], EvaluationResultRecord)[0].id == row.id
    with context[0].get_session() as session:
        item = session.get(ExperimentItemExecutionRecord, "item216")
        assert item.quality_conclusion == "unknown" and not item.scores


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"source": "API"}, "SOURCE_MISMATCH"),
        ({"source": "ANNOTATION"}, "SOURCE_MISMATCH"),
        ({"projectId": "another-project"}, "PROJECT_MISMATCH"),
        ({"name": "online-score"}, "NAME_MISMATCH"),
        ({"dataType": "NUMERIC", "value": 1}, "TYPE_MISMATCH"),
        ({"value": 1}, "EVALUATION_TYPE_MISMATCH"),
        ({"value": None}, "EVALUATION_VALUE_MISSING"),
        ({"environment": "other"}, "ENVIRONMENT_MISMATCH"),
        ({"subject": {"kind": "trace", "id": "trace216"}}, "SUBJECT_MISMATCH"),
        ({"subject": {"kind": "observation", "id": "other", "traceId": "trace216"}}, "SUBJECT_MISMATCH"),
        ({"metadata": {}}, "PROVENANCE_MISSING"),
        ({"id": None}, "SCORE_ID_MISSING"),
    ],
)
def test_untrusted_scores_never_create_results(context, patch, reason):
    report = collect(context, [score(**patch)])
    assert reason in {d["reason"] for d in report["decisions"]}
    assert not records(context[0], EvaluationResultRecord)
    assert report["quality_conclusion"] == "unknown"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("evaluation_rule_id", "online-rule", "RULE_MISMATCH"),
        ("evaluator_id", "other-judge", "EVALUATOR_MISMATCH"),
        ("target_trace_id", "other-launch", "TARGET_MISMATCH"),
        ("target_observation_id", "other-observation", "TARGET_MISMATCH"),
        ("job_execution_id", "", "PROVENANCE_MISSING"),
    ],
)
def test_job_provenance_not_inferred_from_name(context, field, value, reason):
    row = score()
    row["metadata"][field] = value
    report = collect(context, [row])
    assert reason in {d["reason"] for d in report["decisions"]}
    assert not records(context[0], EvaluationResultRecord)


def test_ambiguous_candidates_and_late_conflicts_do_not_overwrite(context):
    collect(context, [score(), score(id="duplicate")])
    assert not records(context[0], EvaluationResultRecord)
    collect(context, [score()])
    before = records(context[0], EvaluationResultRecord)[0]
    report = collect(context, [score(id="late", value=False)])
    assert "RESULT_CONFLICT" in {d["reason"] for d in report["decisions"]}
    after = records(context[0], EvaluationResultRecord)[0]
    assert (after.id, after.value, after.evidence) == (before.id, before.value, before.evidence)


def test_same_id_different_payload_rejected_and_exact_duplicate_deduped(context):
    collect(context, [score(), score(value=False)])
    assert not records(context[0], EvaluationResultRecord)
    row = score()
    collect(context, [row, copy.deepcopy(row)])
    assert len(records(context[0], EvaluationResultRecord)) == 1


def test_missing_scores_are_diagnostic_not_a_measurement(context):
    report = collect(context, [])
    assert report["decisions"][0]["reason"] == "SCORE_MISSING"
    assert not records(context[0], EvaluationResultRecord)


@pytest.mark.parametrize("mutation", ["observation", "output", "checkpoint", "generation", "expired", "manifest"])
def test_item_checkpoint_and_observation_identity_fail_closed(context, mutation):
    db, launch, obs = context
    if mutation == "observation":
        obs["id"] = "another"
    elif mutation == "output":
        obs["output"] = {"answer": "different"}
    else:
        from app.db_models import ExecutionCheckpointRecord, ExperimentLaunchRecord

        with db.get_session() as session:
            cp = session.scalar(select(ExecutionCheckpointRecord))
            if mutation == "checkpoint":
                cp.dataset_item_id = "wrong-item"
            elif mutation == "generation":
                session.get(ExperimentItemExecutionRecord, "item216").dispatch_generation = 2
            elif mutation == "expired":
                cp.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                record = session.get(ExperimentLaunchRecord, launch.id)
                record.manifest = {
                    **record.manifest,
                    "managed_evaluation": {**record.manifest["managed_evaluation"], "environment": "tampered"},
                }
    report = collect(context, [score()])
    assert not records(db, EvaluationResultRecord)
    assert report["decisions"][0]["decision"] == "REJECTED"


def client(handler, **kwargs):
    return ScoresV3Client(
        "https://langfuse.example", "public", "secret", transport=httpx.MockTransport(handler), **kwargs
    )


def test_cursor_all_pages_and_filters():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"data": [{"id": str(len(requests))}], "meta": {"cursor": "next" if len(requests) == 1 else None}}
        )

    with client(handler) as remote:
        assert len(remote.scores_for("trace", "obs", "quality,exact")) == 2
    assert requests[0].url.params["fields"] == "details,subject"
    assert requests[0].url.params["source"] == "EVAL"
    # 名称带逗号也只能按本地 exact match 验收，不能放宽采纳条件。
    assert requests[1].url.params["cursor"] == "next"
    assert requests[0].url.params["traceId"] == "trace"
    assert requests[0].url.params["observationId"] == "obs"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AUTHENTICATION_FAILED"),
        (403, "AUTHORIZATION_FAILED"),
        (429, "RATE_LIMITED"),
        (500, "UPSTREAM_UNAVAILABLE"),
    ],
)
def test_http_failures_are_bounded_and_secret_free(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="secret public private upstream body", headers={"Retry-After": "2"})

    with client(handler) as remote:
        with pytest.raises(ScoresReadError) as error:
            remote.scores_for("trace", "obs", "score")
    assert error.value.code == code
    assert "secret" not in str(error.value) and "private" not in str(error.value)
    assert len(calls) == 1


def test_timeout_and_partial_or_looping_pagination_fail_closed():
    def timeout(request):
        raise httpx.ReadTimeout("secret", request=request)

    with client(timeout) as remote, pytest.raises(ScoresReadError, match="TIMEOUT"):
        remote.scores_for("trace", "obs", "score")
    with client(lambda r: httpx.Response(200, json={"data": [], "meta": {"cursor": "loop"}})) as remote:
        with pytest.raises(ScoresReadError, match="PAGINATION"):
            remote.scores_for("trace", "obs", "score")


def test_collect_and_read_api_expose_evidence_without_gate_mutation(context, monkeypatch):
    from app import api_launches
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_launches.router)
    app.dependency_overrides[api_launches.get_services] = lambda: (context[0], None, None, None)
    remote = Remote(context[2], [score()])
    monkeypatch.setattr(api_launches, "get_managed_score_client", lambda: remote, raising=False)
    with TestClient(app) as api:
        base = f"/api/v1/experiment-launches/{context[1].id}/items/item216/managed-scores"
        response = api.post(base + "/collect")
        assert response.status_code == 200, response.text
        assert response.json()["decisions"][0]["decision"] == "ADOPTED"
        response = api.get(base)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"][0]["evidence"]["score"]["id"] == "score216"
        assert body["evidence"][0]["reason"] == "VERIFIED"
        assert body["quality_conclusion"] == "unknown" and body["releasable"] is False
        assert api.get(base.replace("item216", "missing")).status_code == 404


@pytest.mark.parametrize(
    "kind,value,categories,accepted",
    [
        ("numeric", 0.72, None, True),
        ("numeric", 0, None, True),
        ("numeric", float("nan"), None, False),
        ("numeric", float("inf"), None, False),
        ("numeric", True, None, False),
        ("numeric", "0.8", None, False),
        ("boolean", False, None, True),
        ("categorical", "safe", ["safe", "unsafe"], True),
        ("categorical", "alien", ["safe", "unsafe"], False),
        ("text", "review note", None, True),
    ],
)
def test_typed_values_preserve_raw_types(context, kind, value, categories, accepted):
    from app.db_models import ExecutionCheckpointRecord, ExperimentLaunchRecord
    from app.evaluator_binding import canonical_digest

    db, launch, _ = context
    # 固定测试用契约与 checkpoint；生产 API 不允许编辑 Manifest。
    with db.get_session() as session:
        record = session.get(ExperimentLaunchRecord, launch.id)
        manifest = copy.deepcopy(record.manifest)
        frozen = manifest["managed_evaluation"]
        frozen["scores"][0]["data_type"] = kind
        if categories:
            frozen["scores"][0]["category_values"] = categories
        frozen.pop("acceptance_digest")
        frozen["acceptance_digest"] = canonical_digest(frozen)
        record.manifest = manifest
        session.scalar(select(ExecutionCheckpointRecord)).manifest_digest = canonical_digest(manifest)
    report = collect(context, [score(dataType=kind.upper(), value=value)])
    results = records(db, EvaluationResultRecord)
    assert bool(results) is accepted
    if accepted:
        assert results[0].value == value
        assert type(results[0].value) is type(value)
    else:
        assert report["decisions"][0]["decision"] == "REJECTED"


def test_no_partial_results_on_later_request_failure(context):
    class Unavailable(Remote):
        def scores_for(self, *args):
            raise ScoresReadError("RATE_LIMITED", retry_after="2")

    report = collect(context, [], Unavailable(context[2], []))
    assert report["retry_after"] == "2"
    assert not records(context[0], EvaluationResultRecord)
    assert records(context[0], ManagedScoreEvidenceRecord)[0].reason == "RATE_LIMITED"


def test_context_changes_during_network_read_are_rejected(context):
    class Racing(Remote):
        def scores_for(self, *args):
            with context[0].get_session() as session:
                item = session.get(ExperimentItemExecutionRecord, "item216")
                item.dispatch_generation += 1
            return self.scores

    report = collect(context, [], Racing(context[2], [score()]))
    assert report["decisions"][0]["reason"] == "COLLECTION_CONTEXT_CHANGED"
    assert not records(context[0], EvaluationResultRecord)


def test_builtin_is_not_opted_in_and_never_queried(context):
    from app.db_models import ExperimentLaunchRecord

    with context[0].get_session() as session:
        record = session.get(ExperimentLaunchRecord, context[1].id)
        manifest = copy.deepcopy(record.manifest)
        manifest.pop("evaluation_source")
        record.manifest = manifest
    remote = Remote(context[2], [score()])
    report = collect(context, [], remote)
    assert report["decisions"][0]["reason"] == "MANAGED_NOT_SELECTED"
    assert not remote.calls and not records(context[0], EvaluationResultRecord)


def test_same_external_score_cannot_be_claimed_by_another_launch(context):
    db, launch, obs = context
    collect(context, [score()])
    # 即使有人错误复用 Trace/Observation，唯一外部分数键仍禁止跨 Launch/Item 复用。
    from app.db_models import ExecutionCheckpointRecord

    other = managed(LaunchService(db, __import__("app.registry", fromlist=["AgentRegistry"]).AgentRegistry(db)))
    with db.get_session() as session:
        original_cp = session.scalar(select(ExecutionCheckpointRecord))
        session.add(
            ExperimentItemExecutionRecord(
                id="other-item",
                launch_id=other.id,
                dataset_item_id="dataset-item",
                execution_status="succeeded",
                trace_id="trace216",
                observation_id="obs216",
                dispatch_generation=1,
            )
        )
        session.flush()
        write_execution_checkpoint(
            session,
            item_execution_id="other-item",
            launch_id=other.id,
            dataset_item_id="dataset-item",
            dispatch_generation=1,
            output=original_cp.agent_output,
            input_payload=original_cp.input_payload,
            expected_output=None,
            manifest=other.manifest,
            trace_id="trace216",
            observation_id="obs216",
        )
    raw = records(db, EvaluationResultRecord)[0].evidence["score"]
    report = ManagedScoreCollector(db, Remote(obs, [raw])).collect(other.id, "other-item")
    assert report["decisions"][0]["reason"] == "SCORE_ALREADY_CLAIMED"
    assert len(records(db, EvaluationResultRecord)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": []},
        {"data": None, "meta": {"cursor": None}},
        {"data": [None], "meta": {"cursor": None}},
        {"data": [], "meta": {"cursor": False}},
    ],
)
def test_malformed_pages_are_not_empty_success(payload):
    with client(lambda r: httpx.Response(200, json=payload)) as remote, pytest.raises(ScoresReadError):
        remote.scores_for("t", "o", "s")


def test_partial_cursor_pages_and_page_budget_are_fail_closed():
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, json={"data": [score()], "meta": {"cursor": "next"}})
            if len(calls) == 1
            else httpx.Response(429)
        )

    with client(handler) as remote, pytest.raises(ScoresReadError, match="RATE_LIMITED"):
        remote.scores_for("t", "o", "s")
    with client(
        lambda r: httpx.Response(200, json={"data": [score()], "meta": {"cursor": "next"}}), max_pages=1
    ) as remote:
        with pytest.raises(ScoresReadError, match="PAGINATION_LIMIT"):
            remote.scores_for("t", "o", "s")


def test_score_timestamp_before_observation_is_rejected(context):
    report = collect(context, [score(timestamp="2020-01-01T00:00:00Z")])
    assert report["decisions"][0]["reason"] == "SCORE_TIME_INVALID"
    assert not records(context[0], EvaluationResultRecord)


def test_concurrent_collection_has_one_result_and_audit(context):
    from concurrent.futures import ThreadPoolExecutor

    raw = score()
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda _: collect(context, [raw]), range(4)))
    assert len(records(context[0], EvaluationResultRecord)) == 1
    assert len(records(context[0], ManagedScoreEvidenceRecord)) == 1
    assert all(r["decisions"][0]["decision"] in ("ADOPTED", "ALREADY_ADOPTED") for r in reports)


def test_audit_retains_late_mutation_and_valid_source_wins_over_online(context):
    raw = score()
    report = collect(context, [raw, score(id="online", source="API")])
    assert {d["decision"] for d in report["decisions"]} == {"ADOPTED", "REJECTED"}
    before = records(context[0], EvaluationResultRecord)[0]
    raw["value"] = False
    assert collect(context, [raw])["decisions"][0]["reason"] == "RESULT_CONFLICT"
    after = records(context[0], EvaluationResultRecord)[0]
    assert after.value is True and after.evidence == before.evidence


def test_invalid_score_id_is_json_safe_in_api(context, monkeypatch):
    from app import api_launches
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_launches.router)
    app.dependency_overrides[api_launches.get_services] = lambda: (context[0], None, None, None)
    monkeypatch.setattr(api_launches, "get_managed_score_client", lambda: Remote(context[2], [score(id={"bad": 1})]))
    response = TestClient(app).post(f"/api/v1/experiment-launches/{context[1].id}/items/item216/managed-scores/collect")
    assert response.status_code == 200
    assert response.json()["decisions"][0]["score_id"] is None
