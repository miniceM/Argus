"""Pure, fail-closed policy primitives for Cloud E2E coverage and PR admission."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import yaml

from scripts.ci.ci_constants import (
    CANDIDATE_ENVIRONMENT,
    CANDIDATE_WORKFLOW,
    LOW_RISK_DEV_DEPENDENCIES,
    MAIN_REF,
    REPOSITORY,
    REQUIRED_CI_CHECKS,
    REQUIRED_SECRETS,
    REQUIRED_SUITES,
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _decision(state: str, should_run: bool, secrets_allowed: bool, reason: str) -> dict[str, Any]:
    return {
        "state": state,
        "should_run": should_run,
        "secrets_allowed": secrets_allowed,
        "reason": reason,
    }


def decide_cloud_execution(
    *,
    event_name: str,
    ref: str,
    upstream_results: Mapping[str, str],
    enabled: str | None,
    secrets: Mapping[str, str | None],
    actor: str | None = None,
    triggering_actor: str | None = None,
) -> dict[str, Any]:
    """Return the Cloud job decision; actor identity never grants secret access."""
    # These parameters are deliberately accepted so callers/tests can prove that a
    # bot-owned PR, maintainer rerun, or actor change cannot promote trust.
    del actor, triggering_actor
    if event_name == "pull_request" or not (
        ref == MAIN_REF and event_name in {"push", "workflow_dispatch"}
    ):
        return _decision(
            "skipped_by_policy",
            False,
            False,
            "pull requests never receive Langfuse secrets"
            if event_name == "pull_request"
            else "Cloud secrets are restricted to main push and main dispatch",
        )

    if any(upstream_results.get(name) != "success" for name in REQUIRED_CI_CHECKS):
        return _decision(
            "not_run_upstream_failed", False, False, "all five regular CI jobs must succeed first"
        )
    if enabled != "true":
        return _decision("blocked_disabled", False, False, "LANGFUSE_E2E_ENABLED must equal true")
    missing = [name for name in REQUIRED_SECRETS if not secrets.get(name)]
    if missing:
        return _decision(
            "blocked_missing_secrets", False, False, "missing required test-environment secrets: " + ", ".join(missing)
        )
    return _decision("ready", True, True, "trusted main Cloud E2E is authorized")


def _parse_json_document(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else deepcopy(value)
    if not isinstance(parsed, dict):
        raise ValueError("manifest must be a JSON object")
    return parsed


def _parse_lock_document(value: Any) -> dict[str, Any]:
    parsed = yaml.safe_load(value) if isinstance(value, str) else deepcopy(value)
    if not isinstance(parsed, dict) or str(parsed.get("lockfileVersion")) != "9.0":
        raise ValueError("only pnpm lockfileVersion 9.0 is understood")
    if not isinstance(parsed.get("importers"), dict) or not isinstance(parsed.get("packages"), dict):
        raise ValueError("pnpm lockfile is missing importers or packages")
    if not isinstance(parsed.get("snapshots"), dict):
        raise ValueError("pnpm lockfile is missing snapshots")
    return parsed


def _package_name_and_version(key: str) -> tuple[str, str] | None:
    if not key.startswith("/") or "@" not in key[1:]:
        return None
    name, _, version = key[1:].rpartition("@")
    return (name, version) if name and version else None


def _candidate_keys(name: str, version_ref: str, entries: Mapping[str, Any]) -> list[str]:
    exact = f"/{name}@{version_ref}"
    if exact in entries:
        return [exact]
    # A lock importer may omit peer context while snapshots contain one or more
    # peer-specific resolutions. Resolve all matches conservatively; a missing
    # dependency is an unsupported lock shape, never a low-risk guess.
    prefix = f"/{name}@{version_ref}"
    matches = [
        key
        for key in entries
        if key.startswith(prefix) and (len(key) == len(prefix) or key[len(prefix)] == "(")
    ]
    return matches


def _package_closure(lock: Mapping[str, Any], package_name: str, version_ref: str) -> tuple[set[str], set[str]]:
    packages: Mapping[str, Any] = lock["packages"]
    snapshots: Mapping[str, Any] = lock["snapshots"]
    visited_snapshots: set[str] = set()
    visited_packages: set[str] = set()
    pending: list[tuple[str, str]] = [(package_name, version_ref)]

    while pending:
        name, reference = pending.pop()
        snapshot_keys = _candidate_keys(name, reference, snapshots)
        package_keys = _candidate_keys(name, reference.split("(", 1)[0], packages)
        if not snapshot_keys or not package_keys:
            raise ValueError(f"unresolved lock entry for {name}")
        visited_snapshots.update(snapshot_keys)
        visited_packages.update(package_keys)
        for snapshot_key in snapshot_keys:
            snapshot = snapshots[snapshot_key]
            if not isinstance(snapshot, dict):
                raise ValueError(f"invalid snapshot entry for {name}")
            for section in ("dependencies", "optionalDependencies"):
                children = snapshot.get(section, {})
                if not isinstance(children, dict):
                    raise ValueError(f"invalid {section} for {name}")
                pending.extend((child_name, str(child_ref)) for child_name, child_ref in children.items())
    return visited_packages, visited_snapshots


def _changed_mapping_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def _verify_registry_sources(lock: Mapping[str, Any], package_keys: set[str]) -> None:
    from urllib.parse import urlsplit

    for key in package_keys:
        package = lock["packages"].get(key)
        if not isinstance(package, dict):
            raise ValueError(f"invalid package metadata for {key}")
        resolution = package.get("resolution", {})
        if not isinstance(resolution, dict):
            raise ValueError(f"unknown package resolution for {key}")
        tarball = resolution.get("tarball")
        if tarball:
            parsed = urlsplit(str(tarball))
            if parsed.scheme != "https" or parsed.hostname not in {"registry.npmjs.org", "registry.yarnpkg.com"}:
                raise ValueError(f"non-registry package source for {key}")
        if "integrity" in resolution and not str(resolution["integrity"]).startswith(
            ("sha512-", "sha384-", "sha256-")
        ):
            raise ValueError(f"unsupported package integrity for {key}")


def _validate_console_dev_dependency_change(
    base_manifest_raw: Any,
    head_manifest_raw: Any,
    base_lock_raw: Any,
    head_lock_raw: Any,
) -> None:
    before_manifest = _parse_json_document(base_manifest_raw)
    after_manifest = _parse_json_document(head_manifest_raw)
    before_dev = before_manifest.pop("devDependencies", {})
    after_dev = after_manifest.pop("devDependencies", {})
    if before_manifest != after_manifest:
        raise ValueError("manifest changes outside devDependencies")
    if not isinstance(before_dev, dict) or not isinstance(after_dev, dict):
        raise ValueError("devDependencies must be objects")
    changed_names = _changed_mapping_keys(before_dev, after_dev)
    if not changed_names or not changed_names <= LOW_RISK_DEV_DEPENDENCIES:
        raise ValueError("dependency change is not in the reviewed development-tool allowlist")
    if any(not isinstance(after_dev.get(name), str) for name in changed_names if name in after_dev):
        raise ValueError("development dependency specifications must be strings")

    before_lock = _parse_lock_document(base_lock_raw)
    after_lock = _parse_lock_document(head_lock_raw)
    if before_lock.get("settings") != after_lock.get("settings"):
        raise ValueError("pnpm lock settings changed")
    before_importers = before_lock["importers"]
    after_importers = after_lock["importers"]
    if set(before_importers) != {"."} or set(after_importers) != {"."}:
        raise ValueError("unexpected pnpm importer set")
    before_root = before_importers["."]
    after_root = after_importers["."]
    if not isinstance(before_root, dict) or not isinstance(after_root, dict):
        raise ValueError("invalid root pnpm importer")
    for section in ("dependencies", "optionalDependencies"):
        if before_root.get(section, {}) != after_root.get(section, {}):
            raise ValueError(f"runtime importer {section} changed")
    before_locked_dev = before_root.get("devDependencies", {})
    after_locked_dev = after_root.get("devDependencies", {})
    if not isinstance(before_locked_dev, dict) or not isinstance(after_locked_dev, dict):
        raise ValueError("invalid locked devDependencies")
    if _changed_mapping_keys(before_locked_dev, after_locked_dev) != changed_names:
        raise ValueError("lockfile dev importer changes do not match the manifest")

    for name in changed_names:
        locked = after_locked_dev.get(name)
        if not isinstance(locked, dict) or locked.get("specifier") != after_dev.get(name):
            raise ValueError(f"lockfile specifier does not match manifest for {name}")

    old_closure_packages: set[str] = set()
    old_closure_snapshots: set[str] = set()
    new_closure_packages: set[str] = set()
    new_closure_snapshots: set[str] = set()
    for name in changed_names:
        old_entry = before_locked_dev.get(name)
        new_entry = after_locked_dev.get(name)
        if isinstance(old_entry, dict) and old_entry.get("version"):
            pkg, snap = _package_closure(before_lock, name, str(old_entry["version"]))
            old_closure_packages.update(pkg)
            old_closure_snapshots.update(snap)
        if isinstance(new_entry, dict) and new_entry.get("version"):
            pkg, snap = _package_closure(after_lock, name, str(new_entry["version"]))
            new_closure_packages.update(pkg)
            new_closure_snapshots.update(snap)
    allowed_package_keys = old_closure_packages | new_closure_packages
    allowed_snapshot_keys = old_closure_snapshots | new_closure_snapshots
    _verify_registry_sources(before_lock, old_closure_packages)
    _verify_registry_sources(after_lock, new_closure_packages)

    before_packages = before_lock["packages"]
    after_packages = after_lock["packages"]
    if any(
        before_packages.get(key) != after_packages.get(key)
        for key in before_packages.keys() | after_packages.keys()
        if key not in allowed_package_keys
    ):
        raise ValueError("lockfile changes outside changed development dependency closure")
    before_snapshots = before_lock["snapshots"]
    after_snapshots = after_lock["snapshots"]
    if any(
        before_snapshots.get(key) != after_snapshots.get(key)
        for key in before_snapshots.keys() | after_snapshots.keys()
        if key not in allowed_snapshot_keys
    ):
        raise ValueError("snapshot changes outside changed development dependency closure")



# Medium is intentionally narrower than general runtime code: scoped tests
# and Console presentation receive the five regular CI checks, without
# exposing PR code to Cloud secrets. Unknown paths remain high risk.
MEDIUM_DENIED_TEST_PREFIXES = (
    "tests/test_ci_",
    "tests/test_cloud_",
    "tests/test_langfuse_",
    "tests/test_trajectory_",
    "tests/test_release_",
    "tests/test_policy_",
    "tests/test_auth_",
    "tests/test_credential_",
    "tests/test_security_",
    "tests/test_expected_pass_rate",
)
MEDIUM_DENIED_CONSOLE_TERMS = (
    "auth", "cloud", "credential", "gate", "integration", "langfuse",
    "login", "permission", "policy", "release", "secret", "security",
    "session", "settings", "token", "trace",
)


def _medium_regular_file(path: str, status: str, modes: Mapping[str, Any]) -> bool:
    """Require a modified regular file; no addition, rename, symlink or executable."""
    if status != "modified":
        return False
    file_mode = modes.get(path, {})
    return (
        isinstance(file_mode, Mapping)
        and file_mode.get("base") == "100644"
        and file_mode.get("head") == "100644"
    )


def _medium_scope(path: str) -> bool:
    # Test modules never execute in the production image. Exclude CI/Cloud,
    # security and frozen release baselines even when named test_*.py.
    if (
        path.startswith("tests/test_")
        and path.count("/") == 1
        and path.endswith(".py")
        and not path.startswith(MEDIUM_DENIED_TEST_PREFIXES)
        and re.fullmatch(r"tests/test_[a-zA-Z0-9_]+\.py", path)
    ):
        return True

    # Only existing UI components/pages, not API clients, policy/auth views,
    # build scripts, configuration, shared backend or dependencies.
    root = "services/console/src/"
    if not path.startswith(root):
        return False
    relative = path[len(root):]
    if not relative.startswith(("components/", "pages/")):
        return False
    if not relative.endswith((".tsx", ".css")):
        return False
    return not any(term in relative.lower() for term in MEDIUM_DENIED_CONSOLE_TERMS)


def _medium_or_low_file(path: str, status: str, modes: Mapping[str, Any]) -> bool:
    if not _medium_regular_file(path, status, modes):
        return False
    if path.startswith("docs/") and path.endswith(".md"):
        return True
    return _medium_scope(path)


def classify_change_set(
    *,
    files: Sequence[Mapping[str, Any]],
    contents: Mapping[str, Any],
    file_modes: Mapping[str, Mapping[str, str | None]] | None = None,
    files_complete: bool = True,
) -> dict[str, str]:
    """Classify a fully enumerated PR diff. Unsupported/malformed inputs are high risk."""
    high = {"risk": "high"}
    if not files_complete or not files:
        return {**high, "reason": "changed-file list is incomplete or empty"}
    paths: list[str] = []
    for item in files:
        path = item.get("filename")
        old_path = item.get("previous_filename")
        status = item.get("status")
        if not isinstance(path, str) or not path or status == "renamed" or old_path:
            return {**high, "reason": "rename or malformed file entry requires review"}
        paths.append(path)
    if len(paths) != len(set(paths)):
        return {**high, "reason": "duplicate changed-file entry"}

    if all(path.startswith("docs/") and path.endswith(".md") for path in paths):
        modes = file_modes or {}
        if any(
            modes.get(path, {}).get("base") != "100644"
            or modes.get(path, {}).get("head") != "100644"
            for path in paths
        ):
            return {**high, "reason": "documentation symlink or executable file"}
        return {"risk": "low", "reason": "documentation-only change"}

    # One sensitive, renamed, new or non-regular file escalates the entire PR.
    modes = file_modes or {}
    if all(
        _medium_or_low_file(path, str(file.get("status", "")), modes)
        for path, file in zip(paths, files, strict=True)
    ):
        return {"risk": "medium", "reason": "scoped test or Console presentation change"}

    manifest_path = "services/console/package.json"
    lock_path = "services/console/pnpm-lock.yaml"
    if set(paths) != {manifest_path, lock_path}:
        return {**high, "reason": "change includes runtime, workflow, script, or unclassified files"}
    if any(item.get("status") != "modified" for item in files):
        return {**high, "reason": "dependency files must be modified in place"}
    modes = file_modes or {}
    if any(
        modes.get(path, {}).get(side) != "100644"
        for path in paths
        for side in ("base", "head")
    ):
        return {**high, "reason": "dependency manifest or lockfile is not a regular non-executable file"}
    required = (manifest_path, lock_path)
    if any(path not in contents for path in required):
        return {**high, "reason": "dependency change lacks complete base/head file contents"}
    try:
        base_manifest, head_manifest = contents[manifest_path]
        base_lock, head_lock = contents[lock_path]
        _validate_console_dev_dependency_change(base_manifest, head_manifest, base_lock, head_lock)
    except (ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        return {**high, "reason": f"dependency diff is not fully understood: {type(exc).__name__}"}
    return {"risk": "low", "reason": "allowlisted Console development dependency update"}


def validate_trusted_receipt(receipt: Any, expected: Mapping[str, Any]) -> bool:
    if not isinstance(receipt, dict):
        return False
    if receipt.get("schema_version") != 1 or receipt.get("repository") != REPOSITORY:
        return False
    for name in ("pr_number", "run_id", "run_attempt", "prepare_job_id", "cloud_job_id", "ci_run_id", "ci_run_attempt"):
        if isinstance(receipt.get(name), bool) or not isinstance(receipt.get(name), int) or receipt[name] <= 0:
            return False
    for name in ("head_sha", "base_sha", "tested_sha", "workflow_revision"):
        if not isinstance(receipt.get(name), str) or not SHA_RE.fullmatch(receipt[name]):
            return False
    identity_fields = (
        "repository",
        "pr_number",
        "head_sha",
        "base_sha",
        "tested_sha",
        "workflow_revision",
        "run_id",
        "run_attempt",
        "ci_run_id",
        "ci_run_attempt",
    )
    if any(receipt.get(key) != expected.get(key) for key in identity_fields):
        return False
    if receipt.get("workflow_path") != CANDIDATE_WORKFLOW:
        return False
    if receipt.get("workflow_ref") != f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@{MAIN_REF}":
        return False
    if not isinstance(receipt.get("dispatch_actor"), str) or not receipt["dispatch_actor"].strip():
        return False
    if receipt.get("risk") != "high":
        return False
    if receipt.get("environment") != CANDIDATE_ENVIRONMENT or receipt.get("cloud_state") != "passed":
        return False
    approval = receipt.get("approval")
    if not isinstance(approval, dict) or approval.get("state") != "approved":
        return False
    if approval.get("environment") != CANDIDATE_ENVIRONMENT or not isinstance(approval.get("login"), str) or not approval["login"].strip():
        return False
    for key in ("user_id", "environment_id"):
        if isinstance(approval.get(key), bool) or not isinstance(approval.get(key), int) or approval[key] <= 0:
            return False
    if receipt.get("required_suites") != list(REQUIRED_SUITES):
        return False
    if receipt.get("suite_results") != {suite: "passed" for suite in REQUIRED_SUITES}:
        return False
    if receipt.get("baseline") != {
        "v1": {"passed": 2, "total": 6},
        "v2": {"passed": 6, "total": 6},
    }:
        return False
    evidence = receipt.get("evidence_summary")
    if not isinstance(evidence, dict):
        return False
    if (
        evidence.get("bootstrap_items_upserted") != 6
        or evidence.get("v1_items") != 6
        or evidence.get("v2_items") != 6
        or evidence.get("async_link_verified") is not True
        or evidence.get("remote_run_readback") not in {"CONFIRMED", "NOT_EXPOSED"}
    ):
        return False
    return True


def evaluate_integration_policy(
    *,
    risk: str,
    checks: Mapping[str, str],
    receipt: Any,
    expected_receipt_identity: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the commit-status result and separate Cloud coverage state."""
    if any(name not in checks for name in REQUIRED_CI_CHECKS):
        return {"status": "pending", "state": "waiting_for_ci", "cloud_state": "skipped_by_policy"}
    values = [checks[name] for name in REQUIRED_CI_CHECKS]
    if any(value in {"queued", "in_progress", "pending", "waiting"} for value in values):
        return {"status": "pending", "state": "waiting_for_ci", "cloud_state": "skipped_by_policy"}
    if any(value != "success" for value in values):
        return {"status": "failure", "state": "required_ci_failed", "cloud_state": "skipped_by_policy"}
    if risk in {"low", "medium"}:
        return {"status": "success", "state": "passed", "cloud_state": "skipped_by_policy"}
    if receipt is None:
        return {
            "status": "pending",
            "state": "awaiting_trusted_validation",
            "cloud_state": "skipped_by_policy",
        }
    if expected_receipt_identity is None or not validate_trusted_receipt(receipt, expected_receipt_identity):
        return {"status": "failure", "state": "invalid_trusted_receipt", "cloud_state": "failed"}
    return {"status": "success", "state": "passed", "cloud_state": "passed"}
