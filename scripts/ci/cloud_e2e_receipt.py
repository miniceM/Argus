"""Trusted preparation and receipt validation for manually approved Cloud candidates.

Candidate source and Cloud output are always treated as untrusted data. This module is
checked out from the workflow's trusted main revision in preparation/post-processing
jobs, never imported from the candidate checkout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import zipfile
from collections.abc import Mapping, Sequence
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from scripts.ci.ci_constants import (
    CANDIDATE_ENVIRONMENT,
    CANDIDATE_WORKFLOW,
    MAIN_REF,
    REPOSITORY,
    REQUIRED_CI_CHECKS,
    REQUIRED_SECRETS,
    REQUIRED_SUITES,
)
from scripts.ci.cloud_e2e_policy import classify_change_set, validate_trusted_receipt
from scripts.ci.github_api import GitHubApiError, GitHubClient

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
CANDIDATE_PREPARE_JOB_NAME = "Prepare trusted candidate manifest"
CANDIDATE_PREPARE_STEP = "Prepare exact high-risk candidate"
CANDIDATE_JOB_NAME = "Trusted candidate Cloud E2E"
CANDIDATE_SECRET_STEP = "Validate candidate Langfuse Cloud secrets"
CANDIDATE_E2E_STEP = "Run cloud end-to-end tests"
MANIFEST_ARTIFACT_PREFIX = "candidate-manifest"
EVIDENCE_ARTIFACT_PREFIX = "candidate-cloud-evidence"
RECEIPT_ARTIFACT_PREFIX = "trusted-cloud-receipt"
MAX_ARTIFACT_BYTES = 2_000_000
MAX_ARCHIVE_ENTRIES = 40
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 2_000_000
MAX_ARCHIVE_FILE_BYTES = 1_000_000
EVIDENCE_FILES = ("bootstrap.json", "v1.json", "v2.json", "async-langfuse-link.json")


class CandidateReceiptError(RuntimeError):
    """A candidate or its evidence does not satisfy the trusted receipt contract."""


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateReceiptError("JSON artifact contains duplicate object keys")
        result[key] = value
    return result


def read_json_archive(data: bytes, expected_filenames: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Read small JSON inputs from a zip without extracting or executing any entry."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_ARTIFACT_BYTES:
        raise CandidateReceiptError("artifact archive is empty or exceeds the compressed-size limit")
    expected = set(expected_filenames)
    if len(expected) != len(expected_filenames) or not expected:
        raise CandidateReceiptError("expected artifact filename list is invalid")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise CandidateReceiptError("artifact is not a valid zip archive") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise CandidateReceiptError("artifact contains too many entries")
        names: dict[str, zipfile.ZipInfo] = {}
        seen_paths: set[str] = set()
        total_uncompressed = 0
        for info in infos:
            raw_name = info.filename
            if not raw_name or "\\" in raw_name or raw_name.startswith("/"):
                raise CandidateReceiptError("artifact contains an unsafe path")
            raw_parts = raw_name.rstrip("/").split("/")
            path = PurePosixPath(raw_name)
            if path.is_absolute() or any(part in {"", ".", ".."} for part in raw_parts):
                raise CandidateReceiptError("artifact contains path traversal")
            if info.is_dir() or len(raw_parts) != 1:
                raise CandidateReceiptError("artifact contains an unexpected directory entry")
            normalized = path.as_posix()
            if normalized in seen_paths:
                raise CandidateReceiptError("artifact contains duplicate archive paths")
            seen_paths.add(normalized)
            if info.flag_bits & 0x1:
                raise CandidateReceiptError("encrypted artifact entries are not supported")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise CandidateReceiptError("artifact contains a symbolic link")
            if mode not in {0, stat.S_IFREG}:
                raise CandidateReceiptError("artifact contains a non-regular file")
            basename = path.name
            if basename not in expected:
                raise CandidateReceiptError("artifact contains an unexpected file")
            if basename in names:
                raise CandidateReceiptError("artifact contains duplicate evidence filenames")
            if info.file_size <= 0 or info.file_size > MAX_ARCHIVE_FILE_BYTES:
                raise CandidateReceiptError("artifact entry is empty or exceeds the per-file limit")
            if info.file_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise CandidateReceiptError("artifact entry exceeds the uncompressed-size limit")
            if info.file_size > max(info.compress_size, 1) * 100 and info.file_size > 64_000:
                raise CandidateReceiptError("artifact entry has a suspicious compression ratio")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise CandidateReceiptError("artifact exceeds the total uncompressed-size limit")
            names[basename] = info

        if set(names) != expected:
            raise CandidateReceiptError("artifact is missing required evidence files")
        result: dict[str, dict[str, Any]] = {}
        for filename, info in names.items():
            try:
                raw = archive.read(info)
                parsed = json.loads(raw, object_pairs_hook=_unique_json_object)
            except CandidateReceiptError:
                raise
            except (zipfile.BadZipFile, OSError, RuntimeError, RecursionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateReceiptError(f"artifact JSON is invalid: {filename}") from exc
            if not isinstance(parsed, dict):
                raise CandidateReceiptError(f"artifact JSON is not an object: {filename}")
            result[filename] = parsed
        return result


def _assert_run_result(payload: Mapping[str, Any], *, expected_passed: int, expected_rate: float) -> None:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise CandidateReceiptError("Cloud run evidence has no result object")
    items = result.get("items")
    if not isinstance(items, list) or len(items) != 6:
        raise CandidateReceiptError("Cloud run must contain exactly six item results")
    scores: list[float] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise CandidateReceiptError("Cloud run item evidence is malformed")
        item_scores = item.get("scores")
        score = item_scores.get("overall_pass") if isinstance(item_scores, Mapping) else None
        if score is None or isinstance(score, bool):
            raise CandidateReceiptError("Cloud run item is missing a numeric overall_pass")
        try:
            numeric_score = float(score)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CandidateReceiptError("Cloud run overall_pass score is not numeric") from exc
        if not math.isfinite(numeric_score):
            raise CandidateReceiptError("Cloud run overall_pass score is not finite")
        scores.append(numeric_score)
    passed = sum(score == 1.0 for score in scores)
    run_scores = result.get("run_scores")
    run_score = run_scores.get("overall_pass_rate") if isinstance(run_scores, Mapping) else None
    if passed != expected_passed or run_score is None or isinstance(run_score, bool):
        raise CandidateReceiptError("Cloud run baseline does not match the expected pass count")
    try:
        actual_rate = float(run_score)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CandidateReceiptError("Cloud run score is not numeric") from exc
    if not math.isfinite(actual_rate) or not math.isclose(actual_rate, expected_rate, abs_tol=1e-9):
        raise CandidateReceiptError("Cloud run aggregate score does not match the expected baseline")
    run_url = payload.get("dataset_run_url")
    parsed_url = urlsplit(run_url) if isinstance(run_url, str) else None
    if (
        parsed_url is None
        or parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
    ):
        raise CandidateReceiptError("Cloud run evidence is missing a valid dataset run URL")


def summarize_cloud_evidence(archive_bytes: bytes) -> dict[str, Any]:
    files = read_json_archive(archive_bytes, EVIDENCE_FILES)
    bootstrap = files["bootstrap.json"]
    if bootstrap.get("items_upserted") != 6:
        raise CandidateReceiptError("Cloud bootstrap did not upsert the expected six dataset items")
    _assert_run_result(files["v1.json"], expected_passed=2, expected_rate=2 / 6)
    _assert_run_result(files["v2.json"], expected_passed=6, expected_rate=1.0)

    async_report = files["async-langfuse-link.json"]
    api_state = async_report.get("api_state")
    identity = async_report.get("remote_identity")
    if not isinstance(api_state, dict) or not isinstance(identity, dict):
        raise CandidateReceiptError("async link evidence is missing its API or remote identity")
    link_url = api_state.get("langfuse_experiment_url")
    if api_state.get("status") != "COMPLETED" or api_state.get("langfuse_sync_status") != "SYNCED":
        raise CandidateReceiptError("async launch did not complete and sync successfully")
    if not isinstance(link_url, str) or not link_url or async_report.get("api_detail_url") != link_url:
        raise CandidateReceiptError("async link is missing or inconsistent between API views")
    parsed_link = urlsplit(link_url)
    if parsed_link.scheme not in {"http", "https"} or not parsed_link.netloc or parsed_link.username or parsed_link.password:
        raise CandidateReceiptError("async link is not a valid absolute HTTP(S) URL")
    if not isinstance(identity.get("persisted_run_id"), str) or not identity["persisted_run_id"]:
        raise CandidateReceiptError("async link evidence is missing its persisted run identity")
    if identity.get("url_run_id") != identity.get("persisted_run_id"):
        raise CandidateReceiptError("async link URL does not match its persisted run identity")
    project_id = identity.get("url_project_id")
    dataset_id = identity.get("url_dataset_id")
    if not isinstance(project_id, str) or not project_id or not isinstance(dataset_id, str) or not dataset_id:
        raise CandidateReceiptError("async link URL is missing project or dataset identity")
    path_parts = [part for part in parsed_link.path.split("/") if part]
    try:
        datasets_index = path_parts.index("datasets")
        runs_index = path_parts.index("runs")
        link_project_id = path_parts[datasets_index - 1]
        link_dataset_id = path_parts[datasets_index + 1]
        link_run_id = path_parts[runs_index + 1]
    except (ValueError, IndexError) as exc:
        raise CandidateReceiptError("async link is not a Dataset Run URL") from exc
    if (link_project_id, link_dataset_id, link_run_id) != (
        project_id, dataset_id, identity.get("persisted_run_id")
    ):
        raise CandidateReceiptError("async link URL path does not match the verified remote identity")
    lookup = identity.get("remote_run_lookup")
    if not isinstance(lookup, dict) or lookup.get("status") not in {"CONFIRMED", "NOT_EXPOSED"}:
        raise CandidateReceiptError("async remote run lookup is neither confirmed nor explicitly unavailable")

    return {
        "suite_results": {suite: "passed" for suite in REQUIRED_SUITES},
        "baseline": {"v1": {"passed": 2, "total": 6}, "v2": {"passed": 6, "total": 6}},
        "evidence_summary": {
            "bootstrap_items_upserted": 6,
            "v1_items": 6,
            "v2_items": 6,
            "async_link_verified": True,
            "remote_run_readback": lookup["status"],
        },
    }


def _pr_identity(pr: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    return head.get("sha"), base.get("sha"), head_repo.get("full_name")


def classify_pr_risk(client: GitHubClient, pr: Mapping[str, Any]) -> dict[str, str]:
    """Fetch the complete PR diff through read-only API calls and classify it."""
    files = client.list_pull_request_files(int(pr["number"]))
    base_sha = (pr.get("base") or {}).get("sha")
    head_sha = (pr.get("head") or {}).get("sha")
    if not _valid_sha(base_sha) or not _valid_sha(head_sha):
        raise CandidateReceiptError("PR base/head SHA is missing or malformed")
    base_modes, base_complete = client.get_tree_modes(base_sha)
    head_modes, head_complete = client.get_tree_modes(head_sha)
    if not base_complete or not head_complete:
        return {"risk": "high", "reason": "base or head tree is truncated"}

    file_modes: dict[str, dict[str, str | None]] = {}
    contents: dict[str, tuple[str, str]] = {}
    for item in files:
        path = item.get("filename")
        if not isinstance(path, str):
            return {"risk": "high", "reason": "changed-file path is malformed"}
        old_path = item.get("previous_filename")
        file_modes[path] = {
            "base": base_modes.get(str(old_path or path)),
            "head": head_modes.get(path),
        }
        if path in {"services/console/package.json", "services/console/pnpm-lock.yaml"}:
            if file_modes[path]["base"] == "100644" and file_modes[path]["head"] == "100644":
                contents[path] = (
                    client.get_file_text(path, base_sha),
                    client.get_file_text(path, head_sha),
                )
    return classify_change_set(
        files=files,
        contents=contents,
        file_modes=file_modes,
        files_complete=len(files) < 3000,
    )


def _find_pr_in_run(run: Mapping[str, Any], *, pr_number: int, head_sha: str, base_sha: str) -> bool:
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return False
    for related in pull_requests:
        if not isinstance(related, dict):
            continue
        related_head, related_base, _ = _pr_identity(related)
        if related.get("number") == pr_number and related_head == head_sha and related_base == base_sha:
            return True
    return False


def _require_required_ci_jobs(client: GitHubClient, run: Mapping[str, Any]) -> None:
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    if not isinstance(run_id, int) or run_id <= 0 or not isinstance(attempt, int) or attempt <= 0:
        raise CandidateReceiptError("current regular CI run has invalid identity")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise CandidateReceiptError("latest current-identity regular CI run is not successful")
    jobs = client.list_jobs(run_id, attempt)
    for name in REQUIRED_CI_CHECKS:
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
            raise CandidateReceiptError(f"regular CI job is not successful for current identity: {name}")


def get_current_ci_run(client: GitHubClient, pr: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the newest CI run matching the exact current PR head and base."""
    head_sha, base_sha, _ = _pr_identity(pr)
    if not _valid_sha(head_sha) or not _valid_sha(base_sha):
        raise CandidateReceiptError("PR head/base SHA is malformed")
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    branch = head.get("ref")
    if not isinstance(branch, str) or not branch:
        raise CandidateReceiptError("PR head branch is missing")
    workflow_id = client.get_workflow_id(CI_WORKFLOW_PATH)
    runs = client.list_workflow_runs(
        workflow_id,
        params={"event": "pull_request", "branch": branch},
    )
    matches = [
        run
        for run in runs
        if run.get("event") == "pull_request"
        and str(run.get("path", "")).lstrip("/") == CI_WORKFLOW_PATH
        and _find_pr_in_run(
            run,
            pr_number=int(pr["number"]),
            head_sha=head_sha,
            base_sha=base_sha,
        )
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda run: (
            str(run.get("updated_at") or run.get("run_started_at") or run.get("created_at", "")),
            str(run.get("run_started_at", "")),
            str(run.get("created_at", "")),
            int(run.get("run_attempt", 0) or 0),
            int(run.get("id", 0) or 0),
        )
    )
    return matches[-1]


