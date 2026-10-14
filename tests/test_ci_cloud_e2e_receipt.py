from __future__ import annotations

import json
import stat
import sys
import zipfile
from io import BytesIO
from typing import Any

import pytest

from scripts.ci import cloud_e2e_receipt as receipt_cli
from scripts.ci.ci_constants import (
    CANDIDATE_ENVIRONMENT,
    CANDIDATE_WORKFLOW,
    REQUIRED_CI_CHECKS,
    REQUIRED_SECRETS,
)
from scripts.ci.cloud_e2e_policy import validate_trusted_receipt
from scripts.ci.cloud_e2e_receipt import (
    CandidateReceiptError,
    build_candidate_receipt,
    prepare_candidate_manifest,
    read_json_archive,
    verify_candidate_unchanged,
)

REPOSITORY = "miniceM/Argus"
HEAD = "1" * 40
BASE = "2" * 40
MERGE = "3" * 40
WORKFLOW = "4" * 40
RUN_ID = 9001
ATTEMPT = 2


def _zip_json(entries: dict[str, Any]) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, json.dumps(payload))
    return stream.getvalue()


def _cloud_evidence() -> bytes:
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

    return _zip_json(
        {
            "bootstrap.json": {"items_upserted": 6},
            "v1.json": run(2, 2 / 6),
            "v2.json": run(6, 1.0),
            "async-langfuse-link.json": {
                "api_state": {
                    "status": "COMPLETED",
                    "langfuse_sync_status": "SYNCED",
                    "langfuse_experiment_url": "https://cloud.langfuse.com/project/p/datasets/d/runs/r",
                },
                "api_detail_url": "https://cloud.langfuse.com/project/p/datasets/d/runs/r",
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


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "pr_number": 118,
        "head_sha": HEAD,
        "base_sha": BASE,
        "tested_sha": MERGE,
        "risk": "high",
        "risk_reason": "runner change",
        "workflow_path": CANDIDATE_WORKFLOW,
        "workflow_revision": WORKFLOW,
        "workflow_ref": f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@refs/heads/main",
        "run_id": RUN_ID,
        "run_attempt": ATTEMPT,
        "requested_by": "maintainer",
        "ci_run_id": 8001,
        "ci_run_attempt": 1,
        "ci_run_url": "https://github.com/miniceM/Argus/actions/runs/8001",
        "created_at": "2026-10-13T00:00:00Z",
    }


def _workflow_run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": RUN_ID,
        "run_attempt": ATTEMPT,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": WORKFLOW,
        "path": f"{CANDIDATE_WORKFLOW}@refs/heads/main",
        "status": "in_progress",
        "actor": {"login": "maintainer"},
        "triggering_actor": {"login": "maintainer"},
        "html_url": "https://github.com/miniceM/Argus/actions/runs/9001",
        **overrides,
    }


def _pr(**overrides: Any) -> dict[str, Any]:
    return {
        "number": 118,
        "state": "open",
        "head": {"sha": HEAD, "ref": "feature", "repo": {"full_name": REPOSITORY}},
        "base": {"sha": BASE, "ref": "main"},
        "merge_commit_sha": MERGE,
        **overrides,
    }


def _approvals() -> list[dict[str, Any]]:
    return [
        {
            "state": "approved",
            "user": {"id": 17, "login": "reviewer"},
            "environments": [{"id": 31, "name": CANDIDATE_ENVIRONMENT}],
        }
    ]


def _jobs(**overrides: Any) -> list[dict[str, Any]]:
    prepare = {
        "id": 7000,
        "name": "Prepare trusted candidate manifest",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/miniceM/Argus/actions/runs/9001/job/7000",
        "steps": [
            {"name": "Prepare exact high-risk candidate", "status": "completed", "conclusion": "success"},
        ],
    }
    candidate = {
        "id": 7001,
        "name": "Trusted candidate Cloud E2E",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/miniceM/Argus/actions/runs/9001/job/7001",
        "steps": [
            {"name": "Validate candidate Langfuse Cloud secrets", "status": "completed", "conclusion": "success"},
            {"name": "Run cloud end-to-end tests", "status": "completed", "conclusion": "success"},
        ],
    }
    candidate.update(overrides)
    return [prepare, candidate]


def _expected_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: manifest[key] for key in (
        "repository", "pr_number", "head_sha", "base_sha", "tested_sha",
        "workflow_revision", "run_id", "run_attempt", "ci_run_id", "ci_run_attempt",
    )}


