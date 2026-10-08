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
            return httpx.Response(201, json={"id": "launch-1", "status": "PENDING"})
        if request.url.path.endswith("/run"):
            return httpx.Response(202, json={"launch_id": "launch-1", "status": "QUEUED"})
        if request.url.path.endswith("/launch-1"):
            return httpx.Response(200, json={"id": "launch-1", "status": status,
                                           "agent_id": "banking", "manifest": {"comparison": {"environment": "production"}}})
        if request.url.path.endswith("/summary"):
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
            return httpx.Response(200, json={"id": "launch-1", "status": next(statuses),
                                           "agent_id": "banking", "manifest": {"comparison": {"environment": "production"}}})

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
    from test_issue_7_release_gates import policy, snapshot, store_snapshot

    monkeypatch.setattr(server, "db_manager", setup_runtime[0])
    monkeypatch.setattr(server, "orchestrator", setup_runtime[3])
    registry = AgentRegistry(setup_runtime[0])
    monkeypatch.setattr(server, "launch_service", LaunchService(setup_runtime[0], registry, runner_version="0.1.0"))
    candidate = snapshot(passed=passed)
    store_snapshot(setup_runtime[0], candidate)
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
