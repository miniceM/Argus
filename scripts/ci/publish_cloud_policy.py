"""Evaluate and publish the current PR Cloud integration policy from trusted CI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.ci.ci_constants import (
    AUTOMATION_ACTOR,
    CANDIDATE_WORKFLOW,
    MAIN_REF,
    POLICY_CONTEXT,
    REPOSITORY,
)
from scripts.ci.cloud_e2e_policy import (
    evaluate_integration_policy,
    validate_trusted_receipt,
)
from scripts.ci.cloud_e2e_receipt import (
    CandidateReceiptError,
    _artifact_bytes,
    _pr_identity,
    build_candidate_receipt,
    classify_pr_risk,
    current_ci_check_states,
    get_current_ci_run,
    read_json_archive,
    verify_candidate_unchanged,
)
from scripts.ci.github_api import GitHubApiError, GitHubClient

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
CANDIDATE_RUN_PATH = CANDIDATE_WORKFLOW
CANDIDATE_DISPATCH_URL = f"https://github.com/{REPOSITORY}/actions/workflows/cloud-e2e-trusted.yml"
MANIFEST_PREFIX = "candidate-manifest"
EVIDENCE_PREFIX = "candidate-cloud-evidence"
RECEIPT_PREFIX = "trusted-cloud-receipt"
MAX_DECISION_FILE_BYTES = 2_000_000
MAX_DECISIONS = 500
POLICY_STATES = frozenset(
    {
        "passed",
        "waiting_for_ci",
        "required_ci_failed",
        "awaiting_trusted_validation",
        "trusted_validation_failed",
        "invalid_trusted_receipt",
        "waiting_for_current_base",
        "policy_evaluation_error",
        "pull_request_not_eligible",
    }
)


class PolicyPublisherError(RuntimeError):
    """An untrusted or stale policy decision cannot be published."""


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _schema_v1(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and SHA_RE.fullmatch(value) is not None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyPublisherError("policy artifact contains duplicate JSON keys")
        result[key] = value
    return result


def _decision(
    *,
    pr_number: int,
    head_sha: Any,
    base_sha: Any,
    status: str,
    policy_state: str,
    cloud_state: str,
    risk: str,
    reason: str,
    target_url: str | None = None,
) -> dict[str, Any]:
    description = reason.strip() or policy_state.replace("_", " ")
    result: dict[str, Any] = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "status": status,
        "policy_state": policy_state,
        "cloud_state": cloud_state,
        "risk": risk,
        "reason": description,
        "description": description[:140],
    }
    if target_url:
        result["target_url"] = target_url
    return result


def _run_sort_key(run: Mapping[str, Any]) -> tuple[str, str, int, int]:
    attempt = run.get("run_attempt", 0)
    run_id = run.get("id", 0)
    return (
        str(run.get("updated_at") or run.get("run_started_at") or run.get("created_at", "")),
        str(run.get("run_started_at", "")),
        attempt if _positive_int(attempt) else 0,
        run_id if _positive_int(run_id) else 0,
    )


def _run_mentions_pr(run: Mapping[str, Any], pr_number: int) -> bool:
    title = run.get("display_title") or run.get("name")
    return isinstance(title, str) and title.strip() == f"Trusted Cloud Candidate PR #{pr_number}"


def _read_artifact_json(client: Any, run_id: int, name: str, filename: str) -> dict[str, Any] | None:
    archive = _artifact_bytes(client, run_id, name)
    if archive is None:
        return None
    documents = read_json_archive(archive, (filename,))
    return documents[filename]


def _candidate_run_matches_control_plane(run: Mapping[str, Any]) -> bool:
    return (
        run.get("event") == "workflow_dispatch"
        and (run.get("actor") or {}).get("login") == AUTOMATION_ACTOR
        and run.get("head_branch") == "main"
        and str(run.get("path", "")).lstrip("/") == CANDIDATE_RUN_PATH
    )


def _candidate_result(
    state: str,
    reason: str,
    *,
    cloud_state: str = "skipped_by_policy",
    receipt: Mapping[str, Any] | None = None,
    target_url: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"state": state, "reason": reason, "cloud_state": cloud_state}
    if receipt is not None:
        result["receipt"] = dict(receipt)
    if target_url:
        result["target_url"] = target_url
    return result


def find_trusted_candidate_validation(
    client: Any,
    pr: Mapping[str, Any],
    *,
    current_main_sha: str,
) -> dict[str, Any]:
    """Recompute the latest matching trusted Cloud result from GitHub API evidence.

    The receipt artifact is only a consistency check. Admission is based on the
    manifest, live run/attempt, automated CI identity, actual jobs/steps, Cloud
    evidence, current PR identity, and the receipt recomputed by this trusted code.
    """
    pr_number = pr.get("number")
    head_sha, base_sha, head_repo = _pr_identity(pr)
    if not _positive_int(pr_number) or not _is_sha(head_sha) or not _is_sha(base_sha):
        return _candidate_result("pending", "current PR identity is incomplete")
    if head_repo != REPOSITORY or (pr.get("base") or {}).get("ref") != "main":
        return _candidate_result("pending", "trusted validation is limited to same-repository PRs targeting main")
    if not _is_sha(current_main_sha) or base_sha != current_main_sha:
        return _candidate_result("pending", "PR base is not the current main revision")

    workflow_id = client.get_workflow_id(CANDIDATE_WORKFLOW)
    runs = client.list_workflow_runs(
        workflow_id,
        params={"event": "workflow_dispatch", "branch": "main"},
    )
    candidates: list[tuple[dict[str, Any], dict[str, Any] | None, str | None]] = []
    for raw_run in runs:
        if not isinstance(raw_run, dict) or not _candidate_run_matches_control_plane(raw_run):
            continue
        run_id = raw_run.get("id")
        attempt = raw_run.get("run_attempt")
        if not _positive_int(run_id) or not _positive_int(attempt):
            if _run_mentions_pr(raw_run, pr_number):
                candidates.append((raw_run, None, "candidate run identity is malformed"))
            continue
        manifest_name = f"{MANIFEST_PREFIX}-{run_id}-{attempt}"
        try:
            manifest = _read_artifact_json(client, run_id, manifest_name, "manifest.json")
        except CandidateReceiptError as exc:
            if _run_mentions_pr(raw_run, pr_number):
                candidates.append((raw_run, None, str(exc)))
            continue
        if manifest is None:
            if _run_mentions_pr(raw_run, pr_number):
                candidates.append((raw_run, None, "candidate manifest is missing or expired"))
            continue
        if manifest.get("pr_number") != pr_number:
            continue
        # A well-formed manifest for a prior head/base is stale evidence, not a
        # failed attempt for the current identity. It cannot supersede a current one.
        if manifest.get("head_sha") != head_sha or manifest.get("base_sha") != base_sha:
            continue
        candidates.append((raw_run, manifest, None))

    if not candidates:
        return _candidate_result("pending", "awaiting automatic trusted Cloud validation")
    candidates.sort(key=lambda item: _run_sort_key(item[0]))
    listed_run, manifest, manifest_error = candidates[-1]
    run_id = listed_run.get("id")
    attempt = listed_run.get("run_attempt")
    target_url = listed_run.get("html_url") if isinstance(listed_run.get("html_url"), str) else None
    if manifest is None or manifest_error:
        return _candidate_result(
            "failure",
            "invalid_trusted_receipt",
            cloud_state="failed",
            target_url=target_url,
        )
    if not _positive_int(run_id) or not _positive_int(attempt):
        return _candidate_result("failure", "invalid_trusted_receipt", cloud_state="failed", target_url=target_url)

    # Re-fetch the run because workflow_run events may be delivered out of order,
    # or the same run may have been rerun since it was listed.
    run = client.get_run(run_id)
    if not isinstance(run, dict) or not _candidate_run_matches_control_plane(run):
        return _candidate_result("failure", "invalid_trusted_receipt", cloud_state="failed", target_url=target_url)
    if run.get("run_attempt") != attempt:
        return _candidate_result("pending", "candidate attempt changed while policy was evaluated", target_url=target_url)
    if run.get("id") != run_id or run.get("head_sha") != manifest.get("workflow_revision"):
        return _candidate_result("failure", "invalid_trusted_receipt", cloud_state="failed", target_url=target_url)
    if manifest.get("workflow_path") != CANDIDATE_WORKFLOW or manifest.get("workflow_ref") != f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@{MAIN_REF}":
        return _candidate_result("failure", "invalid_trusted_receipt", cloud_state="failed", target_url=target_url)
    if manifest.get("workflow_revision") != current_main_sha:
        return _candidate_result("pending", "candidate used an older main control revision", target_url=target_url)
    if manifest.get("run_id") != run_id or manifest.get("run_attempt") != attempt:
        return _candidate_result("failure", "invalid_trusted_receipt", cloud_state="failed", target_url=target_url)
    if run.get("status") != "completed":
        return _candidate_result("pending", "automatic trusted Cloud validation is running", target_url=target_url)
    if run.get("conclusion") != "success":
        return _candidate_result(
            "failure",
            "trusted_validation_failed",
            cloud_state="failed",
            target_url=target_url,
        )

    evidence_name = f"{EVIDENCE_PREFIX}-{run_id}-{attempt}"
    receipt_name = f"{RECEIPT_PREFIX}-{run_id}-{attempt}"
    try:
        evidence_zip = _artifact_bytes(client, run_id, evidence_name)
        receipt_artifact = _read_artifact_json(client, run_id, receipt_name, "receipt.json")
        if evidence_zip is None or receipt_artifact is None:
            return _candidate_result(
                "failure",
                "invalid_trusted_receipt",
                cloud_state="failed",
                target_url=target_url,
            )
        jobs = client.list_jobs(run_id, attempt)
        # This rechecks current PR/base/merge parents and the exact regular-CI
        # attempt captured by the candidate manifest before accepting the result.
        verify_candidate_unchanged(client, manifest)
        current_pr = client.get_pull_request(pr_number)
        recomputed = build_candidate_receipt(
            manifest,
            workflow_run=run,
            current_pr=current_pr,
            jobs=jobs,
            cloud_evidence_zip=evidence_zip,
        )
    except CandidateReceiptError as exc:
        message = str(exc)
        if any(token in message for token in ("changed after CI", "no longer matches current main", "current regular CI run changed")):
            return _candidate_result("pending", "candidate identity or regular CI changed during validation", target_url=target_url)
        return _candidate_result("failure", "invalid_trusted_receipt", cloud_state="failed", target_url=target_url)

    expected = {
        name: manifest.get(name)
        for name in (
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
    }
    if (
        not validate_trusted_receipt(recomputed, expected)
        or recomputed.get("cloud_state") != "passed"
        or receipt_artifact != recomputed
    ):
        return _candidate_result(
            "failure",
            "invalid_trusted_receipt",
            cloud_state="failed",
            target_url=target_url,
        )
    return _candidate_result(
        "passed",
        "trusted Cloud evidence matches the current PR identity",
        cloud_state="passed",
        receipt=recomputed,
        target_url=target_url,
    )


def _base_decision(pr_number: int, pr: Mapping[str, Any]) -> dict[str, Any]:
    head_sha, base_sha, _ = _pr_identity(pr)
    return _decision(
        pr_number=pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        status="error",
        policy_state="policy_evaluation_error",
        cloud_state="skipped_by_policy",
        risk="high",
        reason="Cloud integration policy could not verify current PR evidence",
    )


def evaluate_pull_request_policy(client: Any, pr_number: int) -> dict[str, Any]:
    """Evaluate one open PR without trusting PR-controlled code or artifacts."""
    if not _positive_int(pr_number):
        raise PolicyPublisherError("pull request number must be a positive integer")
    try:
        pr = client.get_pull_request(pr_number)
    except Exception as exc:
        raise PolicyPublisherError(f"could not read pull request ({type(exc).__name__})") from exc
    if not isinstance(pr, dict) or pr.get("number") != pr_number:
        raise PolicyPublisherError("pull request response does not match the requested number")
    decision = _base_decision(pr_number, pr)
    try:
        head_sha, base_sha, head_repo = _pr_identity(pr)
        if (
            pr.get("state") != "open"
            or (pr.get("base") or {}).get("ref") != "main"
            or head_repo != REPOSITORY
            or not _is_sha(head_sha)
            or not _is_sha(base_sha)
        ):
            return _decision(
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                status="failure",
                policy_state="pull_request_not_eligible",
                cloud_state="skipped_by_policy",
                risk="high",
                reason="only open same-repository PRs targeting main can receive Cloud policy approval",
            )

        main_ref = client.get_ref("heads/main")
        current_main_sha = ((main_ref.get("object") or {}).get("sha") if isinstance(main_ref, dict) else None)
        if not _is_sha(current_main_sha):
            raise GitHubApiError("current main reference is malformed")
        risk_result = classify_pr_risk(client, pr)
        risk = risk_result.get("risk")
        reason = risk_result.get("reason")
        if risk not in {"low", "medium", "high"} or not isinstance(reason, str):
            raise CandidateReceiptError("risk classifier returned an invalid result")
        if base_sha != current_main_sha:
            return _decision(
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                status="pending",
                policy_state="waiting_for_current_base",
                cloud_state="skipped_by_policy",
                risk=risk,
                reason="PR base is not current main; refresh the branch and rerun regular CI",
            )

        ci_run = get_current_ci_run(client, pr)
        checks = current_ci_check_states(client, ci_run)
        initial = evaluate_integration_policy(
            risk=risk,
            checks=checks,
            receipt=None,
        )
        if initial["state"] != "awaiting_trusted_validation":
            return _decision(
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                status=initial["status"],
                policy_state=initial["state"],
                cloud_state=initial["cloud_state"],
                risk=risk,
                reason=(
                    f"all five current-identity CI checks passed; Cloud is not required for this {risk}-risk change"
                    if initial["status"] == "success"
                    else f"regular CI policy: {initial['state']}"
                ),
                target_url=ci_run.get("html_url") if isinstance(ci_run, dict) else None,
            )

        candidate = find_trusted_candidate_validation(
            client,
            pr,
            current_main_sha=current_main_sha,
        )
        if candidate.get("state") != "passed":
            state = candidate.get("state")
            return _decision(
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
                status="failure" if state == "failure" else "pending",
                policy_state=(
                    candidate.get("reason")
                    if candidate.get("reason") in {"invalid_trusted_receipt", "trusted_validation_failed"}
                    else "awaiting_trusted_validation"
                ),
                cloud_state=candidate.get("cloud_state", "skipped_by_policy"),
                risk=risk,
                reason=str(candidate.get("reason", "awaiting trusted Cloud validation")),
                target_url=candidate.get("target_url") or (CANDIDATE_DISPATCH_URL if state == "pending" else None),
            )

        receipt = candidate.get("receipt")
        result = evaluate_integration_policy(
            risk=risk,
            checks=checks,
            receipt=receipt,
            expected_receipt_identity={
                name: receipt.get(name)
                for name in (
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
            }
            if isinstance(receipt, dict)
            else None,
        )
        return _decision(
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            status=result["status"],
            policy_state=result["state"],
            cloud_state=result["cloud_state"],
            risk=risk,
            reason="current regular CI and trusted Cloud suites passed",
            target_url=candidate.get("target_url"),
        )
    except Exception as exc:
        # Keep the known SHA blocked on API/parse failures. Do not let an older
        # success status remain the only result for this evaluation attempt.
        decision.update(
            status="error",
            policy_state="policy_evaluation_error",
            cloud_state="failed" if decision.get("risk") == "high" else "skipped_by_policy",
            risk=decision.get("risk", "high"),
            reason=f"policy evaluation failed ({type(exc).__name__})",
            description=f"policy evaluation failed ({type(exc).__name__})",
        )
        return decision


def _validate_decision(item: Any, *, repository: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise PolicyPublisherError("policy artifact decision must be an object")
    if not _schema_v1(item.get("schema_version")) or item.get("repository") != repository or repository != REPOSITORY:
        raise PolicyPublisherError("policy artifact decision has an unsupported identity")
    if not _positive_int(item.get("pr_number")):
        raise PolicyPublisherError("policy artifact decision has an invalid PR number")
    if not _is_sha(item.get("head_sha")) or not _is_sha(item.get("base_sha")):
        raise PolicyPublisherError("policy artifact decision has an invalid head/base SHA")
    if item.get("status") not in {"success", "pending", "failure", "error"}:
        raise PolicyPublisherError("policy artifact decision has an invalid status")
    if item.get("policy_state") not in POLICY_STATES:
        raise PolicyPublisherError("policy artifact decision has an invalid policy state")
    if item.get("cloud_state") not in {"passed", "skipped_by_policy", "failed"}:
        raise PolicyPublisherError("policy artifact decision has an invalid Cloud coverage state")
    if item.get("risk") not in {"low", "medium", "high"}:
        raise PolicyPublisherError("policy artifact decision has an invalid risk class")
    description = item.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 140 or any(ord(char) < 32 for char in description):
        raise PolicyPublisherError("policy artifact decision has an invalid description")
    if item["risk"] in {"low", "medium"} and item["cloud_state"] != "skipped_by_policy":
        raise PolicyPublisherError("low/medium-risk decisions must keep Cloud skipped_by_policy")
    if item["policy_state"] == "passed" and item["status"] != "success":
        raise PolicyPublisherError("passed policy decisions must have success status")
    if item["status"] == "success" and item["policy_state"] != "passed":
        raise PolicyPublisherError("successful status requires a passed policy decision")
    if item["risk"] == "high" and item["status"] == "success" and item["cloud_state"] != "passed":
        raise PolicyPublisherError("high-risk success requires passed Cloud evidence")
    target_url = item.get("target_url")
    if target_url is not None:
        if not isinstance(target_url, str):
            raise PolicyPublisherError("policy artifact target URL is malformed")
        parsed = urlsplit(target_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or any(ord(char) < 32 for char in target_url):
            raise PolicyPublisherError("policy artifact target URL is unsafe")
    return item


def publish_decisions(
    client: Any,
    decisions_path: str | Path,
    *,
    repository: str,
) -> int:
    """Publish only trusted, schema-checked decisions matching a freshly read PR."""
    path = Path(decisions_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PolicyPublisherError("policy decisions artifact cannot be read") from exc
    if not raw or len(raw) > MAX_DECISION_FILE_BYTES:
        raise PolicyPublisherError("policy decisions artifact is empty or too large")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except PolicyPublisherError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyPublisherError("policy decisions artifact is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or not _schema_v1(payload.get("schema_version"))
        or payload.get("repository") != repository
        or repository != REPOSITORY
    ):
        raise PolicyPublisherError("policy decisions artifact has an unsupported schema or repository")
    entries = payload.get("decisions")
    if not isinstance(entries, list) or len(entries) > MAX_DECISIONS:
        raise PolicyPublisherError("policy decisions artifact has an invalid decision list")

    validated = [_validate_decision(item, repository=repository) for item in entries]
    numbers = [item["pr_number"] for item in validated]
    if len(numbers) != len(set(numbers)):
        raise PolicyPublisherError("policy decisions artifact contains duplicate PR entries")

    published = 0
    for item in validated:
        current = client.get_pull_request(item["pr_number"])
        if not isinstance(current, dict) or current.get("number") != item["pr_number"]:
            raise PolicyPublisherError("current PR identity cannot be revalidated")
        current_head, current_base, current_repo = _pr_identity(current)
        if current.get("state") != "open":
            continue
        if (
            current.get("base", {}).get("ref") != "main"
            or current_repo != repository
            or current_head != item["head_sha"]
            or current_base != item["base_sha"]
        ):
            continue
        main_ref = client.get_ref("heads/main")
        live_main = ((main_ref.get("object") or {}).get("sha") if isinstance(main_ref, dict) else None)
        if not _is_sha(live_main):
            raise PolicyPublisherError("current main reference cannot be revalidated")
        if live_main != item["base_sha"]:
            continue
        client.create_commit_status(
            item["head_sha"],
            state=item["status"],
            context=POLICY_CONTEXT,
            description=item["description"],
            target_url=item.get("target_url"),
        )
        published += 1
    return published


def _client_from_environment() -> GitHubClient:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if repository != REPOSITORY:
        raise PolicyPublisherError("policy publisher is restricted to the configured repository")
    return GitHubClient(
        api_url=os.environ["GITHUB_API_URL"],
        repository=repository,
        token=os.environ["GITHUB_TOKEN"],
    )


def _parse_event(path: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
        if not raw or len(raw) > 1_000_000:
            raise PolicyPublisherError("GitHub event payload is empty or too large")
        event = json.loads(raw, object_pairs_hook=_unique_object)
    except PolicyPublisherError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyPublisherError("GitHub event payload is unavailable or invalid") from exc
    if not isinstance(event, dict):
        raise PolicyPublisherError("GitHub event payload must be an object")
    return event


def evaluate_cli() -> int:
    client = _client_from_environment()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    requested_pr = os.environ.get("POLICY_PR_NUMBER", "").strip()
    if requested_pr:
        if not requested_pr.isdecimal() or not _positive_int(int(requested_pr)):
            raise PolicyPublisherError("POLICY_PR_NUMBER must be a positive integer")
        pr_numbers = [int(requested_pr)]
    elif event_name == "push":
        pr_numbers = [
            pr["number"]
            for pr in client.list_open_pull_requests(base="main")
            if isinstance(pr.get("number"), int) and not isinstance(pr.get("number"), bool)
        ]
    elif event_name == "workflow_run":
        # Re-evaluating every open PR makes event ordering irrelevant and also
        # handles Trusted Cloud Candidate runs, whose workflow_run payload has no PR object.
        if not event_path:
            raise PolicyPublisherError("workflow_run event path is required")
        _parse_event(event_path)
        pr_numbers = [
            pr["number"]
            for pr in client.list_open_pull_requests(base="main")
            if isinstance(pr.get("number"), int) and not isinstance(pr.get("number"), bool)
        ]
    else:
        raise PolicyPublisherError("unsupported policy publisher trigger")
    if len(pr_numbers) > MAX_DECISIONS:
        raise PolicyPublisherError("open PR count exceeds the configured safety limit")

    decisions = [evaluate_pull_request_policy(client, number) for number in sorted(set(pr_numbers))]
    output = Path(os.environ.get("POLICY_DECISIONS_PATH", "artifacts/policy/decisions.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"schema_version": 1, "repository": repository, "decisions": decisions}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["## Cloud Integration Policy", "", "| PR | Risk | Policy | Cloud |", "|---:|---|---|---|"]
        lines.extend(
            f"| #{item['pr_number']} | {item.get('risk', 'unknown')} | {item.get('policy_state', 'error')} | {item.get('cloud_state', 'failed')} |"
            for item in decisions
        )
        Path(summary).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


def publish_cli() -> int:
    client = _client_from_environment()
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    decisions_path = os.environ.get("POLICY_DECISIONS_PATH", "artifacts/policy/decisions.json")
    count = publish_decisions(client, decisions_path, repository=repository)
    print(f"Published {count} current Cloud Integration Policy decision(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("evaluate", "publish"))
    args = parser.parse_args()
    try:
        return evaluate_cli() if args.command == "evaluate" else publish_cli()
    except (PolicyPublisherError, GitHubApiError) as exc:
        print(f"Cloud integration policy failed closed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
