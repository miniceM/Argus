"""Issue #214: offline verifier contracts, NOT evidence of managed execution."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("managed_probe", ROOT / "scripts/verify_langfuse_managed.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.fixture
def receipt():
    return {
        "trace_id": "1" * 32, "observation_id": "2" * 16,
        "environment": "isolated-test", "dataset_id": "dataset-1", "dataset_item_id": "case-1",
        "experiment_id": "experiment-1", "from_start_time": "2026-10-19T00:00:00Z",
        "input_digest": probe.json_digest({"request": {"question": "compute"}}),
        "output_digest": probe.json_digest({"text": "1266"}),
        "evaluators": [
            {"id": "judge", "version_id": "judge-v1", "rule_id": "rule-1", "score_name": "judge", "type": "llm_as_judge"},
            {"id": "code", "version_id": "code-v1", "rule_id": "rule-2", "score_name": "exact", "type": "code"},
        ],
    }


def scores_for(receipt):
    return [
        {"id": "score-" + e["id"], "source": "EVAL", "name": e["score_name"],
         "dataType": "BOOLEAN", "value": True, "environment": receipt["environment"],
         "subject": {"kind": "observation", "id": receipt["observation_id"], "traceId": receipt["trace_id"]},
         "metadata": {"evaluator_id": e["id"], "evaluator_version_id": e["version_id"],
                      "evaluation_rule_id": e["rule_id"], "job_execution_id": "job-" + e["id"],
                      "target_trace_id": receipt["trace_id"], "target_observation_id": receipt["observation_id"],
                      **({"dispatcher_name": "aws-lambda", "code_eval_runtime": "PYTHON"} if e["type"] == "code" else {})}}
        for e in receipt["evaluators"]
    ]


def test_business_tool_is_not_metadata_only_experiment_replay(receipt):
    business = {"id": receipt["observation_id"], "traceId": receipt["trace_id"], "type": "TOOL",
                "name": "remote-agent-http", "environment": receipt["environment"],
                "input": {"request": {"question": "compute"}}, "output": {"text": "1266"}}
    replay = {**business, "type": "SPAN", "name": "argus-run-item-replay", "input": "", "output": "", "environment": "default"}
    assert probe.verify_observation([replay, business], receipt) == business
    for rows in ([replay], [business, copy.deepcopy(business)]):
        with pytest.raises(probe.EvidenceError):
            probe.verify_observation(rows, receipt)


@pytest.mark.parametrize("field,value", [("traceId", "other-trace"), ("environment", "other-env"),
                                        ("input", None), ("output", {"answer": "1266"})])
def test_observation_identity_and_native_io_fail_closed(receipt, field, value):
    business = {"id": receipt["observation_id"], "traceId": receipt["trace_id"], "type": "TOOL",
                "name": "remote-agent-http", "environment": receipt["environment"],
                "input": {"request": {"question": "compute"}}, "output": {"text": "1266"}, field: value}
    with pytest.raises(probe.EvidenceError):
        probe.verify_observation([business], receipt)


def test_both_managed_scores_require_exact_subject_and_native_jobs(receipt):
    rows = scores_for(receipt)
    assert probe.verify_scores(rows, receipt) == rows
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows[:1], receipt)
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows + [copy.deepcopy(rows[0])], receipt)


@pytest.mark.parametrize("field,value", [("source", "API"), ("source", "ANNOTATION"), ("dataType", "NUMERIC"),
                                        ("value", 1), ("value", False), ("name", "same-looking-name"),
                                        ("environment", "other-env"), ("subject", {"kind": "trace"})])
def test_manually_submitted_wrong_type_or_wrong_subject_is_not_managed_proof(receipt, field, value):
    rows = scores_for(receipt)
    rows[0][field] = value
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows, receipt)


@pytest.mark.parametrize("field,value", [("evaluator_version_id", "old"), ("evaluation_rule_id", "other"),
                                        ("job_execution_id", ""), ("target_trace_id", "other"),
                                        ("target_observation_id", "other")])
def test_metadata_provenance_is_not_inferred_from_name(receipt, field, value):
    rows = scores_for(receipt)
    rows[0]["metadata"][field] = value
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows, receipt)


def test_code_requires_remote_runtime_and_not_another_copy_of_judge(receipt):
    rows = scores_for(receipt)
    del rows[1]["metadata"]["dispatcher_name"]
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows, receipt)
    invalid = copy.deepcopy(receipt)
    invalid["evaluators"][1] = invalid["evaluators"][0]
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(scores_for(receipt), invalid)


def test_experiment_item_join_includes_dataset_and_root_identity(receipt):
    item = {"id": receipt["observation_id"], "traceId": receipt["trace_id"],
            "experimentId": receipt["experiment_id"], "experimentDatasetId": receipt["dataset_id"],
            "experimentItemId": receipt["dataset_item_id"]}
    assert probe.verify_experiment_item([item], receipt) == item
    for field in item:
        with pytest.raises(probe.EvidenceError):
            probe.verify_experiment_item([{**item, field: "other"}], receipt)
    with pytest.raises(probe.EvidenceError):
        probe.verify_experiment_item([item, copy.deepcopy(item)], receipt)


def test_cursor_pagination_is_complete_and_non_repeating():
    calls = []
    def fetch(path, params):
        calls.append((path, dict(params)))
        return {"data": [params.get("cursor", "first")], "meta": {"cursor": "second" if "cursor" not in params else None}}
    assert probe.read_pages(fetch, "/api/public/v3/scores", {"limit": 1}) == ["first", "second"]
    assert calls[1][1]["cursor"] == "second"
    with pytest.raises(probe.EvidenceError):
        probe.read_pages(lambda *_: {"data": [], "meta": {"cursor": "loop"}}, "/api/public/v3/scores", {})
    with pytest.raises(probe.EvidenceError):
        probe.read_pages(lambda *_: {"meta": {}}, "/api/public/v3/scores", {})


def test_http_client_only_issues_reads(monkeypatch):
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self): return json.dumps({"data": [], "meta": {}}).encode()
    def open_request(req, **_):
        seen.append(req)
        return Response()
    monkeypatch.setattr(probe.urllib.request, "urlopen", open_request)
    client = probe.ReadOnlyLangfuse("https://example.test", "public-test", "secret-test")
    client.get("/api/public/v3/scores", {"source": "EVAL", "fields": "subject,details"})
    assert seen[0].get_method() == "GET" and seen[0].data is None
    assert "secret-test" not in seen[0].full_url
    for path in ["https://other.test", "/api/public/scores", "/api/public/v2/evaluators", "//other.test"]:
        with pytest.raises(probe.EvidenceError):
            client.get(path, {})


def test_current_observations_v2_returns_raw_json_strings(receipt):
    row = {"id": receipt["observation_id"], "traceId": receipt["trace_id"], "type": "TOOL",
           "name": "remote-agent-http", "environment": receipt["environment"],
           "input": json.dumps({"request": {"question": "compute"}}), "output": json.dumps({"text": "1266"})}
    assert probe.verify_observation([row], receipt) == row


def test_persisted_evidence_redacts_sdk_public_key_and_secret(monkeypatch, tmp_path):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self):
            return json.dumps({"data": [{"metadata": {"scope.attributes.public_key": "public-credential",
                                                     "nested": '{"secret": "secret-credential"}'}}], "meta": {}}).encode()
    monkeypatch.setattr(probe.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    client = probe.ReadOnlyLangfuse("https://example.test", "public-credential", "secret-credential", tmp_path)
    client.get("/api/public/v2/observations", {})
    saved = next(tmp_path.iterdir()).read_text()
    assert "public-credential" not in saved and "secret-credential" not in saved
    assert "[redacted]" in saved


def test_code_io_digests_and_missing_metadata_fail_closed(receipt):
    receipt["code_io_hashes"] = True
    rows = scores_for(receipt)
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows, receipt)
    rows[1]["metadata"].update({"input_sha256": receipt["input_digest"], "output_sha256": receipt["output_digest"]})
    assert probe.verify_scores(rows, receipt) == rows
    rows[0]["metadata"] = None
    with pytest.raises(probe.EvidenceError):
        probe.verify_scores(rows, receipt)


def test_rate_limit_retries_reads_only_and_honors_bounded_wait(monkeypatch):
    import io
    attempts, sleeps = [], []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self): return b'{"data":[],"meta":{}}'
    def open_request(req, **_):
        attempts.append(req.get_method())
        if len(attempts) == 1:
            raise probe.urllib.error.HTTPError(req.full_url, 429, "limited", {},
                                              io.BytesIO(b'{"details":{"retryAfterSeconds":7}}'))
        return Response()
    monkeypatch.setattr(probe.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: sleeps.append(seconds))
    client = probe.ReadOnlyLangfuse("https://example.test", "public-test", "secret-test")
    assert client.get("/api/public/v3/scores", {})["data"] == []
    assert attempts == ["GET", "GET"] and 8.0 in sleeps
    assert client.requests[0]["status"] == 429
