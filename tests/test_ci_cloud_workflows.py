from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _workflow(path: str) -> dict:
    document = yaml.safe_load((ROOT / path).read_text())
    return document.get("on", document.get(True)) and document


def _action(step: dict) -> tuple[str, str]:
    owner_repo, _, ref = step["uses"].partition("@")
    return owner_repo, ref


def _is_sha(ref: str) -> bool:
    return len(ref) == 40 and all(char in "0123456789abcdef" for char in ref)


def test_ci_has_no_secret_bearing_cloud_job_for_pull_requests() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    jobs = workflow["jobs"]
    pr_job = jobs["cloud-e2e-policy"]
    main_job = jobs["cloud-e2e-main"]

    assert pr_job["name"] == "Langfuse Cloud E2E"
    assert "pull_request" in pr_job["if"]
    assert "environment" not in pr_job
    assert "${{ secrets." not in str(pr_job)
    assert "checkout@" not in str(pr_job)
    assert "skipped_by_policy" in str(pr_job)

    assert main_job["name"] == "Main Langfuse Cloud E2E"
    assert "refs/heads/main" in main_job["if"]
    assert "head.repo.full_name" not in main_job["if"]
    assert main_job["environment"] == "langfuse-e2e"
    assert "LANGFUSE_E2E_ENABLED" not in main_job["if"]
    assert "env" not in main_job or not any("${{ secrets." in str(value) for value in main_job["env"].values())
    assert main_job["concurrency"]["cancel-in-progress"] is False

    secret_steps = [
        step for step in main_job["steps"]
        if any("${{ secrets." in str(value) for value in step.get("env", {}).values())
    ]
    assert secret_steps
    assert all("if" in step and "refs/heads/main" in str(step["if"]) for step in secret_steps)
    assert any(step.get("name") == "Validate Langfuse Cloud secrets" for step in secret_steps)
    assert not any(step.get("continue-on-error") for step in main_job["steps"])


def test_main_cloud_acceptance_aggregates_real_jobs_and_actual_cloud_steps() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    job = workflow["jobs"]["main-cloud-acceptance"]
    assert job["name"] == "Main Cloud Acceptance"
    assert "always()" in job["if"]
    assert all(name in job["needs"] for name in ("quality", "unit-tests", "docker-build", "console", "cloud-e2e-main"))
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    assert "verify_main_cloud_acceptance.py" in str(job["steps"])
    assert any(step.get("name") == "Upload main Cloud acceptance receipt" for step in job["steps"])


def test_regular_ci_checks_keep_names_and_workflow_actions_are_sha_pinned() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    jobs = workflow["jobs"]
    assert [jobs[name]["name"] for name in ("quality", "unit-tests", "docker-build", "console")] == [
        "Code Quality",
        "Full Python Tests",
        "Docker / Compose Validation",
        "Console Quality & E2E",
    ]
    assert "needs" not in jobs["quality"] and "needs" not in jobs["unit-tests"]
    quality_run = next(step["run"] for step in jobs["quality"]["steps"] if step.get("name") == "Ruff")
    compile_run = next(step["run"] for step in jobs["quality"]["steps"] if step.get("name") == "Compile Python sources")
    assert "scripts/ci" in quality_run and "scripts/ci" in compile_run
    for job in jobs.values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert _is_sha(_action(step)[1]), f"unpinned Action: {step['uses']}"


def test_cloud_summary_reports_coverage_without_conflating_policy_and_cloud() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    jobs = workflow["jobs"]
    combined = str(jobs["cloud-e2e-policy"]["steps"] + jobs["cloud-e2e-main"]["steps"])
    assert "write_cloud_summary.py" in combined
    summary_script = (ROOT / "scripts/ci/write_cloud_summary.py").read_text()
    for state in ("passed", "skipped_by_policy", "blocked_missing_secrets", "blocked_disabled", "not_run_upstream_failed"):
        assert state in summary_script or state in combined
    assert "Main Cloud Acceptance" in str(jobs["main-cloud-acceptance"]["name"])