def current_ci_check_states(client: GitHubClient, run: Mapping[str, Any] | None) -> dict[str, str]:
    """Map the latest exact-identity workflow attempt to fail-closed check states."""
    if run is None or run.get("status") != "completed":
        return {name: "pending" for name in REQUIRED_CI_CHECKS}
    if run.get("conclusion") != "success":
        return {name: "failure" for name in REQUIRED_CI_CHECKS}
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt <= 0
    ):
        raise CandidateReceiptError("current regular CI run has invalid identity")
    jobs = client.list_jobs(run_id, attempt)
    states: dict[str, str] = {}
    for name in REQUIRED_CI_CHECKS:
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            states[name] = "failure"
            continue
        job = matches[0]
        if job.get("status") != "completed":
            states[name] = "pending"
        elif job.get("conclusion") == "success":
            states[name] = "success"
        else:
            states[name] = "failure"
    return states


def find_current_ci_run(client: GitHubClient, pr: Mapping[str, Any]) -> dict[str, Any]:
    """Require the newest exact-identity regular CI run and all required jobs green."""
    latest = get_current_ci_run(client, pr)
    if latest is None:
        raise CandidateReceiptError("no regular CI run matches the exact current PR head and base")
    _require_required_ci_jobs(client, latest)
    return latest


