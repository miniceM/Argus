from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "argus-cli" / "src"))
from argus_cli.cli import main  # noqa: E402


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def arguments(*, command="run", **options):
    args = ["eval", command, "--policy", "production", "--policy-version", "1.0.0", "--timeout", "5"]
    if command == "run":
        args += ["--agent", "banking", "--agent-version", "v2", "--dataset", "regression",
                 "--dataset-version", "2026-10-08", "--evaluator", "intent_match@1.0.0", "--wait"]
    elif command == "wait":
        args += ["--launch-id", "launch-1"]
    for key, value in options.items():
        flag = "--" + key.replace("_", "-")
        if flag in args:
            args[args.index(flag) + 1] = str(value)
        else:
            args += [flag, str(value)]
    return args


def launch_response(status="COMPLETED"):
    return {"id": "launch-1", "status": status, "agent_id": "banking", "agent_version": "v2",
            "dataset_name": "regression", "dataset_version": "2026-10-08", "progress": {"evaluating": 0},
            "manifest": {"agent": {"agent_id": "banking", "version": "v2"},
                         "dataset": {"dataset_name": "regression", "dataset_version": "2026-10-08"},
                         "evaluators": [{"id": "intent_match", "version": "1.0.0"}],
                         "comparison": {"environment": "production"}}}


def exercise(args, *, decision="PASS", status="COMPLETED", handler=None):
    calls = []
    clock, out, err = Clock(), io.StringIO(), io.StringIO()

    def respond(request):
        calls.append(request)
        if handler:
            result = handler(request)
            if result is not None:
                return result
        if request.url.path.endswith("/release-policies"):
            return httpx.Response(200, json={"name": "production", "version": "1.0.0",
                                           "agent_id": "banking", "environment": "production"})
        if request.method == "POST" and request.url.path.endswith("/experiment-launches"):
            return httpx.Response(201, json=launch_response("PENDING"))
        if request.url.path.endswith("/run"):
            return httpx.Response(202, json={"launch_id": "launch-1", "status": "QUEUED"})
        if request.url.path.endswith("/launch-1"):
            return httpx.Response(200, json=launch_response(status))
        if request.url.path.endswith("/result-snapshots"):
            return httpx.Response(200, json={"launch_id": "launch-1", "snapshot_id": "snapshot-fixed"})
        if request.url.path.endswith("/evaluate") or "/release-gates/" in request.url.path:
            return httpx.Response(201, json={"id": "gate-1", "decision": decision,
                "releasable": decision == "PASS", "candidate_launch_id": "launch-1",
                "candidate_snapshot_id": "snapshot-fixed", "policy": {"name": "production", "version": "1.0.0"},
                "report_url": "/launches/launch-1?snapshot_id=snapshot-fixed", "rules": [], "reason_codes": []})
        raise AssertionError(f"Unexpected request {request.method} {request.url.path}")

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        code = main(args, client=client, stdout=out, stderr=err, monotonic=clock.monotonic, sleep=clock.sleep)
    return code, json.loads(out.getvalue()), err.getvalue(), calls, clock


@pytest.mark.parametrize("decision,code", [("PASS", 0), ("FAIL", 1), ("UNKNOWN", 2)])
def test_cli_waits_and_returns_gate_exit_code_and_fixed_evidence(decision, code, tmp_path):
    report = tmp_path / "evidence" / "gate.json"
    actual, result, _, calls, _ = exercise(arguments(report=report), decision=decision)
    assert actual == code
    assert result["decision"] == decision
    assert json.loads(report.read_text()) == result
    launch = next(request for request in calls if request.method == "POST" and request.url.path.endswith("/experiment-launches"))
    payload = json.loads(launch.content)
    assert payload["agent_version"] == "v2" and payload["dataset_version"] == "2026-10-08"
    assert payload["evaluator_selections"] == [{"id": "intent_match", "version": "1.0.0"}]
    assert launch.headers["Idempotency-Key"]
    gate = next(request for request in calls if request.url.path.endswith("/evaluate"))
    assert json.loads(gate.content)["candidate_snapshot_id"] == "snapshot-fixed"