def _build(**overrides: Any) -> dict[str, Any]:
    args = {
        "manifest": _manifest(),
        "workflow_run": _workflow_run(),
        "current_pr": _pr(),
        "approvals": _approvals(),
        "jobs": _jobs(),
        "cloud_evidence_zip": _cloud_evidence(),
    }
    args.update(overrides)
    return build_candidate_receipt(**args)


def test_approved_exact_candidate_with_real_cloud_evidence_builds_valid_receipt() -> None:
    manifest = _manifest()
    receipt = _build(manifest=manifest)

    assert receipt["cloud_state"] == "passed"
    assert receipt["approval"]["login"] == "reviewer"
    assert receipt["prepare_job_id"] == 7000
    assert receipt["baseline"] == {
        "v1": {"passed": 2, "total": 6},
        "v2": {"passed": 6, "total": 6},
    }
    assert validate_trusted_receipt(receipt, _expected_identity(manifest))


@pytest.mark.parametrize(
    ("overrides", "evidence"),
    [
        ({"approvals": []}, _cloud_evidence()),
        ({"current_pr": _pr(head={"sha": "5" * 40, "repo": {"full_name": REPOSITORY}})}, _cloud_evidence()),
        ({"current_pr": _pr(base={"sha": "6" * 40, "ref": "main"})}, _cloud_evidence()),
        ({"current_pr": _pr(head={"sha": HEAD, "repo": {"full_name": "external/fork"}})}, _cloud_evidence()),
        ({"workflow_run": _workflow_run(path=".github/workflows/fake.yml@refs/heads/main")}, _cloud_evidence()),
        ({"workflow_run": _workflow_run(head_sha=HEAD)}, _cloud_evidence()),
        ({"workflow_run": _workflow_run(run_attempt=ATTEMPT + 1)}, _cloud_evidence()),
        ({"workflow_run": _workflow_run(actor={"login": "someone-else"})}, _cloud_evidence()),
        ({"workflow_run": _workflow_run(status="completed", conclusion="failure")}, _cloud_evidence()),
        ({"workflow_run": _workflow_run(status="completed", conclusion="cancelled")}, _cloud_evidence()),
        ({"jobs": _jobs(conclusion="failure")}, _cloud_evidence()),
        ({"jobs": _jobs(conclusion="cancelled")}, _cloud_evidence()),
        ({"jobs": [_jobs()[1]]}, _cloud_evidence()),
        ({"jobs": [_jobs()[0] | {"conclusion": "failure"}, _jobs()[1]]}, _cloud_evidence()),
        ({"jobs": _jobs(steps=[{"name": "Run cloud end-to-end tests", "status": "completed", "conclusion": "skipped"}])}, _cloud_evidence()),
        ({}, None),
        ({}, _zip_json({"bootstrap.json": {"items_upserted": 999}})),
        ({}, _zip_json({
            "bootstrap.json": {"items_upserted": 6},
            "v1.json": {"result": {"items": [], "run_scores": {"overall_pass_rate": 0}}, "dataset_run_url": "forged"},
            "v2.json": {"result": {"items": [], "run_scores": {"overall_pass_rate": 0}}, "dataset_run_url": "forged"},
            "async-langfuse-link.json": {"api_state": {"status": "COMPLETED", "langfuse_sync_status": "SYNCED", "langfuse_experiment_url": "forged"}, "api_detail_url": "forged", "remote_identity": {"persisted_run_id": "x", "url_run_id": "x", "url_project_id": "p", "url_dataset_id": "d", "remote_run_lookup": {"status": "CONFIRMED"}}},
        })),
    ],
    ids=["no approval", "stale head", "stale base", "fork", "wrong workflow", "wrong workflow sha", "wrong run attempt", "wrong actor", "failed workflow", "cancelled workflow", "failed job", "cancelled job", "missing prepare job", "failed prepare job", "skipped E2E step", "missing evidence", "wrong bootstrap", "forged baseline"],
)
def test_missing_or_untrusted_candidate_conditions_never_create_passed_receipt(
    overrides: dict[str, Any], evidence: bytes | None
) -> None:
    try:
        receipt = _build(cloud_evidence_zip=evidence, **overrides)
    except CandidateReceiptError:
        return
    assert receipt["cloud_state"] != "passed"
    assert not validate_trusted_receipt(receipt, _expected_identity(_manifest()))


