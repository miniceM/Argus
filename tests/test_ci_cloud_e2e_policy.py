from __future__ import annotations

from scripts.ci.cloud_e2e_policy import (
    REQUIRED_CI_CHECKS,
    classify_change_set,
    decide_cloud_execution,
    evaluate_integration_policy,
    validate_trusted_receipt,
)

SHA_HEAD = "1" * 40
SHA_BASE = "2" * 40
SHA_TESTED = "3" * 40
SHA_WORKFLOW = "4" * 40


def _manifest(version: str = "^6.0.0", script: str = "vitest run") -> dict:
    return {
        "name": "argus-console",
        "private": True,
        "packageManager": "pnpm@10.5.2",
        "scripts": {"test": script},
        "dependencies": {"react": "^19.0.0"},
        "devDependencies": {"@testing-library/jest-dom": version, "vitest": "^5.0.0"},
    }


def _lock(version: str = "6.0.0", specifier: str = "^6.0.0") -> dict:
    package_key = f"/@testing-library/jest-dom@{version}"
    return {
        "lockfileVersion": "9.0",
        "settings": {"autoInstallPeers": True, "excludeLinksFromLockfile": False},
        "importers": {
            ".": {
                "dependencies": {"react": {"specifier": "^19.0.0", "version": "19.0.0"}},
                "devDependencies": {
                    "@testing-library/jest-dom": {"specifier": specifier, "version": version},
                    "vitest": {"specifier": "^5.0.0", "version": "5.0.0"},
                },
            }
        },
        "packages": {package_key: {"resolution": {"integrity": f"sha512-{version}"}}},
        "snapshots": {package_key: {}},
    }


def _receipt() -> dict:
    return {
        "schema_version": 2,
        "repository": "miniceM/Argus",
        "pr_number": 110,
        "head_sha": SHA_HEAD,
        "base_sha": SHA_BASE,
        "tested_sha": SHA_TESTED,
        "workflow_path": ".github/workflows/cloud-e2e-trusted.yml",
        "workflow_revision": SHA_WORKFLOW,
        "workflow_ref": "miniceM/Argus/.github/workflows/cloud-e2e-trusted.yml@refs/heads/main",
        "dispatch_actor": "github-actions[bot]",
        "risk": "high",
        "run_id": 9001,
        "run_attempt": 2,
        "prepare_job_id": 8000,
        "cloud_job_id": 8001,
        "ci_run_id": 8999,
        "ci_run_attempt": 1,
        "environment": "langfuse-e2e-candidate",
        "cloud_state": "passed",
        "authorization": {"mode": "trusted_ci_automatic", "ci_run_id": 8999, "ci_run_attempt": 1},
        "required_suites": ["managed-cloud", "async-link"],
        "suite_results": {"managed-cloud": "passed", "async-link": "passed"},
        "baseline": {"v1": {"passed": 2, "total": 6}, "v2": {"passed": 6, "total": 6}},
        "evidence_summary": {
            "bootstrap_items_upserted": 6,
            "v1_items": 6,
            "v2_items": 6,
            "async_link_verified": True,
            "remote_run_readback": "CONFIRMED",
        },
    }


def test_dependabot_and_human_rerun_never_receive_cloud_secrets() -> None:
    for actor, triggering_actor in (
        ("dependabot[bot]", "dependabot[bot]"),
        ("dependabot[bot]", "maintainer"),
        ("maintainer", "maintainer"),
    ):
        decision = decide_cloud_execution(
            event_name="pull_request",
            ref="refs/pull/110/merge",
            upstream_results={name: "success" for name in REQUIRED_CI_CHECKS},
            enabled="true",
            secrets={"LANGFUSE_BASE_URL": "configured", "LANGFUSE_PUBLIC_KEY": "configured", "LANGFUSE_SECRET_KEY": "configured"},
            actor=actor,
            triggering_actor=triggering_actor,
        )
        assert decision == {
            "state": "skipped_by_policy",
            "should_run": False,
            "secrets_allowed": False,
            "reason": "pull requests never receive Langfuse secrets",
        }


