from __future__ import annotations

import ast
import os
import shlex
import subprocess
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
    assert "scripts.ci.verify_main_cloud_acceptance" in str(job["steps"])
    assert any(step.get("name") == "Upload main Cloud acceptance receipt" for step in job["steps"])


def test_main_cloud_e2e_passes_validated_secrets_to_runner_step() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    job = workflow["jobs"]["cloud-e2e-main"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    run_step = next(step for step in job["steps"] if step.get("name") == "Run cloud end-to-end tests")
    assert run_step["env"]["LANGFUSE_BASE_URL"] == "${{ secrets.LANGFUSE_BASE_URL }}"
    assert run_step["env"]["LANGFUSE_PUBLIC_KEY"] == "${{ secrets.LANGFUSE_PUBLIC_KEY }}"
    assert run_step["env"]["LANGFUSE_SECRET_KEY"] == "${{ secrets.LANGFUSE_SECRET_KEY }}"


def test_regular_ci_checks_keep_names_and_workflow_actions_are_sha_pinned() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    jobs = workflow["jobs"]
    assert [jobs[name]["name"] for name in ("quality", "unit-tests", "docker-build", "console", "trajectory-local")] == [
        "Code Quality",
        "Full Python Tests",
        "Docker / Compose Validation",
        "Console Quality & E2E",
        "S4 Local Langfuse Integration",
    ]
    assert all(
        "trajectory-local" in jobs[job_name]["needs"]
        for job_name in ("cloud-e2e-policy", "cloud-e2e-main", "main-cloud-acceptance")
    )
    policy_summary = str(jobs["cloud-e2e-policy"]["steps"])
    assert 'needs.get("trajectory-local", {}).get("result", "not run")' in policy_summary
    main_gate = next(
        step for step in jobs["cloud-e2e-main"]["steps"]
        if step.get("name") == "Validate main Cloud prerequisites"
    )
    assert "S4_RESULT" in main_gate["env"]
    assert '"$S4_RESULT" != success' in main_gate["run"]
    assert "needs" not in jobs["quality"] and "needs" not in jobs["unit-tests"]
    quality_run = next(step["run"] for step in jobs["quality"]["steps"] if step.get("name") == "Ruff")
    compile_run = next(step["run"] for step in jobs["quality"]["steps"] if step.get("name") == "Compile Python sources")
    assert "scripts/ci" in quality_run and "scripts/ci" in compile_run
    for job in jobs.values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert _is_sha(_action(step)[1]), f"unpinned Action: {step['uses']}"


def test_trajectory_ci_covers_every_module_excluded_from_regular_tests() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    runs = [step["run"] for step in workflow["jobs"]["trajectory-local"]["steps"] if "run" in step]
    commands = [shlex.split(run.replace("\\\n", " ")) for run in runs if "pytest" in run]
    selected = {
        token for command in commands if "trajectory_local" in command
        for token in command if token.startswith("tests/")
    }
    required = set()
    for module in (ROOT / "tests").glob("test_*.py"):
        for node in ast.parse(module.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
            ) and "pytest.mark.trajectory_local" in ast.unparse(node.value):
                required.add(module.relative_to(ROOT).as_posix())
    assert required, "expected real local integration modules"
    assert required <= selected, f"real integration modules missing from CI: {sorted(required - selected)}"


def test_cloud_summary_imports_without_optional_policy_dependencies() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-S", "-c", "import scripts.ci.write_cloud_summary"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_cloud_summary_reports_coverage_without_conflating_policy_and_cloud() -> None:
    workflow = _workflow(".github/workflows/ci.yml")
    jobs = workflow["jobs"]
    combined = str(jobs["cloud-e2e-policy"]["steps"] + jobs["cloud-e2e-main"]["steps"])
    assert "scripts.ci.write_cloud_summary" in combined
    summary_script = (ROOT / "scripts/ci/write_cloud_summary.py").read_text()
    for state in ("passed", "skipped_by_policy", "blocked_missing_secrets", "blocked_disabled", "not_run_upstream_failed"):
        assert state in summary_script or state in combined
    assert "Main Cloud Acceptance" in str(jobs["main-cloud-acceptance"]["name"])


def test_trusted_candidate_workflow_is_main_dispatched_approved_and_least_privilege() -> None:
    workflow = _workflow(".github/workflows/cloud-e2e-trusted.yml")
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"pr_number", "expected_head_sha", "expected_base_sha"}
    assert all(value.get("required") is True for value in inputs.values())

    jobs = workflow["jobs"]
    prepare = jobs["prepare"]
    candidate = jobs["candidate-cloud"]
    receipt = jobs["receipt"]
    assert prepare["permissions"] == {"actions": "read", "contents": "read", "pull-requests": "read"}
    assert candidate["environment"] == "langfuse-e2e-candidate"
    assert candidate["permissions"] == {"actions": "read", "contents": "read", "pull-requests": "read"}
    assert receipt["permissions"] == {"actions": "read", "contents": "read", "pull-requests": "read"}
    assert "always()" in receipt["if"]

    candidate_steps = candidate["steps"]
    revalidate = next(i for i, step in enumerate(candidate_steps) if step.get("name") == "Revalidate candidate and write test credentials")
    run_e2e = next(i for i, step in enumerate(candidate_steps) if step.get("name") == "Run cloud end-to-end tests")
    assert revalidate < run_e2e
    secret_steps = [step for step in candidate_steps if any("${{ secrets." in str(value) for value in step.get("env", {}).values())]
    assert [step["name"] for step in secret_steps] == ["Revalidate candidate and write test credentials"]
    assert "${{ secrets." not in str(candidate_steps[run_e2e].get("env", {}))
    assert "statuses: write" not in str(workflow)
    for job in jobs.values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert _is_sha(_action(step)[1]), f"unpinned Action: {step['uses']}"