class _PreparationClient:
    def __init__(
        self,
        *,
        pr: dict[str, Any] | None = None,
        runs: list[dict[str, Any]] | None = None,
        main_sha: str = BASE,
    ) -> None:
        self.pr = pr or _pr()
        self.main_sha = main_sha
        self.runs = runs or [{
            "id": 8001,
            "run_attempt": 1,
            "event": "pull_request",
            "path": ".github/workflows/ci.yml",
            "pull_requests": [{
                "number": 118,
                "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
                "base": {"sha": BASE},
            }],
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-13T00:00:00Z",
            "html_url": "https://github.com/miniceM/Argus/actions/runs/8001",
        }]

    @property
    def repo_path(self) -> str:
        return "repos/miniceM/Argus"

    def get_pull_request(self, number: int) -> dict[str, Any]:
        assert number == 118
        return self.pr

    def get_ref(self, ref: str) -> dict[str, Any]:
        assert ref == "heads/main"
        return {"object": {"sha": self.main_sha}}

    def get_workflow_id(self, path: str) -> int:
        assert path == ".github/workflows/ci.yml"
        return 99

    def request_json(self, path: str) -> dict[str, Any]:
        assert path.endswith(f"/commits/{MERGE}")
        return {"parents": [{"sha": BASE}, {"sha": HEAD}]}

    def list_pull_request_files(self, number: int) -> list[dict[str, Any]]:
        assert number == 118
        return [{"filename": "services/eval-runner/app/main.py", "status": "modified"}]

    def get_tree_modes(self, sha: str) -> tuple[dict[str, str], bool]:
        assert sha in {BASE, HEAD}
        return {"services/eval-runner/app/main.py": "100644"}, True

    def list_workflow_runs(self, workflow_path: str | int, *, params: dict[str, str]) -> list[dict[str, Any]]:
        assert workflow_path == 99
        assert params == {"event": "pull_request", "branch": "feature"}
        return self.runs

    def list_jobs(self, run_id: int, attempt: int) -> list[dict[str, Any]]:
        assert run_id in {8001, 8002} and attempt == 1
        return [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in REQUIRED_CI_CHECKS
        ]