def test_non_main_dispatch_is_offline_even_when_enabled() -> None:
    decision = decide_cloud_execution(
        event_name="workflow_dispatch",
        ref="refs/heads/feature/try-it",
        upstream_results={name: "success" for name in REQUIRED_CI_CHECKS},
        enabled="true",
        secrets={"LANGFUSE_BASE_URL": "x", "LANGFUSE_PUBLIC_KEY": "x", "LANGFUSE_SECRET_KEY": "x"},
    )
    assert decision["state"] == "skipped_by_policy"
    assert not decision["secrets_allowed"]


def test_main_fails_closed_for_upstream_disabled_and_missing_secrets() -> None:
    main = {"event_name": "push", "ref": "refs/heads/main"}
    upstream = {name: "success" for name in REQUIRED_CI_CHECKS}

    assert decide_cloud_execution(
        **main, upstream_results={**upstream, "Console Quality & E2E": "skipped"}, enabled="true", secrets={}
    )["state"] == "not_run_upstream_failed"
    assert decide_cloud_execution(**main, upstream_results=upstream, enabled="TRUE", secrets={})["state"] == "blocked_disabled"
    assert decide_cloud_execution(**main, upstream_results=upstream, enabled="true", secrets={})["state"] == "blocked_missing_secrets"
    assert decide_cloud_execution(
        **main,
        upstream_results=upstream,
        enabled="true",
        secrets={"LANGFUSE_BASE_URL": "ok", "LANGFUSE_PUBLIC_KEY": "ok", "LANGFUSE_SECRET_KEY": "ok"},
    )["state"] == "ready"


def test_low_risk_allows_only_allowlisted_console_dev_dependency_and_lock_closure() -> None:
    result = classify_change_set(
        files=[
            {"filename": "services/console/package.json", "status": "modified"},
            {"filename": "services/console/pnpm-lock.yaml", "status": "modified"},
        ],
        contents={
            "services/console/package.json": (_manifest(), _manifest("^7.0.1")),
            "services/console/pnpm-lock.yaml": (_lock(), _lock("7.0.1", "^7.0.1")),
        },
        file_modes={path: {"base": "100644", "head": "100644"} for path in (
            "services/console/package.json", "services/console/pnpm-lock.yaml"
        )},
    )
    assert result["risk"] == "low"
    assert result["reason"] == "allowlisted Console development dependency update"


def test_major_test_tool_upgrade_is_still_low_when_the_lockfile_explains_it() -> None:
    result = classify_change_set(
        files=[
            {"filename": "services/console/package.json", "status": "modified"},
            {"filename": "services/console/pnpm-lock.yaml", "status": "modified"},
        ],
        contents={
            "services/console/package.json": (_manifest("^6.0.0"), _manifest("^7.0.0")),
            "services/console/pnpm-lock.yaml": (_lock(), _lock("7.0.0", "^7.0.0")),
        },
        file_modes={path: {"base": "100644", "head": "100644"} for path in (
            "services/console/package.json", "services/console/pnpm-lock.yaml"
        )},
    )
    assert result["risk"] == "low"


def test_docs_only_is_low_but_unknown_or_sensitive_changes_are_high() -> None:
    assert classify_change_set(
        files=[{"filename": "docs/guide.md", "status": "modified"}],
        contents={},
        file_modes={"docs/guide.md": {"base": "100644", "head": "100644"}},
    )["risk"] == "low"
    assert classify_change_set(
        files=[{"filename": "docs/guide.md", "status": "modified"}],
        contents={},
        file_modes={"docs/guide.md": {"base": "120000", "head": "120000"}},
    )["risk"] == "high"
    for file in (
        {"filename": "services/eval-runner/requirements.txt", "status": "modified"},
        {"filename": ".github/workflows/ci.yml", "status": "modified"},
        {"filename": "services/console/vite.config.ts", "status": "modified"},
        {"filename": "services/console/package.json", "previous_filename": "docs/guide.md", "status": "renamed"},
    ):
        assert classify_change_set(files=[file], contents={})["risk"] == "high"