def _assert_merge_commit(client: GitHubClient, tested_sha: str, base_sha: str, head_sha: str) -> None:
    if not _valid_sha(tested_sha):
        raise CandidateReceiptError("GitHub has not produced a full merge commit SHA for this PR")
    commit = client.request_json(f"{client.repo_path}/commits/{tested_sha}")
    parents = commit.get("parents") if isinstance(commit, dict) else None
    parent_shas = [parent.get("sha") for parent in parents if isinstance(parent, dict)] if isinstance(parents, list) else []
    if len(parent_shas) != 2 or set(parent_shas) != {base_sha, head_sha}:
        raise CandidateReceiptError("PR merge commit parents do not match the expected current base and head")


def prepare_candidate_manifest(
    client: GitHubClient,
    *,
    repository: str,
    pr_number: int,
    expected_head_sha: str,
    expected_base_sha: str,
    workflow_ref: str,
    workflow_revision: str,
    run_id: int,
    run_attempt: int,
    requested_by: str,
) -> dict[str, Any]:
    if repository != REPOSITORY:
        raise CandidateReceiptError("trusted Cloud validation is restricted to the configured repository")
    if not _valid_sha(expected_head_sha) or not _valid_sha(expected_base_sha) or not _valid_sha(workflow_revision):
        raise CandidateReceiptError("expected and workflow revisions must be full 40-character SHAs")
    expected_ref = f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@{MAIN_REF}"
    if workflow_ref != expected_ref:
        raise CandidateReceiptError("candidate controller must be dispatched from the trusted main workflow")
    if pr_number <= 0 or run_id <= 0 or run_attempt <= 0:
        raise CandidateReceiptError("PR/run identifiers must be positive integers")

    pr = client.get_pull_request(pr_number)
    head_sha, base_sha, head_repo = _pr_identity(pr)
    if pr.get("state") != "open" or (pr.get("base") or {}).get("ref") != "main":
        raise CandidateReceiptError("candidate PR must be open and target main")
    if head_repo != repository:
        raise CandidateReceiptError("fork pull requests are not eligible for trusted Cloud validation")
    if head_sha != expected_head_sha or base_sha != expected_base_sha:
        raise CandidateReceiptError("candidate PR head or base changed after the expected SHA was supplied")
    current_main = client.get_ref("heads/main").get("object", {}).get("sha")
    if current_main != base_sha:
        raise CandidateReceiptError("candidate base is not the current main branch head")

    merge_sha = pr.get("merge_commit_sha")
    _assert_merge_commit(client, str(merge_sha or ""), base_sha, head_sha)
    risk = classify_pr_risk(client, pr)
    if risk.get("risk") != "high":
        raise CandidateReceiptError("trusted Cloud candidate workflow is reserved for high-risk changes")
    ci_run = find_current_ci_run(client, pr)
    return {
        "schema_version": 1,
        "repository": repository,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "tested_sha": merge_sha,
        "risk": risk["risk"],
        "risk_reason": risk["reason"],
        "workflow_path": CANDIDATE_WORKFLOW,
        "workflow_revision": workflow_revision,
        "workflow_ref": workflow_ref,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "requested_by": requested_by,
        "ci_run_id": int(ci_run["id"]),
        "ci_run_attempt": int(ci_run["run_attempt"]),
        "ci_run_url": str(ci_run.get("html_url", "")),
        "created_at": str(ci_run.get("created_at", "")),
    }


