from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _dependabot_config() -> dict:
    return yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())


def test_default_branch_pip_update_has_single_root_entry() -> None:
    updates = _dependabot_config()["updates"]
    pip_updates = [update for update in updates if update["package-ecosystem"] == "pip"]

    assert len(pip_updates) == 1, "pip requirements are already aggregated at the repository root"
    update = pip_updates[0]
    assert update["directory"] == "/"
    assert "target-branch" not in update
    assert update["schedule"] == {
        "interval": "cron",
        "cronjob": "0 0 1 */2 *",
    }
    assert update["commit-message"] == {"prefix": "build"}
    assert update["labels"] == ["dependencies"]


def test_root_requirements_include_both_runtime_manifests() -> None:
    requirements = ROOT / "requirements-dev.txt"
    references = {
        line.strip()[3:]
        for line in requirements.read_text().splitlines()
        if line.strip().startswith("-r ")
    }

    expected = {
        "services/eval-runner/requirements.txt",
        "services/demo-agent/requirements.txt",
        "services/real-llm-agent/requirements.txt",
    }
    assert references == expected
    for reference in references:
        assert (ROOT / reference).is_file(), f"missing referenced manifest: {reference}"


def test_non_pip_update_sources_are_preserved() -> None:
    updates = [
        update
        for update in _dependabot_config()["updates"]
        if update["package-ecosystem"] != "pip"
    ]
    expected = {
        ("github-actions", "/"): ("ci", ["dependencies", "ci"]),
        ("npm", "/services/console"): ("build", ["dependencies", "console"]),
        ("docker", "/services/eval-runner"): ("build", ["dependencies"]),
        ("docker", "/services/console"): ("build", ["dependencies"]),
        ("docker", "/services/demo-agent"): ("build", ["dependencies"]),
        ("docker", "/services/real-llm-agent"): ("build", ["dependencies"]),
        ("docker", "/deploy/langfuse"): ("build", ["dependencies"]),
    }

    actual = {
        (update["package-ecosystem"], update["directory"]): update
        for update in updates
    }
    assert set(actual) == set(expected)
    for key, (prefix, labels) in expected.items():
        update = actual[key]
        assert update["schedule"] == {
            "interval": "cron",
            "cronjob": "0 0 1 */2 *",
        }
        assert update["commit-message"] == {"prefix": prefix}
        assert update["labels"] == labels

    actions = actual[("github-actions", "/")]
    assert actions["groups"] == {"github-actions": {"patterns": ["*"]}}


def test_shared_python_jobs_check_installed_dependencies() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]
    install_command = "python -m pip install -r requirements-dev.txt"
    missing_checks: list[str] = []

    for job_id in ("quality", "unit-tests", "console"):
        steps = jobs[job_id]["steps"]
        install_steps = [
            (index, step)
            for index, step in enumerate(steps)
            if install_command in step.get("run", "").splitlines()
        ]
        assert len(install_steps) == 1, f"{job_id} must install the aggregate requirements once"
        install_index, install_step = install_steps[0]
        run_lines = [line.strip() for line in install_step["run"].splitlines()]
        install_indexes = [
            index
            for index, line in enumerate(run_lines)
            if line.startswith("python -m pip install ")
        ]
        check_indexes = [
            index for index, line in enumerate(run_lines) if line == "python -m pip check"
        ]

        if len(check_indexes) != 1:
            missing_checks.append(job_id)
            continue
        assert install_indexes and check_indexes[0] > max(install_indexes)
        assert install_step.get("continue-on-error") is not True
        assert install_index < next(
            index
            for index, step in enumerate(steps)
            if index > install_index
            and any(
                command in step.get("run", "")
                for command in (
                    "python -m pytest",
                    "python scripts/export_openapi.py",
                    "pnpm --dir services/console api:generate",
                )
            )
        )

    assert not missing_checks, f"jobs missing installed-dependency checks: {missing_checks}"


def test_console_install_uses_single_aggregate_requirements() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["console"]["steps"]
    install_command = "python -m pip install -r requirements-dev.txt"
    install_steps = [
        step
        for step in steps
        if install_command in step.get("run", "").splitlines()
    ]

    assert len(install_steps) == 1
    requirement_installs = [
        line.strip()
        for line in install_steps[0]["run"].splitlines()
        if line.strip().startswith("python -m pip install -r ")
    ]
    assert requirement_installs == [install_command]


def test_console_pip_cache_covers_aggregate_inputs() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    setup_python = next(
        step
        for step in workflow["jobs"]["console"]["steps"]
        if step.get("uses", "").split("@", 1)[0] == "actions/setup-python"
    )
    cache_paths = setup_python["with"]["cache-dependency-path"].splitlines()

    assert {path.strip() for path in cache_paths if path.strip()} == {
        "requirements-dev.txt",
        "services/eval-runner/requirements.txt",
        "services/demo-agent/requirements.txt",
        "services/real-llm-agent/requirements.txt",
    }