def test_medium_scoped_tests_and_console_presentation_only() -> None:
    approved = (
        "tests/test_agent_registry.py",
        "services/console/src/components/AgentVersionCard.tsx",
        "services/console/src/pages/ExperimentLaunchList.tsx",
        "services/console/src/components/StatusBadge.css",
        "services/console/src/components/AgentCard.test.tsx",
    )
    for path in approved:
        assert classify_change_set(
            files=[{"filename": path, "status": "modified"}],
            contents={},
            file_modes={path: {"base": "100644", "head": "100644"}},
        )["risk"] == "medium", path

    mixed = classify_change_set(
        files=[
            {"filename": "docs/guide.md", "status": "modified"},
            {"filename": "tests/test_agent_registry.py", "status": "modified"},
        ],
        contents={},
        file_modes={
            "docs/guide.md": {"base": "100644", "head": "100644"},
            "tests/test_agent_registry.py": {"base": "100644", "head": "100644"},
        },
    )
    assert mixed["risk"] == "medium"


def test_medium_never_covers_sensitive_ambiguous_or_unsafe_paths() -> None:
    forbidden = (
        "tests/test_ci_cloud_e2e_policy.py",
        "tests/test_ci.py",
        "tests/test_cloud.py",
        "tests/test_cloud_e2e.py",
        "tests/test_langfuse_trace.py",
        "tests/test_release_gate.py",
        "services/console/src/components/CredentialField.tsx",
        "services/console/src/pages/ReleaseGate.tsx",
        "services/console/src/pages/CloudPolicy.tsx",
        "services/console/src/api/client.ts",
        "services/eval-runner/app/evaluators.py",
        ".github/workflows/ci.yml",
        "scripts/ci/publish_cloud_policy.py",
    )
    for path in forbidden:
        assert classify_change_set(
            files=[{"filename": path, "status": "modified"}],
            contents={},
            file_modes={path: {"base": "100644", "head": "100644"}},
        )["risk"] == "high", path

    path = "services/console/src/components/AgentVersionCard.tsx"
    for status, base, head in (
        ("renamed", "100644", "100644"),
        ("modified", "120000", "120000"),
        ("modified", "100644", "100755"),
        ("modified", None, "100644"),
        ("added", None, "100644"),
        ("modified", "100644", None),
    ):
        assert classify_change_set(
            files=[{"filename": path, "status": status}],
            contents={},
            file_modes={path: {"base": base, "head": head}},
        )["risk"] == "high", (status, base, head)

    assert classify_change_set(
        files=[
            {"filename": "services/console/src/components/AgentVersionCard.tsx", "status": "modified"},
            {"filename": "services/eval-runner/app/main.py", "status": "modified"},
        ],
        contents={},
        file_modes={
            "services/console/src/components/AgentVersionCard.tsx": {"base": "100644", "head": "100644"},
            "services/eval-runner/app/main.py": {"base": "100644", "head": "100644"},
        },
    )["risk"] == "high"


def test_medium_requires_all_five_ci_checks_but_no_cloud_receipt() -> None:
    green = {name: "success" for name in REQUIRED_CI_CHECKS}
    assert evaluate_integration_policy(risk="medium", checks=green, receipt=None) == {
        "status": "success", "state": "passed", "cloud_state": "skipped_by_policy",
    }
    assert evaluate_integration_policy(
        risk="medium", checks={**green, "S4 Local Langfuse Integration": "skipped"}, receipt=None
    )["status"] == "failure"


def test_manifest_scripts_runtime_and_lock_only_changes_are_high() -> None:
    files = [
        {"filename": "services/console/package.json", "status": "modified"},
        {"filename": "services/console/pnpm-lock.yaml", "status": "modified"},
    ]
    script_change = classify_change_set(
        files=files,
        contents={
            "services/console/package.json": (_manifest(), _manifest(script="vitest run --watch")),
            "services/console/pnpm-lock.yaml": (_lock(), _lock()),
        },
    )
    assert script_change["risk"] == "high"

    runtime_base = _manifest()
    runtime_head = _manifest()
    runtime_head["dependencies"]["react"] = "^20.0.0"
    runtime_change = classify_change_set(
        files=files,
        contents={
            "services/console/package.json": (runtime_base, runtime_head),
            "services/console/pnpm-lock.yaml": (_lock(), _lock()),
        },
    )
    assert runtime_change["risk"] == "high"
    assert classify_change_set(
        files=[{"filename": "services/console/pnpm-lock.yaml", "status": "modified"}], contents={}
    )["risk"] == "high"