def verify_candidate_unchanged(client: GitHubClient, manifest: Mapping[str, Any]) -> None:
    """Recheck the exact approved PR, main base, merge parents, and CI attempt."""
    if manifest.get("repository") != REPOSITORY or manifest.get("workflow_path") != CANDIDATE_WORKFLOW:
        raise CandidateReceiptError("candidate manifest is not from the trusted repository workflow")
    for field in ("head_sha", "base_sha", "tested_sha", "workflow_revision"):
        if not _valid_sha(manifest.get(field)):
            raise CandidateReceiptError(f"candidate manifest has an invalid {field}")
    for field in ("pr_number", "ci_run_id", "ci_run_attempt"):
        if isinstance(manifest.get(field), bool) or not isinstance(manifest.get(field), int) or manifest[field] <= 0:
            raise CandidateReceiptError(f"candidate manifest has an invalid {field}")

    pr = client.get_pull_request(manifest["pr_number"])
    head_sha, base_sha, head_repo = _pr_identity(pr)
    if (
        pr.get("state") != "open"
        or (pr.get("base") or {}).get("ref") != "main"
        or head_repo != REPOSITORY
        or head_sha != manifest["head_sha"]
        or base_sha != manifest["base_sha"]
        or pr.get("merge_commit_sha") != manifest["tested_sha"]
    ):
        raise CandidateReceiptError("candidate PR head, base, repository, or merge SHA changed after approval")
    current_main = client.get_ref("heads/main").get("object", {}).get("sha")
    if current_main != manifest["base_sha"]:
        raise CandidateReceiptError("candidate base no longer matches current main")
    _assert_merge_commit(client, manifest["tested_sha"], manifest["base_sha"], manifest["head_sha"])
    current_ci = find_current_ci_run(client, pr)
    if (current_ci.get("id"), current_ci.get("run_attempt")) != (
        manifest["ci_run_id"], manifest["ci_run_attempt"]
    ):
        raise CandidateReceiptError("current regular CI run changed after candidate preparation")