def test_trusted_candidate_builds_without_secrets_then_runs_existing_cloud_suite() -> None:
    workflow = _workflow(".github/workflows/cloud-e2e-trusted.yml")
    steps = workflow["jobs"]["candidate-cloud"]["steps"]
    names = [step.get("name") for step in steps]
    install = names.index("Install candidate test dependencies without Cloud credentials")
    build = names.index("Build candidate images without Cloud credentials")
    verify = names.index("Revalidate candidate and write test credentials")
    run_e2e = names.index("Run cloud end-to-end tests")
    assert install < build < verify < run_e2e
    assert steps[build]["env"]["ARGUS_BUILD_ID"] == "${{ needs.prepare.outputs.tested_sha }}"
    assert "scripts/ci-e2e-cloud.sh" in str(steps[run_e2e])
    assert steps[run_e2e]["env"]["ARGUS_CLOUD_BUILD_IMAGES"] == "false"

    script = (ROOT / "scripts/ci-e2e-cloud.sh").read_text()
    assert 'source "$ROOT/scripts/ci/load-cloud-env.sh"' in script
    assert 'load_cloud_environment_file "$COMPOSE_ENV_FILE"' in script
    assert "scripts.ci.cloud_error_summary" in script
    assert "ARGUS_CLOUD_BUILD_IMAGES" in script
    assert 'false) "${COMPOSE[@]}" up -d ;;' in script


def test_candidate_cloud_env_loader_treats_values_as_data(tmp_path: Path) -> None:
    loader = ROOT / "scripts/ci/load-cloud-env.sh"
    env_file = tmp_path / ".env.cloud"
    marker = tmp_path / "must-not-be-created"
    secret_value = f"$(touch {marker})"
    env_file.write_text(
        "LANGFUSE_BASE_URL=https://langfuse.example\n"
        "LANGFUSE_PUBLIC_KEY=pk=with-equals\n"
        f"LANGFUSE_SECRET_KEY={secret_value}\n",
        encoding="utf-8",
    )
    command = (
        'source "$1"; load_cloud_environment_file "$2"; '
        'printf "%s\n" "$LANGFUSE_BASE_URL" "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY"'
    )

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", command, "cloud-env-test", str(loader), str(env_file)],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "LANGFUSE_BASE_URL": "https://preset.example"},
    )

    assert result.stdout.splitlines() == ["https://preset.example", "pk=with-equals", secret_value]
    assert not marker.exists()


def test_cloud_integration_policy_is_a_serialized_default_branch_controller() -> None:
    workflow = _workflow(".github/workflows/cloud-integration-policy.yml")
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_run", "push", "workflow_dispatch"}
    assert trigger["workflow_run"]["workflows"] == ["CI", "Trusted Cloud Candidate"]
    assert trigger["workflow_run"]["types"] == ["completed"]
    assert "branches" not in trigger["workflow_run"]
    assert trigger["push"]["branches"] == ["main"]
    assert trigger["workflow_dispatch"]["inputs"]["pr_number"]["required"] is True
    assert workflow["concurrency"]["cancel-in-progress"] is False

    jobs = workflow["jobs"]
    evaluate = jobs["evaluate"]
    publish = jobs["publish"]
    assert evaluate["permissions"] == {"actions": "read", "contents": "read", "pull-requests": "read"}
    assert publish["permissions"]["statuses"] == "write"
    assert all(
        value != "write"
        for scope, value in publish["permissions"].items()
        if scope != "statuses"
    )
    assert "statuses: write" not in str(evaluate)
    assert "scripts.ci.publish_cloud_policy evaluate" in str(evaluate["steps"])
    assert "scripts.ci.publish_cloud_policy publish" in str(publish["steps"])
    assert "github.workflow_sha" in str(evaluate["steps"])
    assert "github.workflow_sha" in str(publish["steps"])
    assert "cloud-integration-policy-decisions-" in str(evaluate["steps"] + publish["steps"])
    assert "actions/download-artifact@" in str(publish["steps"])
    assert "actions/upload-artifact@" in str(evaluate["steps"])
    for job in jobs.values():
        for step in job.get("steps", []):
            if "uses" in step:
                assert _is_sha(_action(step)[1]), f"unpinned Action: {step['uses']}"


def test_workflow_python_cli_scripts_use_module_invocation() -> None:
    workflow_paths = (
        ".github/workflows/ci.yml",
        ".github/workflows/cloud-e2e-trusted.yml",
        ".github/workflows/cloud-integration-policy.yml",
        ".github/workflows/langfuse-i18n.yml",
    )

    def runs_in(value: object) -> list[str]:
        if isinstance(value, dict):
            found = [str(value["run"])] if isinstance(value.get("run"), str) else []
            for child in value.values():
                found.extend(runs_in(child))
            return found
        if isinstance(value, list):
            return [run for child in value for run in runs_in(child)]
        return []

    for path in workflow_paths:
        cli_runs = [run for run in runs_in(_workflow(path)) if "scripts.ci." in run or "scripts/ci/" in run]
        assert cli_runs, f"expected Python CI CLI invocations in {path}"
        assert all("python -m scripts.ci." in run for run in cli_runs), path
        assert all("python scripts/ci/" not in run for run in cli_runs), path