def test_incomplete_or_unparseable_change_set_is_high() -> None:
    assert classify_change_set(files=[], contents={})["risk"] == "high"
    assert classify_change_set(
        files=[{"filename": "docs/guide.md", "status": "modified"}], contents={}, files_complete=False
    )["risk"] == "high"
    assert classify_change_set(
        files=[
            {"filename": "services/console/package.json", "status": "modified"},
            {"filename": "services/console/pnpm-lock.yaml", "status": "modified"},
        ],
        contents={
            "services/console/package.json": (_manifest(), _manifest("^7.0.0")),
            "services/console/pnpm-lock.yaml": ("bad: [", "bad: ["),
        },
    )["risk"] == "high"


def test_s4_local_integration_is_required_for_policy_success() -> None:
    four_existing_checks = {
        "Code Quality": "success",
        "Full Python Tests": "success",
        "Docker / Compose Validation": "success",
        "Console Quality & E2E": "success",
    }
    missing_s4 = evaluate_integration_policy(
        risk="low", checks=four_existing_checks, receipt=None
    )
    assert missing_s4 == {
        "status": "pending",
        "state": "waiting_for_ci",
        "cloud_state": "skipped_by_policy",
    }

    skipped_s4 = evaluate_integration_policy(
        risk="low",
        checks={**four_existing_checks, "S4 Local Langfuse Integration": "skipped"},
        receipt=None,
    )
    assert skipped_s4["status"] == "failure"


def test_required_check_and_receipt_decisions_are_fail_closed() -> None:
    green = {name: "success" for name in REQUIRED_CI_CHECKS}
    assert evaluate_integration_policy(risk="low", checks=green, receipt=None)["status"] == "success"
    assert evaluate_integration_policy(risk="high", checks=green, receipt=None)["state"] == "awaiting_trusted_validation"
    assert evaluate_integration_policy(
        risk="high", checks={**green, "Console Quality & E2E": "skipped"}, receipt=_receipt()
    )["status"] == "failure"
    assert evaluate_integration_policy(risk="high", checks=green, receipt={"cloud_state": "passed"})["status"] == "failure"
    receipt = _receipt()
    expected = {
        key: receipt[key]
        for key in (
            "repository", "pr_number", "head_sha", "base_sha", "tested_sha",
            "workflow_revision", "run_id", "run_attempt", "ci_run_id", "ci_run_attempt"
        )
    }
    accepted = evaluate_integration_policy(
        risk="high", checks=green, receipt=receipt, expected_receipt_identity=expected
    )
    assert accepted == {"status": "success", "state": "passed", "cloud_state": "passed"}


def test_receipt_is_bound_to_exact_pr_base_head_and_run() -> None:
    receipt = _receipt()
    expected = {
        "repository": "miniceM/Argus",
        "pr_number": 110,
        "head_sha": SHA_HEAD,
        "base_sha": SHA_BASE,
        "tested_sha": SHA_TESTED,
        "workflow_revision": SHA_WORKFLOW,
        "run_id": 9001,
        "run_attempt": 2,
        "ci_run_id": 8999,
        "ci_run_attempt": 1,
    }
    assert validate_trusted_receipt(receipt, expected)
    for key, value in (
        ("head_sha", "5" * 40),
        ("base_sha", "6" * 40),
        ("workflow_revision", "not-a-sha"),
        ("run_attempt", 1),
        ("prepare_job_id", True),
        ("workflow_ref", "external/workflow"),
        ("dispatch_actor", ""),
        ("evidence_summary", {"async_link_verified": False}),
        ("authorization", {"mode": "trusted_ci_automatic", "ci_run_id": 8999, "ci_run_attempt": 2}),
    ):
        assert not validate_trusted_receipt({**receipt, key: value}, expected)