def _approved_candidate_environment(approvals: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    for record in approvals:
        if record.get("state") != "approved":
            continue
        user = record.get("user") if isinstance(record.get("user"), dict) else {}
        for environment in record.get("environments", []):
            if not isinstance(environment, dict) or environment.get("name") != CANDIDATE_ENVIRONMENT:
                continue
            login = user.get("login")
            user_id = user.get("id")
            if (
                not isinstance(login, str)
                or not login
                or isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id <= 0
            ):
                continue
            environment_id = environment.get("id")
            if isinstance(environment_id, bool) or not isinstance(environment_id, int) or environment_id <= 0:
                continue
            return {
                "state": "approved",
                "environment": CANDIDATE_ENVIRONMENT,
                "environment_id": environment_id,
                "environment_url": str(environment.get("html_url") or environment.get("url") or ""),
                "login": login,
                "user_id": user_id,
                "source": "actions-run-approvals",
            }
    return None


def _candidate_job(jobs: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [job for job in jobs if job.get("name") == name]
    return dict(matches[0]) if len(matches) == 1 else None


def _step_result(job: Mapping[str, Any], name: str) -> str | None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    matches = [step for step in steps if isinstance(step, dict) and step.get("name") == name]
    if len(matches) != 1:
        return None
    step = matches[0]
    if step.get("status") != "completed":
        return "incomplete"
    return str(step.get("conclusion") or "unknown")


def build_candidate_receipt(
    manifest: Mapping[str, Any],
    *,
    workflow_run: Mapping[str, Any],
    current_pr: Mapping[str, Any],
    approvals: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
    cloud_evidence_zip: bytes | None,
) -> dict[str, Any]:
    if manifest.get("schema_version") != 1 or manifest.get("repository") != REPOSITORY:
        raise CandidateReceiptError("trusted candidate manifest has an unsupported schema or repository")
    for name in ("head_sha", "base_sha", "tested_sha", "workflow_revision"):
        if not _valid_sha(manifest.get(name)):
            raise CandidateReceiptError(f"trusted candidate manifest has an invalid {name}")
    for name in ("pr_number", "run_id", "run_attempt", "ci_run_id", "ci_run_attempt"):
        if isinstance(manifest.get(name), bool) or not isinstance(manifest.get(name), int) or manifest[name] <= 0:
            raise CandidateReceiptError(f"trusted candidate manifest has an invalid {name}")
    if manifest.get("workflow_path") != CANDIDATE_WORKFLOW or manifest.get("risk") != "high":
        raise CandidateReceiptError("candidate manifest is not a high-risk trusted-workflow candidate")
    expected_workflow_ref = f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@{MAIN_REF}"
    if manifest.get("workflow_ref") != expected_workflow_ref:
        raise CandidateReceiptError("candidate manifest was not dispatched from trusted main")

    run_path = str(workflow_run.get("path", "")).lstrip("/")
    trusted_identity = (
        workflow_run.get("id") == manifest.get("run_id")
        and workflow_run.get("run_attempt") == manifest.get("run_attempt")
        and workflow_run.get("event") == "workflow_dispatch"
        and workflow_run.get("head_branch") == "main"
        and workflow_run.get("head_sha") == manifest.get("workflow_revision")
        and (workflow_run.get("actor") or {}).get("login") == manifest.get("requested_by")
        and run_path == f"{CANDIDATE_WORKFLOW}@{MAIN_REF}"
        and workflow_run.get("status") in {"in_progress", "completed"}
    )
    if not trusted_identity:
        raise CandidateReceiptError("candidate workflow run does not match the trusted main manifest")

    current_head, current_base, current_repo = _pr_identity(current_pr)
    current = (
        current_pr.get("state") == "open"
        and (current_pr.get("base") or {}).get("ref") == "main"
        and current_repo == REPOSITORY
        and current_head == manifest.get("head_sha")
        and current_base == manifest.get("base_sha")
        and current_pr.get("merge_commit_sha") == manifest.get("tested_sha")
    )
    approval = _approved_candidate_environment(approvals)
    prepare_job = _candidate_job(jobs, CANDIDATE_PREPARE_JOB_NAME)
    prepare_job_id = int(prepare_job.get("id", 0)) if prepare_job else 0
    prepare_job_passed = bool(
        prepare_job
        and prepare_job.get("status") == "completed"
        and prepare_job.get("conclusion") == "success"
        and _step_result(prepare_job, CANDIDATE_PREPARE_STEP) == "success"
    )
    cloud_job = _candidate_job(jobs, CANDIDATE_JOB_NAME)
    cloud_job_id = int(cloud_job.get("id", 0)) if cloud_job else 0
    cloud_job_passed = bool(
        cloud_job
        and cloud_job.get("status") == "completed"
        and cloud_job.get("conclusion") == "success"
        and _step_result(cloud_job, CANDIDATE_SECRET_STEP) == "success"
        and _step_result(cloud_job, CANDIDATE_E2E_STEP) == "success"
    )

    evidence: dict[str, Any] | None = None
    if cloud_evidence_zip is not None:
        try:
            evidence = summarize_cloud_evidence(cloud_evidence_zip)
        except CandidateReceiptError:
            evidence = None
    passed = bool(current and approval and prepare_job_passed and cloud_job_passed and evidence)
    if workflow_run.get("status") == "completed" and workflow_run.get("conclusion") != "success":
        passed = False

    fallback_approval = approval or {
        "state": "not_approved",
        "environment": CANDIDATE_ENVIRONMENT,
        "login": "",
        "source": "actions-run-approvals",
    }
    suite_results = evidence["suite_results"] if evidence else {suite: "failed" for suite in REQUIRED_SUITES}
    baseline = evidence["baseline"] if evidence else {
        "v1": {"passed": 0, "total": 0},
        "v2": {"passed": 0, "total": 0},
    }
    run_url = str(workflow_run.get("html_url", ""))
    receipt = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr_number": manifest["pr_number"],
        "head_sha": manifest["head_sha"],
        "base_sha": manifest["base_sha"],
        "tested_sha": manifest["tested_sha"],
        "risk": "high",
        "workflow_path": CANDIDATE_WORKFLOW,
        "workflow_revision": manifest["workflow_revision"],
        "workflow_ref": manifest["workflow_ref"],
        "dispatch_actor": (workflow_run.get("actor") or {}).get("login", ""),
        "triggering_actor": (workflow_run.get("triggering_actor") or {}).get("login", ""),
        "run_id": manifest["run_id"],
        "run_attempt": manifest["run_attempt"],
        "run_url": run_url,
        "prepare_job_id": prepare_job_id,
        "prepare_job_url": str(prepare_job.get("html_url", "")) if prepare_job else "",
        "cloud_job_id": cloud_job_id,
        "cloud_job_url": str(cloud_job.get("html_url", "")) if cloud_job else "",
        "environment": CANDIDATE_ENVIRONMENT,
        "cloud_state": "passed" if passed else "failed",
        "failure_reason": "" if passed else "identity, approval, Cloud steps, or suite evidence did not pass",
        "approval": fallback_approval,
        "ci_run_id": manifest["ci_run_id"],
        "ci_run_attempt": manifest["ci_run_attempt"],
        "ci_run_url": manifest["ci_run_url"],
        "required_suites": list(REQUIRED_SUITES),
        "suite_results": suite_results,
        "baseline": baseline,
        "evidence_summary": evidence.get("evidence_summary", {}) if evidence else {},
    }
    expected_identity = {
        key: manifest[key]
        for key in ("repository", "pr_number", "head_sha", "base_sha", "tested_sha", "workflow_revision", "run_id", "run_attempt", "ci_run_id", "ci_run_attempt")
    }
    if passed and not validate_trusted_receipt(receipt, expected_identity):
        raise CandidateReceiptError("constructed candidate receipt does not satisfy the trusted schema")
    return receipt


def _artifact_bytes(client: GitHubClient, run_id: int, exact_name: str) -> bytes | None:
    matches = [
        item
        for item in client.list_artifacts(run_id)
        if item.get("name") == exact_name and not item.get("expired", False)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise CandidateReceiptError("multiple artifacts match the exact evidence name")
    artifact = matches[0]
    size = artifact.get("size_in_bytes")
    artifact_id = artifact.get("id")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > MAX_ARTIFACT_BYTES:
        raise CandidateReceiptError("artifact metadata reports an invalid or oversized archive")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
        raise CandidateReceiptError("artifact metadata has an invalid identifier")
    data = client.download_artifact(artifact_id, max_bytes=MAX_ARTIFACT_BYTES)
    if len(data) > MAX_ARTIFACT_BYTES:
        raise CandidateReceiptError("downloaded artifact exceeds the configured size limit")
    return data


def _client_from_environment() -> GitHubClient:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    return GitHubClient(
        api_url=os.environ["GITHUB_API_URL"],
        repository=repository,
        token=os.environ["GITHUB_TOKEN"],
    )


def _write_summary(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(text.rstrip() + "\n", encoding="utf-8")


def _prepare_cli() -> int:
    client = _client_from_environment()
    try:
        manifest = prepare_candidate_manifest(
            client,
            repository=os.environ["GITHUB_REPOSITORY"],
            pr_number=int(os.environ["PR_NUMBER"]),
            expected_head_sha=os.environ["EXPECTED_HEAD_SHA"],
            expected_base_sha=os.environ["EXPECTED_BASE_SHA"],
            workflow_ref=os.environ["GITHUB_WORKFLOW_REF"],
            workflow_revision=os.environ["GITHUB_WORKFLOW_SHA"],
            run_id=int(os.environ["GITHUB_RUN_ID"]),
            run_attempt=int(os.environ["GITHUB_RUN_ATTEMPT"]),
            requested_by=os.environ.get("GITHUB_ACTOR", "unknown"),
        )
    except (GitHubApiError, CandidateReceiptError, KeyError, ValueError) as exc:
        print(f"Trusted candidate preparation failed closed ({type(exc).__name__})", file=sys.stderr)
        return 1
    output = Path(os.environ.get("MANIFEST_PATH", "artifacts/candidate/manifest.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        handle.write(f"tested_sha={manifest['tested_sha']}\n")
        handle.write(f"head_sha={manifest['head_sha']}\n")
        handle.write(f"base_sha={manifest['base_sha']}\n")
    print(f"Prepared high-risk PR #{manifest['pr_number']} exact candidate {manifest['tested_sha']}")
    print(f"Current regular CI: {manifest['ci_run_url']} ({manifest['ci_run_id']}/{manifest['ci_run_attempt']})")
    _write_summary(
        "## Trusted Cloud candidate manifest\n\n"
        f"- PR: `{manifest['pr_number']}`\n"
        f"- Risk: `high` ({manifest['risk_reason']})\n"
        f"- Head/base/tested SHA: `{manifest['head_sha']}` / `{manifest['base_sha']}` / `{manifest['tested_sha']}`\n"
        f"- Trusted workflow revision: `{manifest['workflow_revision']}`\n"
        f"- Current regular CI: [{manifest['ci_run_id']}/{manifest['ci_run_attempt']}]({manifest['ci_run_url']})\n"
        "- Approval: awaiting `langfuse-e2e-candidate` environment reviewer; candidate checkout remains unstarted.\n"
    )
    return 0


def _verify_cli() -> int:
    try:
        client = _client_from_environment()
        run_id = int(os.environ["GITHUB_RUN_ID"])
        attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
        manifest_name = f"{MANIFEST_ARTIFACT_PREFIX}-{run_id}-{attempt}"
        archive = _artifact_bytes(client, run_id, manifest_name)
        if archive is None:
            raise CandidateReceiptError("trusted preparation manifest artifact is missing")
        manifest = read_json_archive(archive, ("manifest.json",))["manifest.json"]
        if int(os.environ["PR_NUMBER"]) != manifest.get("pr_number"):
            raise CandidateReceiptError("dispatch PR number does not match the frozen manifest")
        verify_candidate_unchanged(client, manifest)
        values = {name: os.environ.get(name, "") for name in REQUIRED_SECRETS}
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise CandidateReceiptError("required candidate test secrets are missing: " + ", ".join(missing))
        if any("\n" in value or "\r" in value for value in values.values()):
            raise CandidateReceiptError("candidate test secrets contain unsupported line breaks")
        candidate_dir = Path(os.environ["CANDIDATE_DIR"]).resolve(strict=True)
        workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
        if candidate_dir == workspace or workspace not in candidate_dir.parents or not candidate_dir.is_dir():
            raise CandidateReceiptError("candidate checkout path is outside the isolated workspace directory")
        env_path = candidate_dir / ".env.cloud"
        if env_path.exists() or env_path.is_symlink():
            raise CandidateReceiptError("candidate Cloud environment file already exists")
        os.umask(0o077)
        env_path.write_text(
            "".join(f"{name}={value}\n" for name, value in values.items())
            + f"ARGUS_LANGFUSE_DASHBOARD_URL={values['LANGFUSE_BASE_URL']}\n",
            encoding="utf-8",
        )
    except (GitHubApiError, CandidateReceiptError, KeyError, ValueError, OSError) as exc:
        print(f"Trusted candidate revalidation failed closed ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"Revalidated approved Cloud candidate PR #{manifest['pr_number']} at frozen SHA {manifest['tested_sha']}")
    return 0


def _record_cli() -> int:
    client = _client_from_environment()
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    manifest_name = f"{MANIFEST_ARTIFACT_PREFIX}-{run_id}-{attempt}"
    evidence_name = f"{EVIDENCE_ARTIFACT_PREFIX}-{run_id}-{attempt}"
    try:
        manifest_zip = _artifact_bytes(client, run_id, manifest_name)
        if manifest_zip is None:
            raise CandidateReceiptError("trusted preparation manifest artifact is missing")
        manifest = read_json_archive(manifest_zip, ("manifest.json",))["manifest.json"]
        evidence_zip = _artifact_bytes(client, run_id, evidence_name)
        workflow_run = client.get_run(run_id)
        current_pr = client.get_pull_request(int(manifest["pr_number"]))
        approvals = client.list_run_approvals(run_id)
        jobs = client.list_jobs(run_id, attempt)
        receipt = build_candidate_receipt(
            manifest,
            workflow_run=workflow_run,
            current_pr=current_pr,
            approvals=approvals,
            jobs=jobs,
            cloud_evidence_zip=evidence_zip,
        )
    except (GitHubApiError, CandidateReceiptError, KeyError, ValueError) as exc:
        print(f"Trusted candidate receipt rejected ({type(exc).__name__})", file=sys.stderr)
        return 1

    output = Path(os.environ.get("RECEIPT_PATH", "artifacts/candidate-receipt/receipt.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if Path(os.environ.get("GITHUB_OUTPUT", "/dev/null")).exists():
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
            handle.write(f"cloud_state={receipt['cloud_state']}\n")
    _write_summary(
        "## Trusted candidate Cloud receipt\n\n"
        f"- Cloud state: `{receipt['cloud_state']}`\n"
        f"- PR/head/base/tested SHA: `{receipt['pr_number']}` / `{receipt['head_sha']}` / `{receipt['base_sha']}` / `{receipt['tested_sha']}`\n"
        f"- Workflow/run/attempt: `{receipt['workflow_revision']}` / `{receipt['run_id']}` / `{receipt['run_attempt']}`\n"
        f"- Prepare job: [{receipt['prepare_job_id']}]({receipt['prepare_job_url']})\n"
        f"- Environment approval: `{receipt['approval']['state']}` by `{receipt['approval'].get('login', 'unknown')}`\n"
        f"- Suites: managed-cloud `{receipt['suite_results']['managed-cloud']}`, async-link `{receipt['suite_results']['async-link']}`\n"
        f"- Baseline: v1 `{receipt['baseline']['v1']['passed']}/{receipt['baseline']['v1']['total']}`, v2 `{receipt['baseline']['v2']['passed']}/{receipt['baseline']['v2']['total']}`\n"
        f"- Evidence: {receipt['run_url']}\n"
    )
    print(f"Trusted candidate Cloud receipt: {receipt['cloud_state']}")
    return 0 if receipt["cloud_state"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_subparsers(dest="command", required=True)
    parser.parse_args()
    command = sys.argv[1]
    if command == "prepare":
        return _prepare_cli()
    if command == "verify":
        return _verify_cli()
    if command == "record":
        return _record_cli()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
