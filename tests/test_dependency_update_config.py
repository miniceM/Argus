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
    assert update["schedule"] == {"interval": "weekly"}
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
        ("docker", "/deploy/langfuse"): ("build", ["dependencies"]),
    }

    actual = {
        (update["package-ecosystem"], update["directory"]): update
        for update in updates
    }
    assert set(actual) == set(expected)
    for key, (prefix, labels) in expected.items():
        update = actual[key]
        assert update["schedule"] == {"interval": "weekly"}
        assert update["commit-message"] == {"prefix": prefix}
        assert update["labels"] == labels

    actions = actual[("github-actions", "/")]
    assert actions["groups"] == {"github-actions": {"patterns": ["*"]}}