def test_manifest_binds_current_same_repo_high_risk_pr_and_successful_current_ci() -> None:
    manifest = prepare_candidate_manifest(
        _PreparationClient(),
        repository=REPOSITORY,
        pr_number=118,
        expected_head_sha=HEAD,
        expected_base_sha=BASE,
        workflow_ref=f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@refs/heads/main",
        workflow_revision=WORKFLOW,
        run_id=RUN_ID,
        run_attempt=ATTEMPT,
        requested_by="maintainer",
    )

    assert manifest["tested_sha"] == MERGE
    assert manifest["risk"] == "high"
    assert (manifest["ci_run_id"], manifest["ci_run_attempt"]) == (8001, 1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repository": "external/Argus"}, "restricted"),
        ({"workflow_ref": f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@refs/heads/feature"}, "trusted main"),
        ({"expected_head_sha": "bad"}, "full 40-character"),
        ({"client": _PreparationClient(pr=_pr(head={"sha": "5" * 40, "repo": {"full_name": REPOSITORY}}))}, "changed"),
        ({"client": _PreparationClient(pr=_pr(head={"sha": HEAD, "repo": {"full_name": "external/fork"}}))}, "fork"),
    ],
)
def test_manifest_preparation_rejects_untrusted_identity(kwargs: dict[str, Any], message: str) -> None:
    args: dict[str, Any] = {
        "client": _PreparationClient(),
        "repository": REPOSITORY,
        "pr_number": 118,
        "expected_head_sha": HEAD,
        "expected_base_sha": BASE,
        "workflow_ref": f"{REPOSITORY}/{CANDIDATE_WORKFLOW}@refs/heads/main",
        "workflow_revision": WORKFLOW,
        "run_id": RUN_ID,
        "run_attempt": ATTEMPT,
        "requested_by": "maintainer",
    }
    args.update(kwargs)
    with pytest.raises(CandidateReceiptError, match=message):
        prepare_candidate_manifest(**args)


def test_archive_rejects_path_traversal_symlinks_duplicates_and_unexpected_members() -> None:
    safe = {"manifest.json": {"schema_version": 1}}
    with pytest.raises(CandidateReceiptError, match="traversal"):
        read_json_archive(_zip_json({"../manifest.json": safe["manifest.json"]}), ("manifest.json",))
    with pytest.raises(CandidateReceiptError, match="unexpected"):
        read_json_archive(_zip_json({"manifest.json": safe["manifest.json"], "extra.txt": {"x": 1}}), ("manifest.json",))

    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps(safe["manifest.json"]))
        archive.writestr("nested/manifest.json", json.dumps(safe["manifest.json"]))
    with pytest.raises(CandidateReceiptError, match="directory entry|duplicate evidence"):
        read_json_archive(stream.getvalue(), ("manifest.json",))

    symlink = BytesIO()
    with zipfile.ZipFile(symlink, "w") as archive:
        link = zipfile.ZipInfo("manifest.json")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "outside.json")
    with pytest.raises(CandidateReceiptError, match="symbolic link"):
        read_json_archive(symlink.getvalue(), ("manifest.json",))

    duplicate_json = BytesIO()
    with zipfile.ZipFile(duplicate_json, "w") as archive:
        archive.writestr("manifest.json", '{"schema_version": 1, "schema_version": 2}')
    with pytest.raises(CandidateReceiptError, match="duplicate object keys"):
        read_json_archive(duplicate_json.getvalue(), ("manifest.json",))


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (_PreparationClient(pr=_pr(head={"sha": "5" * 40, "ref": "feature", "repo": {"full_name": REPOSITORY}})), "changed"),
        (_PreparationClient(main_sha="6" * 40), "current main"),
        (_PreparationClient(runs=[{
            "id": 8002,
            "run_attempt": 1,
            "event": "pull_request",
            "path": ".github/workflows/ci.yml",
            "pull_requests": [{"number": 118, "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}}, "base": {"sha": BASE}}],
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-10-13T01:00:00Z",
        }]), "regular CI"),
    ],
    ids=["updated PR", "advanced main", "newer CI identity"],
)
def test_candidate_is_revalidated_after_environment_approval(client: _PreparationClient, message: str) -> None:
    with pytest.raises(CandidateReceiptError, match=message):
        verify_candidate_unchanged(client, _manifest())


def test_candidate_revalidation_accepts_only_the_frozen_manifest_identity() -> None:
    verify_candidate_unchanged(_PreparationClient(), _manifest())


def test_verify_cli_exports_validated_cloud_secrets_for_following_steps(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    candidate = workspace / "candidate"
    workspace.mkdir()
    candidate.mkdir()
    github_env = tmp_path / "github_env"
    github_env.write_text("", encoding="utf-8")
    values = {
        "LANGFUSE_BASE_URL": "https://langfuse.example",
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
    }

    monkeypatch.setattr(receipt_cli, "_client_from_environment", lambda: object())
    monkeypatch.setattr(
        receipt_cli,
        "_artifact_bytes",
        lambda client, run_id, name: _zip_json({"manifest.json": _manifest()}),
    )
    monkeypatch.setattr(receipt_cli, "verify_candidate_unchanged", lambda client, manifest: None)
    monkeypatch.setenv("GITHUB_RUN_ID", str(RUN_ID))
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", str(ATTEMPT))
    monkeypatch.setenv("PR_NUMBER", "118")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv("CANDIDATE_DIR", str(candidate))
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    assert receipt_cli._verify_cli() == 0

    assert github_env.read_text(encoding="utf-8").splitlines() == [
        f"{name}={values[name]}" for name in REQUIRED_SECRETS
    ]
    assert (candidate / ".env.cloud").read_text(encoding="utf-8").splitlines() == [
        *(f"{name}={values[name]}" for name in REQUIRED_SECRETS),
        f"ARGUS_LANGFUSE_DASHBOARD_URL={values['LANGFUSE_BASE_URL']}",
    ]
    captured = capsys.readouterr()
    assert all(value not in captured.out + captured.err for value in values.values())


@pytest.mark.parametrize(
    ("command", "handler_name"),
    [
        ("prepare", "_prepare_cli"),
        ("verify", "_verify_cli"),
        ("record", "_record_cli"),
    ],
)
def test_cli_dispatches_supported_commands(
    command: str, handler_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["cloud_e2e_receipt", command])
    monkeypatch.setattr(receipt_cli, handler_name, lambda: 0)

    assert receipt_cli.main() == 0