def test_cli_polls_until_terminal_without_resubmitting_launch():
    statuses = iter(["RUNNING", "QUEUED", "COMPLETED"])

    def handler(request):
        if request.url.path.endswith("/launch-1"):
            return httpx.Response(200, json=launch_response(next(statuses)))

    code, _, _, calls, clock = exercise(arguments(), handler=handler)
    assert code == 0 and clock.now == 2
    assert sum(request.url.path.endswith("/run") for request in calls) == 1
    assert sum(request.method == "POST" and request.url.path.endswith("/experiment-launches") for request in calls) == 1


def test_cli_timeout_is_bounded_and_keeps_launch_id_for_recovery():
    code, result, _, calls, clock = exercise(arguments(timeout=2), status="RUNNING")
    assert code == 2 and clock.now == 2
    assert result["error_code"] == "TIMEOUT"
    assert result["launch_id"] == "launch-1"
    assert not any(request.url.path.endswith("/evaluate") for request in calls)


@pytest.mark.parametrize("flag", ["agent_version", "dataset_version", "policy_version", "evaluator"])
def test_cli_rejects_unpinned_versions_before_any_side_effect(flag):
    value = "intent_match@latest" if flag == "evaluator" else "latest"
    code, result, _, calls, _ = exercise(arguments(**{flag: value}))
    assert code == 2 and result["error_code"] == "INVALID_VERSION"
    assert calls == []


@pytest.mark.parametrize("failure", ["http", "timeout", "invalid_json"])
def test_cli_does_not_repeat_mutations_or_expose_remote_error_content(failure):
    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/experiment-launches"):
            if failure == "timeout":
                raise httpx.ReadTimeout("private-token", request=request)
            return httpx.Response(503, text="private-token") if failure == "http" else httpx.Response(201, text="private-token")

    code, result, err, calls, _ = exercise(arguments(), handler=handler)
    assert code == 2 and result["decision"] == "UNKNOWN"
    assert "private-token" not in json.dumps(result) + err
    assert sum(request.method == "POST" for request in calls) == 1
    assert result["idempotency_key"] == next(request.headers["Idempotency-Key"] for request in calls if request.method == "POST")


def test_wait_uses_existing_launch_without_creating_or_starting_one():
    code, _, _, calls, _ = exercise(arguments(command="wait"))
    assert code == 0
    assert not any(request.url.path.endswith("/run") for request in calls)
    assert not any(request.method == "POST" and request.url.path.endswith("/experiment-launches") for request in calls)


def test_result_can_read_historical_gate_and_return_its_exit_code():
    code, result, _, calls, _ = exercise(["eval", "result", "--gate-id", "gate-1"], decision="FAIL")
    assert code == 1 and result["id"] == "gate-1"
    assert len(calls) == 1 and calls[0].method == "GET"


def test_scope_mismatch_never_starts_a_new_launch():
    def handler(request):
        if request.url.path.endswith("/release-policies"):
            return httpx.Response(200, json={"name": "production", "version": "1.0.0", "agent_id": "other", "environment": "production"})

    code, result, _, calls, _ = exercise(arguments(), handler=handler)
    assert code == 2 and result["error_code"] == "POLICY_SCOPE_MISMATCH"
    assert all(request.method == "GET" for request in calls)


def test_inconsistent_pass_response_is_not_accepted_as_release_authorization():
    def handler(request):
        if request.url.path.endswith("/evaluate"):
            return httpx.Response(201, json={"decision": "PASS", "releasable": False})

    code, result, _, _, _ = exercise(arguments(), handler=handler)
    assert code == 2 and result["releasable"] is False


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1"])
def test_invalid_timeout_never_creates_launch(timeout):
    code, _, _, calls, _ = exercise(arguments(timeout=timeout))
    assert code == 2 and not calls


