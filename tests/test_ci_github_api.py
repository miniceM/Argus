from __future__ import annotations

from typing import Any

import pytest

from scripts.ci.github_api import GitHubApiError, GitHubClient


def _client() -> GitHubClient:
    return GitHubClient(
        api_url="https://api.github.com",
        repository="miniceM/Argus",
        token="test-token",
    )


def test_get_workflow_id_resolves_exact_active_workflow_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls: list[tuple[str, str | None]] = []

    def paginate(path: str, *, array_key: str | None = None, **_kwargs: Any) -> list[dict[str, Any]]:
        calls.append((path, array_key))
        return [
            {"id": 12, "path": ".github/workflows/ci.yml", "state": "active"},
            {"id": 13, "path": ".github/workflows/other.yml", "state": "active"},
        ]

    monkeypatch.setattr(client, "paginate", paginate)

    assert client.get_workflow_id(".github/workflows/ci.yml") == 12
    assert calls == [("repos/miniceM/Argus/actions/workflows", "workflows")]


def test_get_workflow_id_fails_closed_for_duplicate_or_inactive_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    workflows = [
        {"id": 12, "path": ".github/workflows/ci.yml", "state": "active"},
        {"id": 13, "path": ".github/workflows/ci.yml", "state": "active"},
    ]
    monkeypatch.setattr(client, "paginate", lambda *_args, **_kwargs: workflows)
    with pytest.raises(GitHubApiError, match="exactly one"):
        client.get_workflow_id(".github/workflows/ci.yml")

    workflows[:] = [{"id": 12, "path": ".github/workflows/ci.yml", "state": "disabled_manually"}]
    with pytest.raises(GitHubApiError, match="not active"):
        client.get_workflow_id(".github/workflows/ci.yml")


def test_list_workflow_runs_uses_resolved_numeric_workflow_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls: list[tuple[str, dict[str, str] | None]] = []

    def paginate(path: str, *, array_key: str | None = None, params: dict[str, str] | None = None, **_kwargs: Any) -> list[dict[str, Any]]:
        assert array_key == "workflow_runs"
        calls.append((path, params))
        return [{"id": 5}]

    monkeypatch.setattr(client, "paginate", paginate)

    assert client.list_workflow_runs(12, params={"event": "pull_request"}) == [{"id": 5}]
    assert calls == [("repos/miniceM/Argus/actions/workflows/12/runs", {"event": "pull_request"})]


def test_list_workflow_runs_rejects_invalid_identifiers() -> None:
    with pytest.raises(ValueError, match="identifier"):
        _client().list_workflow_runs(0)
    with pytest.raises(ValueError, match="identifier"):
        _client().list_workflow_runs(True)


def test_list_open_pull_requests_paginates_and_filters_base_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    calls: list[tuple[str, dict[str, str] | None]] = []

    def paginate(path: str, *, params: dict[str, str] | None = None, **_kwargs: Any) -> list[dict[str, Any]]:
        calls.append((path, params))
        return [
            {"number": 12, "state": "open", "base": {"ref": "main"}},
            {"number": 13, "state": "closed", "base": {"ref": "main"}},
            {"number": 14, "state": "open", "base": {"ref": "release"}},
        ]

    monkeypatch.setattr(client, "paginate", paginate)

    assert client.list_open_pull_requests(base="main") == [
        {"number": 12, "state": "open", "base": {"ref": "main"}}
    ]
    assert calls == [(
        "repos/miniceM/Argus/pulls",
        {"state": "open", "base": "main", "sort": "updated", "direction": "desc"},
    )]


def test_list_open_pull_requests_fails_closed_on_malformed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    monkeypatch.setattr(client, "paginate", lambda *_args, **_kwargs: [{"number": True, "state": "open"}])
    with pytest.raises(GitHubApiError, match="invalid number"):
        client.list_open_pull_requests()
    with pytest.raises(ValueError, match="base branch"):
        client.list_open_pull_requests(base="\n")
