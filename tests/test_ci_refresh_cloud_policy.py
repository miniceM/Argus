"""Contract tests for the completion-triggered Cloud policy refresh command."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.ci.refresh_cloud_policy import PolicyRefreshError, dispatch_policy_refresh


class _Client:
    repo_path = "repos/miniceM/Argus"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_json(self, path: str, *, method: str, body: dict[str, Any]) -> None:
        self.calls.append((path, method, body))


def test_final_candidate_callback_explicitly_dispatches_exact_policy_refresh() -> None:
    client = _Client()
    dispatch_policy_refresh(
        client, repository="miniceM/Argus", pr_number="190",
        run_id="38559776207", run_attempt="1",
    )
    assert client.calls == [
        (
            "repos/miniceM/Argus/actions/workflows/cloud-integration-policy.yml/dispatches",
            "POST",
            {
                "ref": "main",
                "inputs": {
                    "pr_number": "190",
                    "candidate_run_id": "38559776207",
                    "candidate_run_attempt": "1",
                },
            },
        ),
    ]


@pytest.mark.parametrize(
    ("repository", "pr_number", "run_id", "run_attempt"),
    [
        ("some/fork", "190", "120", "1"),
        ("miniceM/Argus", "0", "120", "1"),
        ("miniceM/Argus", "abc", "120", "1"),
        ("miniceM/Argus", "190", "-1", "1"),
        ("miniceM/Argus", "190", "120", "0"),
        ("miniceM/Argus", "190", "120", "not-a-number"),
    ],
)
def test_callback_rejects_malformed_or_untrusted_identity(
    repository: str, pr_number: str, run_id: str, run_attempt: str
) -> None:
    client = _Client()
    with pytest.raises(PolicyRefreshError):
        dispatch_policy_refresh(
            client, repository=repository, pr_number=pr_number,
            run_id=run_id, run_attempt=run_attempt,
        )
    assert client.calls == []