@pytest.mark.parametrize("passed,expected_code", [(2, 0), (1, 1)])
def test_cli_wait_against_real_api_and_database(setup_runtime, monkeypatch, passed, expected_code):
    from app import main as server
    from app.manifest import LaunchService
    from app.registry import AgentRegistry
    from fastapi.testclient import TestClient
    from test_issue_7_release_gates import policy

    monkeypatch.setattr(server, "db_manager", setup_runtime[0])
    monkeypatch.setattr(server, "orchestrator", setup_runtime[3])
    registry = AgentRegistry(setup_runtime[0])
    monkeypatch.setattr(server, "launch_service", LaunchService(setup_runtime[0], registry, runner_version="0.1.0"))
    candidate = _live_cli_snapshot(setup_runtime[0], passed=passed)
    draft = policy().model_dump()
    out, err = io.StringIO(), io.StringIO()
    with TestClient(server.app) as client:
        assert client.post("/api/v1/release-policies", json=draft).status_code == 201
        code = main(["eval", "wait", "--launch-id", candidate.launch_id,
                     "--policy", draft["name"], "--policy-version", draft["version"]],
                    client=client, stdout=out, stderr=err)
        result = json.loads(out.getvalue())
        assert code == expected_code, err.getvalue()
        assert result["candidate_snapshot_id"] == candidate.id
        assert client.get(f"/api/v1/release-gates/{result['id']}").json() == result


def test_wrong_gate_identity_is_not_accepted():
    def handler(request):
        if request.url.path.endswith("/evaluate"):
            return httpx.Response(201, json={"id": "gate-2", "decision": "PASS", "releasable": True,
                "candidate_launch_id": "other", "candidate_snapshot_id": "other",
                "policy": {"name": "production", "version": "1.0.0"}})

    code, result, _, _, _ = exercise(arguments(), handler=handler)
    assert code == 2 and result["error_code"] == "INVALID_RESPONSE"


