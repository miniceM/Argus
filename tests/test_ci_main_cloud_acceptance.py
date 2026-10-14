from __future__ import annotations

import json
import zipfile
from copy import deepcopy
from io import BytesIO

import pytest

from scripts.ci.cloud_e2e_policy import REQUIRED_CI_CHECKS
from scripts.ci.verify_main_cloud_acceptance import (
    CLOUD_E2E_STEP,
    CLOUD_JOB_NAME,
    CLOUD_SECRET_STEP,
    MAIN_ACCEPTANCE_ARTIFACT_PREFIX,
    MAIN_ACCEPTANCE_JOB_NAME,
    MainCloudAcceptanceError,
    verify_main_cloud_acceptance,
    wait_for_current_main_cloud_acceptance,
)

SHA = "a" * 40


def _job(job_id: int, name: str, *, conclusion: str = "success", steps: list[dict] | None = None) -> dict:
    return {"id": job_id, "name": name, "status": "completed", "conclusion": conclusion, "steps": steps or []}


def _client() -> object:
    class FakeClient:
        def __init__(self) -> None:
            self.run = {
                "path": "/.github/workflows/ci.yml@refs/heads/main",
                "event": "push",
                "head_branch": "main",
                "head_sha": SHA,
                "run_attempt": 2,
                "status": "in_progress",
                "html_url": "https://github.com/miniceM/Argus/actions/runs/77",
            }
            self.jobs = [
                *[_job(index, name) for index, name in enumerate(REQUIRED_CI_CHECKS, 1)],
                _job(
                    10,
                    CLOUD_JOB_NAME,
                    steps=[
                        {"name": CLOUD_SECRET_STEP, "status": "completed", "conclusion": "success"},
                        {"name": CLOUD_E2E_STEP, "status": "completed", "conclusion": "success"},
                    ],
                ),
            ]
            self.ref_sha = SHA
            self.runs = {77: self.run}
            self.workflow_runs: list[dict] = []
            self.artifacts: list[dict] = []
            self.artifact_payloads: dict[int, bytes] = {}

        def get_run(self, run_id: int) -> dict:
            return self.runs[run_id]

        def list_jobs(self, run_id: int, attempt: int) -> list[dict]:
            assert run_id in self.runs
            assert attempt == self.runs[run_id]["run_attempt"]
            return self.jobs

        def get_ref(self, ref: str) -> dict:
            assert ref == "heads/main"
            return {"object": {"sha": self.ref_sha}}

        def get_workflow_id(self, path: str) -> int:
            assert path == ".github/workflows/ci.yml"
            return 5

        def list_workflow_runs(self, workflow_id: int, *, params: dict[str, str]) -> list[dict]:
            assert workflow_id == 5
            return [run for run in self.workflow_runs if run.get("event") == params["event"]]

        def list_artifacts(self, run_id: int) -> list[dict]:
            assert run_id in self.runs
            return self.artifacts

        def download_artifact(self, artifact_id: int, *, max_bytes: int = 2_000_000) -> bytes:
            data = self.artifact_payloads[artifact_id]
            assert len(data) <= max_bytes
            return data

    return FakeClient()


def test_main_acceptance_requires_same_current_main_run_and_actual_cloud_steps() -> None:
    client = _client()
    receipt = verify_main_cloud_acceptance(
        client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
    )
    assert receipt["cloud_state"] == "passed"
    assert receipt["suite_results"] == {"managed-cloud": "passed", "async-link": "passed"}
    assert receipt["baseline"] == {"v1": {"passed": 2, "total": 6}, "v2": {"passed": 6, "total": 6}}
    assert receipt["run_url"].endswith("/77")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("head_sha", "b" * 40),
        ("path", "/.github/workflows/attacker.yml@refs/heads/main"),
        ("run_attempt", 1),
    ],
)
def test_main_acceptance_rejects_wrong_workflow_context(field: str, value: object) -> None:
    client = _client()
    client.run[field] = value
    with pytest.raises(MainCloudAcceptanceError):
        verify_main_cloud_acceptance(
            client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
        )


def test_main_acceptance_rejects_stale_main_failed_check_and_cloud_skip() -> None:
    client = _client()
    client.ref_sha = "b" * 40
    with pytest.raises(MainCloudAcceptanceError, match="main advanced"):
        verify_main_cloud_acceptance(
            client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
        )

    client = _client()
    client.jobs[0]["conclusion"] = "skipped"
    with pytest.raises(MainCloudAcceptanceError):
        verify_main_cloud_acceptance(
            client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
        )

    client = _client()
    cloud = next(job for job in client.jobs if job["name"] == CLOUD_JOB_NAME)
    cloud["steps"][1]["conclusion"] = "skipped"
    with pytest.raises(MainCloudAcceptanceError, match="Cloud step"):
        verify_main_cloud_acceptance(
            client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
        )


