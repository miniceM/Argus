"""Automatic Cloud dispatch only accepts exact, successful same-repo PR CI."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scripts.ci import auto_dispatch_cloud_candidate as auto

HEAD = "1" * 40
BASE = "2" * 40
PR = 190


class _Client:
    repo_path = "repos/miniceM/Argus"

    def __init__(self) -> None:
        self.pr = {
            "number": PR,
            "state": "open",
            "head": {"sha": HEAD, "repo": {"full_name": "miniceM/Argus"}},
            "base": {"sha": BASE, "ref": "main"},
        }
        self.main = BASE
        self.dispatched: list[tuple[str, str, dict[str, Any]]] = []

    def get_pull_request(self, number: int) -> dict[str, Any]:
        assert number == PR
        return self.pr

    def get_ref(self, name: str) -> dict[str, Any]:
        assert name == "heads/main"
        return {"object": {"sha": self.main}}

    def get_workflow_id(self, path: str) -> int:
        assert path == ".github/workflows/cloud-e2e-trusted.yml"
        return 383177300

    def request_json(self, path: str, *, method: str, body: dict[str, Any]) -> None:
        self.dispatched.append((path, method, body))


@pytest.fixture
def event() -> dict[str, Any]:
    return {
        "action": "completed",
        "workflow_run": {
            "id": 5001,
            "run_attempt": 1,
            "name": "CI",
            "event": "pull_request",
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "head_repository": {"full_name": "miniceM/Argus"},
            "pull_requests": [{"number": PR}],
        },
    }


def test_dispatch_exact_high_risk_current_ci(
    event: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client()
    monkeypatch.setattr(auto, "find_current_ci_run", lambda c, p: {"id": 5001, "run_attempt": 1})
    monkeypatch.setattr(auto, "classify_pr_risk", lambda c, p: {"risk": "high"})
    assert auto.dispatch_for_completed_ci(client, event) == [PR]
    assert client.dispatched == [(
        "repos/miniceM/Argus/actions/workflows/383177300/dispatches",
        "POST",
        {
            "ref": "main",
            "inputs": {"pr_number": "190", "expected_head_sha": HEAD, "expected_base_sha": BASE},
        },
    )]


@pytest.mark.parametrize("mutation", [
    lambda e: e.update(action="requested"),
    lambda e: e["workflow_run"].update(conclusion="failure"),
    lambda e: e["workflow_run"].update(status="in_progress"),
    lambda e: e["workflow_run"].update(event="push"),
    lambda e: e["workflow_run"].update(name="Fake CI"),
    lambda e: e["workflow_run"].update(path=".github/workflows/fake.yml"),
    lambda e: e["workflow_run"].update(head_repository={"full_name": "outside/fork"}),
    lambda e: e["workflow_run"].update(pull_requests=[]),
    lambda e: e["workflow_run"].update(pull_requests=[{"number": PR}, {"number": 191}]),
    lambda e: e["workflow_run"].update(run_attempt=True),
])
def test_dispatch_rejects_untrusted_ci_event(
    event: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mutation: Any
) -> None:
    client = _Client()
    monkeypatch.setattr(auto, "find_current_ci_run", lambda c, p: {"id": 5001, "run_attempt": 1})
    monkeypatch.setattr(auto, "classify_pr_risk", lambda c, p: {"risk": "high"})
    updated = deepcopy(event)
    mutation(updated)
    assert auto.dispatch_for_completed_ci(client, updated) == []
    assert client.dispatched == []


@pytest.mark.parametrize("change", ["fork", "closed", "base_moved", "ci_rerun", "low_risk"])
def test_dispatch_rejects_stale_or_ineligible_pr(
    event: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    client = _Client()
    ci = {"id": 5001, "run_attempt": 1}
    risk = {"risk": "high"}
    if change == "fork":
        client.pr["head"]["repo"]["full_name"] = "outside/fork"
    elif change == "closed":
        client.pr["state"] = "closed"
    elif change == "base_moved":
        client.main = "3" * 40
    elif change == "ci_rerun":
        ci = {"id": 5001, "run_attempt": 2}
    elif change == "low_risk":
        risk = {"risk": "low"}
    monkeypatch.setattr(auto, "find_current_ci_run", lambda c, p: ci)
    monkeypatch.setattr(auto, "classify_pr_risk", lambda c, p: risk)
    assert auto.dispatch_for_completed_ci(client, event) == []
    assert client.dispatched == []
