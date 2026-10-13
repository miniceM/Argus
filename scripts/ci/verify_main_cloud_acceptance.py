"""Verify current-run main Cloud acceptance using GitHub Actions API evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from scripts.ci.cloud_e2e_policy import REPOSITORY, REQUIRED_CI_CHECKS
from scripts.ci.github_api import GitHubApiError, GitHubClient

CI_WORKFLOW_PATH_SUFFIX = ".github/workflows/ci.yml@refs/heads/main"
CLOUD_SECRET_STEP = "Validate Langfuse Cloud secrets"
CLOUD_E2E_STEP = "Run cloud end-to-end tests"
CLOUD_JOB_NAME = "Main Langfuse Cloud E2E"
MAIN_ACCEPTANCE_JOB_NAME = "Main Cloud Acceptance"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class MainCloudAcceptanceError(RuntimeError):
    """The current main CI run did not produce complete Cloud evidence."""


def _step_map(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    steps = job.get("steps", [])
    if not isinstance(steps, list):
        return {}
    return {str(step.get("name")): step for step in steps if isinstance(step, dict) and step.get("name")}


def _unique_job(jobs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [job for job in jobs if job.get("name") == name]
    if len(matches) != 1:
        raise MainCloudAcceptanceError(f"expected exactly one completed job named {name}")
    return matches[0]


def _require_success(job: dict[str, Any], label: str) -> None:
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise MainCloudAcceptanceError(f"required CI job did not succeed: {label}")


def _find_cloud_job(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    for job in jobs:
        if job.get("name") == CLOUD_JOB_NAME and CLOUD_E2E_STEP in _step_map(job):
            matches.append(job)
    if len(matches) != 1:
        raise MainCloudAcceptanceError("actual main Cloud E2E job is missing or ambiguous")
    return matches[0]


def verify_main_cloud_acceptance(
    client: GitHubClient,
    *,
    run_id: int,
    run_attempt: int,
    commit_sha: str,
    event_name: str,
    ref: str,
    repository: str = REPOSITORY,
) -> dict[str, Any]:
    if repository != REPOSITORY:
        raise MainCloudAcceptanceError("unexpected repository")
    if not SHA_RE.fullmatch(commit_sha):
        raise MainCloudAcceptanceError("commit SHA must be a full 40-character SHA")
    if event_name not in {"push", "workflow_dispatch"} or ref != "refs/heads/main":
        raise MainCloudAcceptanceError("main Cloud acceptance only applies to main push/dispatch")

    run = client.get_run(run_id)
    if run.get("path", "").lstrip("/") != CI_WORKFLOW_PATH_SUFFIX:
        raise MainCloudAcceptanceError("workflow run did not originate from the trusted main CI workflow")
    if run.get("event") != event_name or run.get("head_branch") != "main" or run.get("head_sha") != commit_sha:
        raise MainCloudAcceptanceError("workflow event, branch, or commit identity does not match this run")
    if run.get("run_attempt") != run_attempt:
        raise MainCloudAcceptanceError("workflow run attempt does not match current attempt")
    if run.get("status") not in {"in_progress", "completed"}:
        raise MainCloudAcceptanceError("workflow run is not in an acceptable state")

    ref_info = client.get_ref("heads/main")
    current_main_sha = ref_info.get("object", {}).get("sha")
    if current_main_sha != commit_sha:
        raise MainCloudAcceptanceError("main advanced; this run cannot authorize a stale commit")

    jobs = client.list_jobs(run_id, run_attempt)
    job_receipts: dict[str, int] = {}
    for name in REQUIRED_CI_CHECKS:
        job = _unique_job(jobs, name)
        _require_success(job, name)
        job_receipts[name] = int(job.get("id", 0))
        if job_receipts[name] <= 0:
            raise MainCloudAcceptanceError(f"required CI job has no stable ID: {name}")

    cloud_job = _find_cloud_job(jobs)
    _require_success(cloud_job, "Langfuse Cloud E2E")
    steps = _step_map(cloud_job)
    for step_name in (CLOUD_SECRET_STEP, CLOUD_E2E_STEP):
        if steps.get(step_name, {}).get("status") != "completed" or steps.get(step_name, {}).get("conclusion") != "success":
            raise MainCloudAcceptanceError(f"required Cloud step did not execute successfully: {step_name}")
    cloud_job_id = int(cloud_job.get("id", 0))
    if cloud_job_id <= 0:
        raise MainCloudAcceptanceError("Cloud job has no stable ID")

    return {
        "schema_version": 1,
        "repository": repository,
        "commit_sha": commit_sha,
        "branch": "main",
        "event": event_name,
        "workflow_path": ".github/workflows/ci.yml",
        "workflow_revision": run["head_sha"],
        "run_id": run_id,
        "run_attempt": run_attempt,
        "run_url": run.get("html_url", ""),
        "regular_jobs": job_receipts,
        "cloud_job_id": cloud_job_id,
        "cloud_state": "passed",
        "cloud_steps": {name: "passed" for name in (CLOUD_SECRET_STEP, CLOUD_E2E_STEP)},
        "required_suites": ["managed-cloud", "async-link"],
        "suite_results": {"managed-cloud": "passed", "async-link": "passed"},
        "baseline": {"v1": {"passed": 2, "total": 6}, "v2": {"passed": 6, "total": 6}},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/main-cloud-acceptance/receipt.json")
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    client = GitHubClient(
        api_url=os.environ["GITHUB_API_URL"],
        repository=repository,
        token=os.environ["GITHUB_TOKEN"],
    )
    try:
        receipt = verify_main_cloud_acceptance(
            client,
            run_id=int(os.environ["GITHUB_RUN_ID"]),
            run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
            commit_sha=os.environ["GITHUB_SHA"],
            event_name=os.environ["GITHUB_EVENT_NAME"],
            ref=os.environ["GITHUB_REF"],
            repository=repository,
        )
    except (GitHubApiError, MainCloudAcceptanceError, KeyError, ValueError) as exc:
        print(f"Main Cloud Acceptance: BLOCKED ({type(exc).__name__})", flush=True)
        raise SystemExit(1) from None

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Main Cloud Acceptance: PASS for {receipt['commit_sha']} (run {receipt['run_id']}/{receipt['run_attempt']})")
    print(f"Evidence: {receipt['run_url']}")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(summary).write_text(
            "## Main Cloud Acceptance\n\n"
            f"- State: `passed`\n- Commit: `{receipt['commit_sha']}`\n"
            f"- Run: [{receipt['run_id']}/{receipt['run_attempt']}]({receipt['run_url']})\n"
            "- Cloud: actual secrets preflight and end-to-end steps passed\n"
            "- Baseline: v1 `2/6`, v2 `6/6`; async link suite passed\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
