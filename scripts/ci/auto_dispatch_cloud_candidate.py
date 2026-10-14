"""Dispatch Cloud candidates automatically from completed, trusted PR CI events.

Only the default-branch Cloud Integration Policy workflow invokes this controller.
No PR checkout or Langfuse credentials are required by the dispatcher.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from scripts.ci.ci_constants import CANDIDATE_WORKFLOW, REPOSITORY
from scripts.ci.cloud_e2e_receipt import classify_pr_risk, find_current_ci_run
from scripts.ci.github_api import GitHubApiError, GitHubClient
from scripts.ci.publish_cloud_policy import _parse_event

CI_PATH = ".github/workflows/ci.yml"


def dispatch_for_completed_ci(client: GitHubClient, event: Mapping[str, Any]) -> list[int]:
    """Start a candidate only for the exact current same-repo high-risk PR CI."""
    run = event.get("workflow_run")
    if event.get("action") != "completed" or not isinstance(run, dict):
        return []
    if (
        run.get("name") != "CI"
        or run.get("event") != "pull_request"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or str(run.get("path", "")).lstrip("/") != CI_PATH
        or (run.get("head_repository") or {}).get("full_name") != REPOSITORY
    ):
        return []
    number = run.get("id")
    attempt = run.get("run_attempt")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return []
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        return []
    related = run.get("pull_requests")
    if not isinstance(related, list) or len(related) != 1 or not isinstance(related[0], dict):
        return []
    pr_number = related[0].get("number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return []

    pr = client.get_pull_request(pr_number)
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    if (
        pr.get("state") != "open"
        or base.get("ref") != "main"
        or (head.get("repo") or {}).get("full_name") != REPOSITORY
        or not isinstance(head.get("sha"), str)
        or not isinstance(base.get("sha"), str)
        or len(head["sha"]) != 40
        or len(base["sha"]) != 40
    ):
        return []
    main_sha = client.get_ref("heads/main").get("object", {}).get("sha")
    if base["sha"] != main_sha:
        return []

    # This repeats the same API-backed current-identity/required-job validation
    # used in the frozen candidate manifest. Never trust the webhook payload
    # alone or dispatch a stale CI rerun after a PR/head/base transition.
    current = find_current_ci_run(client, pr)
    if (current.get("id"), current.get("run_attempt")) != (number, attempt):
        return []
    risk = classify_pr_risk(client, pr)
    if risk.get("risk") != "high":
        return []

    workflow_id = client.get_workflow_id(CANDIDATE_WORKFLOW)
    client.request_json(
        f"{client.repo_path}/actions/workflows/{workflow_id}/dispatches",
        method="POST",
        body={
            "ref": "main",
            "inputs": {
                "pr_number": str(pr_number),
                "expected_head_sha": head["sha"],
                "expected_base_sha": base["sha"],
            },
        },
    )
    return [pr_number]


def main() -> int:
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        print("Automatic Cloud dispatcher: unexpected repository", file=sys.stderr)
        return 1
    try:
        event = _parse_event(os.environ["GITHUB_EVENT_PATH"])
        client = GitHubClient(
            api_url=os.environ["GITHUB_API_URL"],
            repository=REPOSITORY,
            token=os.environ["GITHUB_TOKEN"],
        )
        dispatched = dispatch_for_completed_ci(client, event)
    except (GitHubApiError, KeyError, ValueError, TypeError) as exc:
        print(f"Automatic Cloud dispatcher failed closed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"Automatically dispatched trusted Cloud validation for PRs: {dispatched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