def test_main_acceptance_rejects_missing_or_duplicate_job_evidence() -> None:
    client = _client()
    client.jobs = [job for job in client.jobs if job["name"] != "Full Python Tests"]
    with pytest.raises(MainCloudAcceptanceError):
        verify_main_cloud_acceptance(
            client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
        )

    client = _client()
    client.jobs.append(deepcopy(client.jobs[0]))
    with pytest.raises(MainCloudAcceptanceError):
        verify_main_cloud_acceptance(
            client, run_id=77, run_attempt=2, commit_sha=SHA, event_name="push", ref="refs/heads/main"
        )


def _zip_receipt(receipt: dict) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("receipt.json", json.dumps(receipt))
    return stream.getvalue()


def _release_client() -> object:
    client = _client()
    client.run.update(
        {
            "id": 77,
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
            "created_at": "2026-10-13T00:00:00Z",
            "updated_at": "2026-10-13T00:30:00Z",
        }
    )
    client.jobs.append(_job(11, MAIN_ACCEPTANCE_JOB_NAME))
    client.runs = {77: client.run}
    client.workflow_runs = [client.run]
    receipt = verify_main_cloud_acceptance(
        client,
        run_id=77,
        run_attempt=1,
        commit_sha=SHA,
        event_name="push",
        ref="refs/heads/main",
    )
    archive = _zip_receipt(receipt)
    client.artifacts = [
        {
            "id": 91,
            "name": f"{MAIN_ACCEPTANCE_ARTIFACT_PREFIX}-77-1",
            "size_in_bytes": len(archive),
            "expired": False,
        }
    ]
    client.artifact_payloads = {91: archive}
    return client


def test_release_gate_requires_exact_current_completed_run_and_receipt() -> None:
    client = _release_client()

    receipt = wait_for_current_main_cloud_acceptance(
        client,
        commit_sha=SHA,
        timeout_seconds=0,
        poll_seconds=1,
    )

    assert receipt["commit_sha"] == SHA
    assert receipt["run_id"] == 77
    assert receipt["cloud_state"] == "passed"


def test_release_gate_rejects_missing_or_mismatched_receipt_artifact() -> None:
    client = _release_client()
    client.artifacts = []
    with pytest.raises(MainCloudAcceptanceError, match="receipt artifact"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)

    client = _release_client()
    tampered = {**verify_main_cloud_acceptance(
        client,
        run_id=77,
        run_attempt=1,
        commit_sha=SHA,
        event_name="push",
        ref="refs/heads/main",
    ), "commit_sha": "b" * 40}
    archive = _zip_receipt(tampered)
    client.artifacts[0]["size_in_bytes"] = len(archive)
    client.artifact_payloads[91] = archive
    with pytest.raises(MainCloudAcceptanceError, match="does not match live"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)


def test_release_gate_never_reuses_older_success_when_latest_attempt_failed() -> None:
    client = _release_client()
    latest = {
        **client.run,
        "id": 78,
        "run_attempt": 1,
        "conclusion": "failure",
        "created_at": "2026-10-13T00:10:00Z",
        "updated_at": "2026-10-13T00:40:00Z",
        "html_url": "https://github.com/miniceM/Argus/actions/runs/78",
    }
    client.runs[78] = latest
    client.workflow_runs = [client.run, latest]

    with pytest.raises(MainCloudAcceptanceError, match="latest exact-SHA"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)


def test_release_gate_rejects_stale_main_and_wrong_sha() -> None:
    client = _release_client()
    client.ref_sha = "b" * 40
    with pytest.raises(MainCloudAcceptanceError, match="main advanced"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)

    client = _release_client()
    client.workflow_runs[0]["head_sha"] = "b" * 40
    with pytest.raises(MainCloudAcceptanceError, match="timed out"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)


def test_completed_main_run_requires_main_acceptance_job_and_unique_cloud_steps() -> None:
    client = _release_client()
    client.jobs[-1]["conclusion"] = "skipped"
    with pytest.raises(MainCloudAcceptanceError, match="Main Cloud Acceptance"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)

    client = _release_client()
    cloud = next(job for job in client.jobs if job["name"] == CLOUD_JOB_NAME)
    cloud["steps"].append({"name": CLOUD_E2E_STEP, "status": "completed", "conclusion": "success"})
    with pytest.raises(MainCloudAcceptanceError, match="exactly one Cloud step"):
        wait_for_current_main_cloud_acceptance(client, commit_sha=SHA, timeout_seconds=0)
