from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.ci.cloud_e2e_policy import REQUIRED_CI_CHECKS
from scripts.ci.verify_main_cloud_acceptance import (
    CLOUD_E2E_STEP,
    CLOUD_JOB_NAME,
    CLOUD_SECRET_STEP,
    MainCloudAcceptanceError,
    verify_main_cloud_acceptance,
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

        def get_run(self, run_id: int) -> dict:
            assert run_id == 77
            return self.run

        def list_jobs(self, run_id: int, attempt: int) -> list[dict]:
            assert run_id == 77 and attempt == 2
            return self.jobs

        def get_ref(self, ref: str) -> dict:
            assert ref == "heads/main"
            return {"object": {"sha": self.ref_sha}}

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
