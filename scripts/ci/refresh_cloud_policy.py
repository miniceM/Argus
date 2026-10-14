"""Notify the trusted default-branch policy after a Cloud candidate attempt.

Called only by the main-controlled candidate's isolated final callback job.
Uses Actions dispatch only; never checks out PR code or accesses Cloud secrets.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from scripts.ci.ci_constants import REPOSITORY
from scripts.ci.github_api import GitHubApiError, GitHubClient


class PolicyRefreshError(RuntimeError):
    """An automatic Cloud policy callback is invalid."""


def _positive_number(value: str) -> bool:
    return value.isdecimal() and int(value) > 0


def dispatch_policy_refresh(
    client: Any, *, repository: str, pr_number: str, run_id: str, run_attempt: str
) -> None:
    if repository != REPOSITORY or any(
        not _positive_number(value) for value in (pr_number, run_id, run_attempt)
    ):
        raise PolicyRefreshError("Cloud policy refresh requires trusted repository and positive IDs")
    client.request_json(
        f"{client.repo_path}/actions/workflows/cloud-integration-policy.yml/dispatches",
        method="POST",
        body={
            "ref": "main",
            "inputs": {
                "pr_number": pr_number,
                "candidate_run_id": run_id,
                "candidate_run_attempt": run_attempt,
            },
        },
    )


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        client = GitHubClient(
            api_url=os.environ["GITHUB_API_URL"],
            repository=repository,
            token=os.environ["GITHUB_TOKEN"],
        )
        dispatch_policy_refresh(
            client,
            repository=repository,
            pr_number=os.environ["PR_NUMBER"],
            run_id=os.environ["CANDIDATE_RUN_ID"],
            run_attempt=os.environ["CANDIDATE_RUN_ATTEMPT"],
        )
    except (PolicyRefreshError, GitHubApiError, KeyError, ValueError) as exc:
        print(f"Automatic Cloud policy refresh failed closed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Dispatched trusted Cloud policy refresh for the current candidate run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
