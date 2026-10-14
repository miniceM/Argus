from __future__ import annotations

import json
import zipfile
from io import BytesIO
from typing import Any

import pytest

from scripts.ci.ci_constants import CANDIDATE_ENVIRONMENT, CANDIDATE_WORKFLOW, POLICY_CONTEXT, REQUIRED_CI_CHECKS
from scripts.ci.cloud_e2e_receipt import build_candidate_receipt
from scripts.ci.github_api import GitHubApiError
from scripts.ci.publish_cloud_policy import (
    PolicyPublisherError,
    evaluate_pull_request_policy,
    find_trusted_candidate_validation,
    publish_decisions,
)

REPOSITORY = "miniceM/Argus"
PR_NUMBER = 118
HEAD = "1" * 40
BASE = "2" * 40
MERGE = "3" * 40
CI_RUN_ID = 5001
CANDIDATE_RUN_ID = 6001


def _zip(entries: dict[str, Any]) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, json.dumps(payload))
    return stream.getvalue()


def _evidence() -> bytes:
    def run(passed: int, rate: float) -> dict[str, Any]:
        return {
            "result": {
                "items": [
                    {"scores": {"overall_pass": 1 if index < passed else 0}}
                    for index in range(6)
                ],
                "run_scores": {"overall_pass_rate": rate},
            },
            "dataset_run_url": "https://cloud.langfuse.com/project/p/datasets/d/runs/r",
        }

    link = "https://cloud.langfuse.com/project/p/datasets/d/runs/r"
    return _zip(
        {
            "bootstrap.json": {"items_upserted": 6},
            "v1.json": run(2, 2 / 6),
            "v2.json": run(6, 1.0),
            "async-langfuse-link.json": {
                "api_state": {"status": "COMPLETED", "langfuse_sync_status": "SYNCED", "langfuse_experiment_url": link},
                "api_detail_url": link,
                "remote_identity": {
                    "persisted_run_id": "r",
                    "url_run_id": "r",
                    "url_project_id": "p",
                    "url_dataset_id": "d",
                    "remote_run_lookup": {"status": "CONFIRMED"},
                },
            },
        }
    )


def _pr() -> dict[str, Any]:
    return {
        "number": PR_NUMBER,
        "state": "open",
        "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": REPOSITORY}},
        "base": {"sha": BASE, "ref": "main"},
        "merge_commit_sha": MERGE,
    }


def _ci_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": CI_RUN_ID,
        "run_attempt": 1,
        "event": "pull_request",
        "path": ".github/workflows/ci.yml",
        "pull_requests": [
            {
                "number": PR_NUMBER,
                "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
                "base": {"sha": BASE},
            }
        ],
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-13T00:00:00Z",
        "updated_at": "2026-10-13T00:05:00Z",
        "html_url": "https://github.com/miniceM/Argus/actions/runs/5001",
        **overrides,
    }


def _candidate_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": CANDIDATE_RUN_ID,
        "workflow_id": 20,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": BASE,
        "path": f"{CANDIDATE_WORKFLOW}@refs/heads/main",
        "display_title": f"Trusted Cloud Candidate PR #{PR_NUMBER}",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-13T01:00:00Z",
        "updated_at": "2026-10-13T01:30:00Z",
        "actor": {"login": "maintainer"},
        "triggering_actor": {"login": "maintainer"},
        "html_url": "https://github.com/miniceM/Argus/actions/runs/6001",
        **overrides,
    }


def _manifest(run_id: int = CANDIDATE_RUN_ID, attempt: int = 1) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD,
        "base_sha": BASE,
        "tested_sha": MERGE,
        "risk": "high",
        "risk_reason": "runner source change",
        "workflow_path": CANDIDATE_WORKFLOW,
        "workflow_revision": BASE,
        "workflow_ref": f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@refs/heads/main",
        "run_id": run_id,
        "run_attempt": attempt,
        "requested_by": "maintainer",
        "ci_run_id": CI_RUN_ID,
        "ci_run_attempt": 1,
        "ci_run_url": "https://github.com/miniceM/Argus/actions/runs/5001",
        "created_at": "2026-10-13T00:00:00Z",
    }


def _approval() -> list[dict[str, Any]]:
    return [{
        "state": "approved",
        "user": {"id": 17, "login": "reviewer"},
        "environments": [{"id": 31, "name": CANDIDATE_ENVIRONMENT}],
    }]


