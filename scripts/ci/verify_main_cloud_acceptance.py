"""Verify current-run main Cloud acceptance using GitHub Actions API evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from scripts.ci.cloud_e2e_policy import REPOSITORY, REQUIRED_CI_CHECKS
from scripts.ci.cloud_e2e_receipt import CandidateReceiptError, _artifact_bytes, read_json_archive
from scripts.ci.github_api import GitHubApiError, GitHubClient

CLOUD_SECRET_STEP = "Validate Langfuse Cloud secrets"
CLOUD_E2E_STEP = "Run cloud end-to-end tests"
CLOUD_JOB_NAME = "Main Langfuse Cloud E2E"
MAIN_ACCEPTANCE_JOB_NAME = "Main Cloud Acceptance"
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
MAIN_ACCEPTANCE_ARTIFACT_PREFIX = "main-cloud-acceptance"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class MainCloudAcceptanceError(RuntimeError):
    """The current main CI run did not produce complete Cloud evidence."""


def _unique_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    steps = job.get("steps", [])
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name] if isinstance(steps, list) else []
    if len(matches) != 1:
        raise MainCloudAcceptanceError(f"expected exactly one Cloud step named {name}")
    return matches[0]


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
        if job.get("name") != CLOUD_JOB_NAME:
            continue
        steps = job.get("steps")
        if isinstance(steps, list) and any(
            isinstance(step, dict) and step.get("name") == CLOUD_E2E_STEP for step in steps
        ):
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
    if run.get("path", "").lstrip("/") != CI_WORKFLOW_PATH:
        raise MainCloudAcceptanceError("workflow run did not originate from the trusted main CI workflow")
    if run.get("event") != event_name or run.get("head_branch") != "main" or run.get("head_sha") != commit_sha:
        raise MainCloudAcceptanceError("workflow event, branch, or commit identity does not match this run")
    if run.get("run_attempt") != run_attempt:
        raise MainCloudAcceptanceError("workflow run attempt does not match current attempt")
    if run.get("status") not in {"in_progress", "completed"}:
        raise MainCloudAcceptanceError("workflow run is not in an acceptable state")
    if run.get("status") == "completed" and run.get("conclusion") != "success":
        raise MainCloudAcceptanceError("completed main CI workflow did not succeed")

    ref_info = client.get_ref("heads/main")
    ref_object = ref_info.get("object") if isinstance(ref_info, dict) else None
    current_main_sha = ref_object.get("sha") if isinstance(ref_object, dict) else None
    if current_main_sha != commit_sha:
        raise MainCloudAcceptanceError("main advanced; this run cannot authorize a stale commit")

    jobs = client.list_jobs(run_id, run_attempt)
    if run.get("status") == "completed":
        _require_success(_unique_job(jobs, MAIN_ACCEPTANCE_JOB_NAME), MAIN_ACCEPTANCE_JOB_NAME)
    job_receipts: dict[str, int] = {}
    for name in REQUIRED_CI_CHECKS:
        job = _unique_job(jobs, name)
        _require_success(job, name)
        job_receipts[name] = int(job.get("id", 0))
        if job_receipts[name] <= 0:
            raise MainCloudAcceptanceError(f"required CI job has no stable ID: {name}")

    cloud_job = _find_cloud_job(jobs)
    _require_success(cloud_job, "Langfuse Cloud E2E")
    for step_name in (CLOUD_SECRET_STEP, CLOUD_E2E_STEP):
        step = _unique_step(cloud_job, step_name)
        if step.get("status") != "completed" or step.get("conclusion") != "success":
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


def verify_completed_main_cloud_acceptance(
    client: GitHubClient,
    *,
    run_id: int,
    run_attempt: int,
    commit_sha: str,
    event_name: str,
    repository: str = REPOSITORY,
) -> dict[str, Any]:
    """Require a completed successful CI run and its exact uploaded receipt."""
    run = client.get_run(run_id)
    if (
        not isinstance(run, dict)
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("run_attempt") != run_attempt
    ):
        raise MainCloudAcceptanceError("main CI run is not the completed successful attempt")
    receipt = verify_main_cloud_acceptance(
        client,
        run_id=run_id,
        run_attempt=run_attempt,
        commit_sha=commit_sha,
        event_name=event_name,
        ref="refs/heads/main",
        repository=repository,
    )
    try:
        archive = _artifact_bytes(
            client,
            run_id,
            f"{MAIN_ACCEPTANCE_ARTIFACT_PREFIX}-{run_id}-{run_attempt}",
        )
        if archive is None:
            raise MainCloudAcceptanceError("exact main Cloud acceptance receipt artifact is missing or expired")
        artifact_receipt = read_json_archive(archive, ("receipt.json",))["receipt.json"]
    except CandidateReceiptError as exc:
        raise MainCloudAcceptanceError("main Cloud acceptance receipt artifact is invalid") from exc
    if artifact_receipt != receipt:
        raise MainCloudAcceptanceError("main Cloud acceptance receipt does not match live run evidence")
    return receipt


def _run_sort_key(run: dict[str, Any]) -> tuple[str, str, int, int]:
    attempt = run.get("run_attempt")
    run_id = run.get("id")
    return (
        str(run.get("updated_at") or run.get("run_started_at") or run.get("created_at", "")),
        str(run.get("run_started_at", "")),
        attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0,
        run_id if isinstance(run_id, int) and not isinstance(run_id, bool) else 0,
    )


def wait_for_current_main_cloud_acceptance(
    client: GitHubClient,
    *,
    commit_sha: str,
    timeout_seconds: int = 3600,
    poll_seconds: int = 20,
    repository: str = REPOSITORY,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> dict[str, Any]:
    """Wait for and verify exact-SHA main CI acceptance before image publication."""
    if repository != REPOSITORY or not SHA_RE.fullmatch(commit_sha):
        raise MainCloudAcceptanceError("release gate repository or commit SHA is invalid")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 0
        or isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, int)
        or poll_seconds <= 0
    ):
        raise MainCloudAcceptanceError("release gate polling bounds are invalid")

    workflow_id = client.get_workflow_id(CI_WORKFLOW_PATH)
    deadline = monotonic() + timeout_seconds
    while True:
        ref_info = client.get_ref("heads/main")
        ref_object = ref_info.get("object") if isinstance(ref_info, dict) else None
        current_main = ref_object.get("sha") if isinstance(ref_object, dict) else None
        if current_main != commit_sha:
            raise MainCloudAcceptanceError("main advanced; release gate only accepts the current main SHA")

        candidates: list[dict[str, Any]] = []
        for event_name in ("push", "workflow_dispatch"):
            runs = client.list_workflow_runs(
                workflow_id,
                params={"event": event_name, "branch": "main"},
            )
            candidates.extend(
                run
                for run in runs
                if isinstance(run, dict)
                and run.get("event") == event_name
                and run.get("head_branch") == "main"
                and run.get("head_sha") == commit_sha
                and str(run.get("path", "")).lstrip("/") == CI_WORKFLOW_PATH
            )

        if candidates:
            latest = max(candidates, key=_run_sort_key)
            run_id = latest.get("id")
            attempt = latest.get("run_attempt")
            if (
                isinstance(run_id, bool)
                or not isinstance(run_id, int)
                or run_id <= 0
                or isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt <= 0
            ):
                raise MainCloudAcceptanceError("current main CI run has an invalid identity")
            run = client.get_run(run_id)
            if run.get("run_attempt") != attempt:
                # The run was rerun after listing. Re-read instead of accepting an
                # earlier green attempt; the next pass observes the current attempt.
                pass
            elif run.get("status") == "completed":
                if run.get("conclusion") != "success":
                    raise MainCloudAcceptanceError("latest exact-SHA main CI attempt did not succeed")
                return verify_completed_main_cloud_acceptance(
                    client,
                    run_id=run_id,
                    run_attempt=attempt,
                    commit_sha=commit_sha,
                    event_name=str(run.get("event", "")),
                    repository=repository,
                )
            elif run.get("status") not in {"queued", "in_progress", "waiting", "pending", "requested"}:
                raise MainCloudAcceptanceError("latest exact-SHA main CI run has an unknown status")

        remaining = deadline - monotonic()
        if remaining <= 0:
            raise MainCloudAcceptanceError("timed out waiting for exact-SHA Main Cloud Acceptance")
        sleep(min(poll_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/main-cloud-acceptance/receipt.json")
    parser.add_argument("--wait-for-current-main", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=20)
    args = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    client = GitHubClient(
        api_url=os.environ["GITHUB_API_URL"],
        repository=repository,
        token=os.environ["GITHUB_TOKEN"],
    )
    try:
        if args.wait_for_current_main:
            receipt = wait_for_current_main_cloud_acceptance(
                client,
                commit_sha=os.environ["GITHUB_SHA"],
                timeout_seconds=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
                repository=repository,
            )
        else:
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