def test_report_write_error_prevents_release_even_for_passing_gate(tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("occupied")
    code, result, _, _, _ = exercise(arguments(report=occupied / "gate.json"))
    assert code == 2 and result["error_code"] == "REPORT_WRITE_FAILED"


@pytest.mark.parametrize("api_url", ["https://user:token@example.test", "file:///tmp/api", "https://example.test?token=secret"])
def test_cli_rejects_credential_bearing_or_non_http_api_url(api_url):
    code, result, _, calls, _ = exercise(arguments(api_url=api_url))
    assert code == 2 and result["error_code"] == "INVALID_API_URL"
    assert not calls


def test_response_arriving_after_budget_never_authorizes_release():
    clock, out = Clock(), io.StringIO()

    def delayed(request):
        clock.sleep(2)
        return httpx.Response(200, json={"id": "gate-1", "decision": "PASS", "releasable": True,
                                       "candidate_launch_id": "launch-1", "candidate_snapshot_id": "snapshot-1"})

    with httpx.Client(transport=httpx.MockTransport(delayed)) as client:
        code = main(["eval", "result", "--gate-id", "gate-1", "--timeout", "1"],
                    client=client, stdout=out, stderr=io.StringIO(), monotonic=clock.monotonic)
    assert code == 2
    assert json.loads(out.getvalue())["error_code"] == "TIMEOUT"


def test_result_rejects_another_gate_id():
    def handler(request):
        return httpx.Response(200, json={"id": "other-gate", "decision": "PASS", "releasable": True,
                                       "candidate_launch_id": "launch", "candidate_snapshot_id": "snapshot"})
    code, result, _, _, _ = exercise(["eval", "result", "--gate-id", "gate-1"], handler=handler)
    assert code == 2 and result["error_code"] == "INVALID_RESPONSE"


@pytest.mark.parametrize("invalid", ["bad_float", "missing_version", "unknown_flag"])
def test_parser_errors_produce_unknown_json_and_report(tmp_path, invalid):
    report = tmp_path / "gate.json"
    args = arguments(report=report)
    if invalid == "bad_float":
        args[args.index("--timeout") + 1] = "nope"
    elif invalid == "missing_version":
        index = args.index("--policy-version")
        del args[index:index + 2]
    else:
        args += ["--private-unknown", "private-token"]
    code, result, err, calls, _ = exercise(args)
    assert code == 2 and result["error_code"] == "INVALID_ARGUMENTS"
    assert json.loads(report.read_text()) == result
    assert not calls and "private-token" not in err


def test_authenticated_cli_rejects_non_loopback_plaintext_http(monkeypatch):
    monkeypatch.setenv("ARGUS_API_TOKEN", "example-private-token")
    code, result, _, calls, _ = exercise(arguments(api_url="http://argus.internal"))
    assert code == 2 and result["error_code"] == "INSECURE_AUTH_TRANSPORT"
    assert not calls


def test_same_key_start_conflict_waits_for_the_existing_launch():
    def handler(request):
        if request.url.path.endswith("/run"):
            return httpx.Response(409, json={"detail": "another caller started the launch"})
    code, result, _, calls, _ = exercise(arguments(idempotency_key="stable-ci-key"), handler=handler)
    assert code == 0 and result["decision"] == "PASS"
    assert sum(request.url.path.endswith("/run") for request in calls) == 1


def test_start_conflict_does_not_accept_a_pending_launch():
    def handler(request):
        if request.url.path.endswith("/run"):
            return httpx.Response(409, json={"detail": "not started"})
    code, result, _, _, _ = exercise(arguments(), handler=handler, status="PENDING")
    assert code == 2 and result["error_code"] == "HTTP_409"


@pytest.mark.parametrize("problem", ["agent_version", "manifest_agent", "dataset_version", "manifest_dataset", "dataset_name", "evaluator", "extra_evaluator", "baseline"])
def test_run_rejects_a_passing_launch_for_other_frozen_inputs(problem):
    value = launch_response()
    if problem == "agent_version":
        value["agent_version"] = "v1"
    elif problem == "manifest_agent":
        value["manifest"]["agent"]["version"] = "v1"
    elif problem == "dataset_version":
        value["dataset_version"] = "other-version"
    elif problem == "manifest_dataset":
        value["manifest"]["dataset"]["dataset_version"] = "other-version"
    elif problem == "dataset_name":
        value["dataset_name"] = "other-dataset"
    elif problem == "evaluator":
        value["manifest"]["evaluators"][0]["version"] = "2.0.0"
    elif problem == "extra_evaluator":
        value["manifest"]["evaluators"].append({"id": "pii_safe", "version": "1.0.0"})
    else:
        value["manifest"]["comparison"]["baseline_snapshot_id"] = "other-baseline"
    def handler(request):
        if request.url.path.endswith("/launch-1"):
            return httpx.Response(200, json=value)
    options = {"baseline_snapshot": "requested-baseline"} if problem == "baseline" else {}
    # 创建响应与请求相符，轮询响应却错配：不能用该 Gate 放行。
    if options:
        def with_baseline(request):
            if request.method == "POST" and request.url.path.endswith("/experiment-launches"):
                created = launch_response("PENDING")
                created["manifest"]["comparison"]["baseline_snapshot_id"] = "requested-baseline"
                return httpx.Response(201, json=created)
            return handler(request)
        response_handler = with_baseline
    else:
        response_handler = handler
    code, result, _, calls, _ = exercise(arguments(**options), handler=response_handler)
    assert code == 2 and result["error_code"] == "CANDIDATE_IDENTITY_MISMATCH"
    assert not any(request.url.path.endswith("/evaluate") for request in calls)


def test_wait_ignores_the_old_snapshot_until_evaluation_only_work_finishes():
    evaluating = iter([1, 0])
    def handler(request):
        if request.url.path.endswith("/launch-1"):
            value = launch_response()
            value["progress"]["evaluating"] = next(evaluating)
            return httpx.Response(200, json=value)
    code, _, _, calls, clock = exercise(arguments(command="wait"), handler=handler)
    assert code == 0 and clock.now == 1
    assert sum(request.url.path.endswith("/launch-1") for request in calls) == 2
    assert sum(request.url.path.endswith("/result-snapshots") for request in calls) == 1


def test_launch_progress_exposes_in_flight_evaluation_only_work(setup_runtime):
    from app.db_models import ExperimentItemExecutionRecord
    from test_issue_7_release_gates import snapshot, store_snapshot
    candidate = snapshot()
    manager, _, _, orchestrator, _, _ = setup_runtime
    store_snapshot(manager, candidate)
    with manager.get_session() as session:
        session.add(ExperimentItemExecutionRecord(id="reevaluating", launch_id=candidate.launch_id, dataset_item_id="0", execution_status="succeeded", eval_status="failed", evaluation_status="evaluating"))
        session.commit()
    assert orchestrator.get_launch_progress(candidate.launch_id)["evaluating"] == 1



def test_wrong_created_launch_is_rejected_before_start():
    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/experiment-launches"):
            wrong = launch_response("PENDING")
            wrong["agent_version"] = "v1"
            return httpx.Response(201, json=wrong)
    code, result, _, calls, _ = exercise(arguments(), handler=handler)
    assert code == 2 and result["error_code"] == "CANDIDATE_IDENTITY_MISMATCH"
    assert not any(request.url.path.endswith("/run") for request in calls)


def test_terminal_launch_with_missing_evaluation_progress_never_reads_stale_summary():
    def handler(request):
        if request.url.path.endswith("/launch-1"):
            value = launch_response()
            value.pop("progress")
            return httpx.Response(200, json=value)
    code, result, _, calls, _ = exercise(arguments(command="wait"), handler=handler)
    assert code == 2 and result["error_code"] == "INVALID_RESPONSE"
    assert not any(request.url.path.endswith("/result-snapshots") for request in calls)


def test_equivalent_utc_dataset_versions_preserve_the_requested_identity():
    def handler(request):
        if request.url.path.endswith("/launch-1") or (request.method == "POST" and request.url.path.endswith("/experiment-launches")):
            value = launch_response("PENDING" if request.method == "POST" else "COMPLETED")
            value["dataset_version"] = "2026-10-08T00:00:00+00:00"
            value["manifest"]["dataset"]["dataset_version"] = value["dataset_version"]
            return httpx.Response(201 if request.method == "POST" else 200, json=value)
    code, _, _, _, _ = exercise(arguments(dataset_version="2026-10-08T00:00:00Z"), handler=handler)
    assert code == 0

def _live_cli_snapshot(manager, *, passed=2):
    from app.db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
    from app.result_snapshots import create_result_snapshot
    from test_issue_7_release_gates import snapshot

    value = snapshot(passed=passed)
    with manager.get_session() as session:
        launch = ExperimentLaunchRecord(
            id=value.launch_id, name="cli-fresh-evidence", agent_id="test-agent", agent_version="v1",
            agent_version_id="test-agent-v1", dataset_id="ds", dataset_name="ds", dataset_version="v1",
            manifest=value.manifest, status="COMPLETED",
        )
        session.add(launch)
        session.flush()
        for item in value.items:
            session.add(ExperimentItemExecutionRecord(
                id=f"{value.launch_id}-{item['dataset_item_id']}", launch_id=value.launch_id,
                dataset_item_id=item["dataset_item_id"], execution_status=item["execution_status"],
                eval_status=item["eval_status"], quality_conclusion=item["quality_conclusion"],
                quality_evaluation=item["quality_evaluation"], scores=item["scores"], evaluation_status="idle",
            ))
        session.flush()
        frozen = create_result_snapshot(session, launch)
        session.commit()
        return frozen


def test_cli_freezes_completed_retry_before_gate_and_preserves_the_previous_pass(setup_runtime, monkeypatch):
    from app import main as server
    from app.db_models import ExperimentItemExecutionRecord
    from app.manifest import LaunchService
    from app.registry import AgentRegistry
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from test_issue_7_release_gates import policy

    manager = setup_runtime[0]
    monkeypatch.setattr(server, "db_manager", manager)
    monkeypatch.setattr(server, "orchestrator", setup_runtime[3])
    monkeypatch.setattr(server, "launch_service", LaunchService(manager, AgentRegistry(manager), runner_version="0.1.0"))
    candidate = _live_cli_snapshot(manager)
    draft = policy().model_dump()
    options = ["eval", "wait", "--launch-id", candidate.launch_id,
               "--policy", draft["name"], "--policy-version", draft["version"]]
    with TestClient(server.app) as client:
        assert client.post("/api/v1/release-policies", json=draft).status_code == 201
        original = client.get(f"/api/v1/experiment-launches/{candidate.launch_id}/result-snapshots/{candidate.id}").json()
        first = io.StringIO()
        assert main(options, client=client, stdout=first, stderr=io.StringIO()) == 0, first.getvalue()
        original_gate = json.loads(first.getvalue())
        with manager.get_session() as session:
            item = session.scalar(select(ExperimentItemExecutionRecord).where(
                ExperimentItemExecutionRecord.launch_id == candidate.launch_id,
                ExperimentItemExecutionRecord.dataset_item_id == "0",
            ))
            # 评测重试已提交新结论，周期性 Reconciler 尚未冻结新 Snapshot。
            item.quality_conclusion = "fail"
            item.quality_evaluation = {"rules": [{"critical": False, "conclusion": "fail"}]}
            item.scores = {"intent_match": 0.0}
            item.evaluation_status = "recovered"
            session.commit()
        changed = io.StringIO()
        assert main(options, client=client, stdout=changed, stderr=io.StringIO()) == 1
        new_gate = json.loads(changed.getvalue())
        assert new_gate["decision"] == "FAIL"
        assert new_gate["candidate_snapshot_id"] != candidate.id
        assert client.get(f"/api/v1/experiment-launches/{candidate.launch_id}/result-snapshots/{candidate.id}").json() == original
        assert client.get(f"/api/v1/release-gates/{original_gate['id']}").json() == original_gate
        unchanged = io.StringIO()
        assert main(options, client=client, stdout=unchanged, stderr=io.StringIO()) == 1
        assert json.loads(unchanged.getvalue())["id"] == new_gate["id"]


@pytest.mark.parametrize("in_flight", ["evaluation", "execution"])
def test_refresh_never_falls_back_to_an_old_snapshot_during_work(setup_runtime, monkeypatch, in_flight):
    from app import main as server
    from app.db_models import ExperimentItemExecutionRecord
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    manager = setup_runtime[0]
    monkeypatch.setattr(server, "db_manager", manager)
    monkeypatch.setattr(server, "orchestrator", setup_runtime[3])
    candidate = _live_cli_snapshot(manager)
    with manager.get_session() as session:
        item = session.scalar(select(ExperimentItemExecutionRecord).where(
            ExperimentItemExecutionRecord.launch_id == candidate.launch_id,
        ))
        if in_flight == "evaluation":
            item.evaluation_status = "evaluating"
        else:
            item.execution_status = "running"
        session.commit()
    with TestClient(server.app) as client:
        path = f"/api/v1/experiment-launches/{candidate.launch_id}/summary"
        assert client.post(f"/api/v1/experiment-launches/{candidate.launch_id}/result-snapshots").status_code == 409
        pinned = client.get(path, params={"snapshot_id": candidate.id})
        assert pinned.status_code == 200
        assert pinned.json()["snapshot_id"] == candidate.id
        assert pinned.json()["summary"]["pass_rate"] == 1.0


def test_old_control_plane_without_explicit_freeze_support_cannot_release():
    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/result-snapshots"):
            return httpx.Response(405)
        return None

    code, report, _, calls, _ = exercise(arguments(), handler=handler)
    assert code == 2
    assert report["error_code"] == "HTTP_405"
    assert not any(request.url.path.endswith("/evaluate") for request in calls)


def test_overlong_idempotency_key_is_unknown_before_any_http_request():
    code, report, _, calls, _ = exercise(arguments(idempotency_key='x'*129))
    assert code == 2
    assert report['error_code'] == 'INVALID_IDEMPOTENCY_KEY'
    assert calls == []


def test_idempotency_key_at_database_limit_is_preserved():
    key = 'x'*128
    code, _, _, calls, _ = exercise(arguments(idempotency_key=key))
    assert code == 0
    create = next(request for request in calls if request.method == 'POST' and request.url.path.endswith('/experiment-launches'))
    assert create.headers['Idempotency-Key'] == key