def _jobs(*, cloud_conclusion: str = "success") -> list[dict[str, Any]]:
    return [
        {
            "id": 6100,
            "name": "Prepare trusted candidate manifest",
            "status": "completed",
            "conclusion": "success",
            "html_url": "https://github.com/miniceM/Argus/actions/runs/6001/job/6100",
            "steps": [{"name": "Prepare exact high-risk candidate", "status": "completed", "conclusion": "success"}],
        },
        {
            "id": 6101,
            "name": "Trusted candidate Cloud E2E",
            "status": "completed",
            "conclusion": cloud_conclusion,
            "html_url": "https://github.com/miniceM/Argus/actions/runs/6001/job/6101",
            "steps": [
                {"name": "Validate candidate Langfuse Cloud secrets", "status": "completed", "conclusion": "success"},
                {"name": "Run cloud end-to-end tests", "status": "completed", "conclusion": cloud_conclusion},
            ],
        },
    ]


def _receipt(manifest: dict[str, Any] | None = None, *, cloud_conclusion: str = "success") -> dict[str, Any]:
    manifest = manifest or _manifest()
    return build_candidate_receipt(
        manifest,
        workflow_run=_candidate_run(id=manifest["run_id"], run_attempt=manifest["run_attempt"]),
        current_pr=_pr(),
        approvals=_approval(),
        jobs=_jobs(cloud_conclusion=cloud_conclusion),
        cloud_evidence_zip=_evidence(),
    )


class _PolicyClient:
    def __init__(self) -> None:
        self.pr = _pr()
        self.main_sha = BASE
        self.ci_runs = [_ci_run()]
        self.candidate_runs = [_candidate_run()]
        self.jobs_by_run: dict[tuple[int, int], list[dict[str, Any]]] = {
            (CI_RUN_ID, 1): [
                {"name": name, "status": "completed", "conclusion": "success"}
                for name in REQUIRED_CI_CHECKS
            ],
            (CANDIDATE_RUN_ID, 1): _jobs(),
        }
        manifest = _manifest()
        self.manifests = {CANDIDATE_RUN_ID: manifest}
        self.approvals = {CANDIDATE_RUN_ID: _approval()}
        self.evidence = {CANDIDATE_RUN_ID: _evidence()}
        self.receipts = {CANDIDATE_RUN_ID: _receipt(manifest)}
        self.file_changes = [{"filename": "services/eval-runner/app/main.py", "status": "modified"}]
        self.downloads: dict[int, bytes] = {}

    def get_pull_request(self, number: int) -> dict[str, Any]:
        assert number == PR_NUMBER
        return self.pr

    def get_ref(self, ref: str) -> dict[str, Any]:
        assert ref == "heads/main"
        return {"object": {"sha": self.main_sha}}

    def get_workflow_id(self, path: str) -> int:
        return {".github/workflows/ci.yml": 10, CANDIDATE_WORKFLOW: 20}[path]

    def list_workflow_runs(self, workflow_id: int, *, params: dict[str, str]) -> list[dict[str, Any]]:
        if workflow_id == 10:
            assert params == {"event": "pull_request", "branch": "feature"}
            return self.ci_runs
        assert workflow_id == 20
        assert params == {"event": "workflow_dispatch", "branch": "main"}
        return self.candidate_runs

    def list_pull_request_files(self, number: int) -> list[dict[str, Any]]:
        assert number == PR_NUMBER
        return self.file_changes

    def get_tree_modes(self, sha: str) -> tuple[dict[str, str], bool]:
        assert sha in {HEAD, BASE}
        modes = {"services/eval-runner/app/main.py": "100644"}
        if any(item.get("filename") == "docs/guide.md" for item in self.file_changes):
            modes["docs/guide.md"] = "100644"
        return modes, True

    def list_jobs(self, run_id: int, attempt: int) -> list[dict[str, Any]]:
        return self.jobs_by_run[(run_id, attempt)]

    def get_run(self, run_id: int) -> dict[str, Any]:
        return next(run for run in self.candidate_runs if run["id"] == run_id)

    def list_artifacts(self, run_id: int) -> list[dict[str, Any]]:
        manifest = self.manifests.get(run_id)
        if manifest is None:
            return []
        attempt = manifest["run_attempt"]
        files = {
            f"candidate-manifest-{run_id}-{attempt}": _zip({"manifest.json": manifest}),
            f"candidate-cloud-evidence-{run_id}-{attempt}": self.evidence[run_id],
            f"trusted-cloud-receipt-{run_id}-{attempt}": _zip({"receipt.json": self.receipts[run_id]}),
        }
        selected = [
            {"id": index, "name": name, "size_in_bytes": len(data), "expired": False}
            for index, (name, data) in enumerate(files.items(), start=1)
        ]
        self.downloads.update({item["id"]: list(files.values())[item["id"] - 1] for item in selected})
        return selected

    def download_artifact(self, artifact_id: int, *, max_bytes: int = 2_000_000) -> bytes:
        data = self.downloads[artifact_id]
        assert len(data) <= max_bytes
        return data

    def list_run_approvals(self, run_id: int) -> list[dict[str, Any]]:
        return self.approvals[run_id]

    @property
    def repo_path(self) -> str:
        return "repos/miniceM/Argus"

    def request_json(self, path: str) -> dict[str, Any]:
        assert path.endswith(f"/commits/{MERGE}")
        return {"parents": [{"sha": BASE}, {"sha": HEAD}]}

    def create_commit_status(self, sha: str, **kwargs: Any) -> dict[str, Any]:
        self.published = (sha, kwargs)
        return {"state": kwargs["state"], "context": kwargs["context"]}


def test_high_risk_policy_passes_only_with_current_ci_and_recomputed_trusted_receipt() -> None:
    decision = evaluate_pull_request_policy(_PolicyClient(), PR_NUMBER)

    assert decision["status"] == "success"
    assert decision["policy_state"] == "passed"
    assert decision["cloud_state"] == "passed"
    assert decision["head_sha"] == HEAD
    assert decision["base_sha"] == BASE


def test_current_ci_run_accepts_workflow_path_returned_by_actions_api() -> None:
    client = _PolicyClient()
    client.ci_runs[0] = _ci_run(path=".github/workflows/ci.yml")
    client.file_changes = [{"filename": "docs/guide.md", "status": "modified"}]

    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["status"] == "success"
    assert decision["policy_state"] == "passed"


def test_low_risk_policy_passes_without_cloud_and_keeps_cloud_skipped() -> None:
    client = _PolicyClient()
    client.file_changes = [{"filename": "docs/guide.md", "status": "modified"}]

    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["risk"] == "low"
    assert decision["status"] == "success"
    assert decision["cloud_state"] == "skipped_by_policy"


def test_high_risk_policy_waits_without_receipt_and_fails_on_invalid_receipt() -> None:
    client = _PolicyClient()
    client.candidate_runs = []
    decision = evaluate_pull_request_policy(client, PR_NUMBER)
    assert (decision["status"], decision["policy_state"], decision["cloud_state"]) == (
        "pending", "awaiting_trusted_validation", "skipped_by_policy"
    )

    client = _PolicyClient()
    client.receipts[CANDIDATE_RUN_ID]["head_sha"] = "9" * 40
    decision = evaluate_pull_request_policy(client, PR_NUMBER)
    assert (decision["status"], decision["policy_state"], decision["cloud_state"]) == (
        "failure", "invalid_trusted_receipt", "failed"
    )


@pytest.mark.parametrize("conclusion", ["failure", "cancelled"])
def test_latest_matching_failed_or_cancelled_candidate_revokes_older_success(conclusion: str) -> None:
    client = _PolicyClient()
    failed_id = CANDIDATE_RUN_ID + 1
    manifest = _manifest(run_id=failed_id)
    client.candidate_runs.append(_candidate_run(
        id=failed_id,
        run_attempt=1,
        conclusion=conclusion,
        updated_at="2026-10-13T02:00:00Z",
        html_url=f"https://github.com/miniceM/Argus/actions/runs/{failed_id}",
    ))
    client.manifests[failed_id] = manifest
    client.approvals[failed_id] = _approval()
    client.evidence[failed_id] = _evidence()
    client.receipts[failed_id] = _receipt(manifest, cloud_conclusion="failure")
    client.jobs_by_run[(failed_id, 1)] = _jobs(cloud_conclusion="failure")

    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["status"] == "failure"
    assert decision["policy_state"] == "trusted_validation_failed"
    assert decision["cloud_state"] == "failed"


def test_candidate_for_old_head_or_old_base_cannot_authorize_current_pr() -> None:
    client = _PolicyClient()
    client.manifests[CANDIDATE_RUN_ID]["head_sha"] = "9" * 40
    assert find_trusted_candidate_validation(client, client.pr, current_main_sha=BASE)["state"] == "pending"

    client = _PolicyClient()
    client.main_sha = "9" * 40
    client.pr["base"]["sha"] = "9" * 40
    assert find_trusted_candidate_validation(client, client.pr, current_main_sha="9" * 40)["state"] == "pending"


def test_api_failure_produces_error_decision_for_known_head() -> None:
    client = _PolicyClient()

    def fail(_path: str) -> int:
        raise GitHubApiError("private API error")

    client.get_workflow_id = fail  # type: ignore[method-assign]
    decision = evaluate_pull_request_policy(client, PR_NUMBER)
    assert decision["status"] == "error"
    assert decision["policy_state"] == "policy_evaluation_error"
    assert decision["head_sha"] == HEAD


def test_publish_uses_only_policy_context_and_rejects_bad_artifact(tmp_path: Any) -> None:
    client = _PolicyClient()
    decision = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD,
        "base_sha": BASE,
        "status": "pending",
        "policy_state": "awaiting_trusted_validation",
        "cloud_state": "skipped_by_policy",
        "risk": "high",
        "description": "Awaiting trusted Cloud validation",
    }
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps({"schema_version": 1, "repository": REPOSITORY, "decisions": [decision]}))
    assert publish_decisions(client, path, repository=REPOSITORY) == 1
    assert client.published[0] == HEAD
    assert client.published[1]["context"] == POLICY_CONTEXT
    assert client.published[1]["state"] == "pending"

    decision["status"] = "success"
    decision["head_sha"] = "$(touch /tmp/should-not-run)"
    path.write_text(json.dumps({"schema_version": 1, "repository": REPOSITORY, "decisions": [decision]}))
    with pytest.raises(PolicyPublisherError):
        publish_decisions(client, path, repository=REPOSITORY)


def test_latest_failed_current_identity_regular_ci_run_blocks_older_success() -> None:
    client = _PolicyClient()
    client.ci_runs.append(
        _ci_run(
            id=CI_RUN_ID + 1,
            conclusion="failure",
            created_at="2026-10-13T00:10:00Z",
            updated_at="2026-10-13T00:12:00Z",
        )
    )

    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["status"] == "failure"
    assert decision["policy_state"] == "required_ci_failed"


def test_successful_ci_run_with_missing_required_job_does_not_pass() -> None:
    client = _PolicyClient()
    client.jobs_by_run[(CI_RUN_ID, 1)] = client.jobs_by_run[(CI_RUN_ID, 1)][:-1]

    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["status"] == "failure"
    assert decision["policy_state"] == "required_ci_failed"


def test_successful_candidate_without_cloud_evidence_or_receipt_fails_closed() -> None:
    client = _PolicyClient()
    client.evidence[CANDIDATE_RUN_ID] = _zip({"unexpected.json": {"ok": True}})

    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["status"] == "failure"
    assert decision["policy_state"] == "invalid_trusted_receipt"
    assert decision["cloud_state"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    [("path", ".github/workflows/attacker.yml@refs/heads/main"), ("event", "pull_request")],
)
def test_candidate_from_wrong_workflow_or_trigger_cannot_authorize_pr(field: str, value: str) -> None:
    client = _PolicyClient()
    client.candidate_runs[0][field] = value
    decision = evaluate_pull_request_policy(client, PR_NUMBER)

    assert decision["status"] == "pending"
    assert decision["policy_state"] == "awaiting_trusted_validation"


def test_publish_skips_closed_or_stale_pr_decisions(tmp_path: Any) -> None:
    client = _PolicyClient()
    decision = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr_number": PR_NUMBER,
        "head_sha": HEAD,
        "base_sha": BASE,
        "status": "success",
        "policy_state": "passed",
        "cloud_state": "passed",
        "risk": "high",
        "description": "Cloud Integration Policy passed",
    }
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps({"schema_version": 1, "repository": REPOSITORY, "decisions": [decision]}))

    client.pr["state"] = "closed"
    assert publish_decisions(client, path, repository=REPOSITORY) == 0
    assert not hasattr(client, "published")

    client.pr["state"] = "open"
    client.pr["head"]["sha"] = "9" * 40
    assert publish_decisions(client, path, repository=REPOSITORY) == 0
    assert not hasattr(client, "published")

    client.pr["head"]["sha"] = HEAD
    client.main_sha = "8" * 40
    assert publish_decisions(client, path, repository=REPOSITORY) == 0
    assert not hasattr(client, "published")


def test_latest_ci_attempt_uses_rerun_time_not_original_creation_time() -> None:
    from scripts.ci.cloud_e2e_receipt import get_current_ci_run

    client = _PolicyClient()
    rerun = _ci_run(
        run_attempt=2,
        conclusion="failure",
        created_at="2026-10-13T00:00:00Z",
        run_started_at="2026-10-13T00:20:00Z",
        updated_at="2026-10-13T00:25:00Z",
    )
    later_created = _ci_run(
        id=CI_RUN_ID + 1,
        created_at="2026-10-13T00:10:00Z",
        run_started_at="2026-10-13T00:10:00Z",
        updated_at="2026-10-13T00:15:00Z",
    )
    client.ci_runs = [later_created, rerun]

    assert get_current_ci_run(client, client.pr) == rerun
