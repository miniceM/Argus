#!/usr/bin/env python3
"""Real asynchronous Langfuse link verification (Issue #44).

The Cloud Compose profile runs the control plane in ``ARGUS_DB_MODE=test``, where the
background worker, outbox syncer and reconciler loops are intentionally disabled.
Triggering the async API and waiting is therefore not enough: this script drives the
real Worker, Outbox Syncer, Run Score Syncer and link compensator explicitly, against
a real Langfuse project and the real demo agent, and then verifies that the persisted
link describes the actual remote Dataset Run.

Evidence is written to ``$ARTIFACT_DIR`` (default ``artifacts/e2e``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EVAL_RUNNER = ROOT / "services" / "eval-runner"
sys.path.insert(0, str(EVAL_RUNNER))

TIMEOUT_SECONDS = 120.0

# Experiment lookups are scoped by name, so the time window only has to be wide
# enough to cover the run; it is not used to narrow results.
EXPERIMENT_LOOKUP_START = datetime(2020, 1, 1, tzinfo=UTC)


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file without adding a dependency.

    The Cloud Compose profile feeds this same file to the eval-runner container, so it
    is the authoritative description of the configuration under verification.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    # utf-8-sig: a hand-edited env file can start with a BOM, which would otherwise
    # corrupt the first variable *name* instead of its value.
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _require_env(name: str, env_file_values: dict[str, str] | None = None) -> str:
    """Resolve a Langfuse variable, preferring the Compose env file over the shell.

    The verification must exercise the endpoint the container actually used. Shell and
    environment values can differ from the file after normalization (Docker Compose
    strips a UTF-8 BOM from env files), so the file wins when it defines the name.
    """
    if env_file_values and env_file_values.get(name, "").strip():
        return env_file_values[name].strip()
    value = os.getenv(name, "").strip()
    if not value:
        _fail(f"{name} is required for the real async link verification")
    return value


def _describe_invisible_chars(value: str) -> list[str]:
    """Report stripped code points without echoing the (possibly sensitive) value."""
    from app.config import INVISIBLE_CHARS

    return sorted({f"U+{ord(char):04X}" for char in value if char in INVISIBLE_CHARS})


def _apply_langfuse_credentials(env_file_values: dict[str, str] | None = None) -> None:
    """Resolve the API keys and publish the sanitized values to ``os.environ``.

    Two things make this necessary rather than cosmetic:

    * the SDK reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` from the process
      environment and never normalizes them, so a stray BOM or a trailing newline from a
      GitHub Secret becomes part of the base64 ``Authorization`` payload and every call
      fails with 401 "Invalid credentials";
    * validating a value is not the same as using it. The Langfuse client is built from
      ``os.environ``, so a resolved-but-unpublished key would leave the SDK authenticating
      with the raw shell value while this script reported the file value as verified.

    Only the key names and the removed code points are ever printed.
    """
    from app.config import sanitize_langfuse_credential_input

    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        raw = _require_env(name, env_file_values)
        sanitized = sanitize_langfuse_credential_input(raw)
        removed = _describe_invisible_chars(raw)
        if raw != raw.strip():
            removed = removed + ["SURROUNDING_WHITESPACE"]
        if removed:
            print(f"Sanitized {name}: removed {sorted(set(removed))}")
        if not sanitized:
            _fail(f"{name} is empty after removing {sorted(set(removed))}")
        os.environ[name] = sanitized


def _configure_environment(db_path: Path, dashboard_url: str) -> None:
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["ARGUS_DB_MODE"] = "test"
    os.environ["ARGUS_AUTO_IMPORT_YAML"] = "false"
    os.environ["ARGUS_WORKER_ENABLED"] = "false"
    os.environ["ARGUS_RECONCILER_ENABLED"] = "false"
    os.environ["ARGUS_BUILD_ID"] = os.getenv("ARGUS_BUILD_ID", "async-link-verify")
    # The verification must exercise the real Dataset Run path, never the local seed.
    os.environ["ARGUS_DATASET_SOURCE"] = "langfuse"
    os.environ["ARGUS_LANGFUSE_DASHBOARD_URL"] = dashboard_url
    os.environ.setdefault("LANGFUSE_TRACING_ENVIRONMENT", "poc-cloud")


def _normalize_langfuse_endpoint(dashboard_url: str) -> str:
    """Apply the app's own sanitizer and hand the result to the Langfuse SDK.

    The SDK reads ``LANGFUSE_BASE_URL`` straight from the environment and performs no
    normalization, so an invisible leading BOM there makes every API call fail with an
    opaque "missing an 'http://' or 'https://' protocol" error. ``app.config`` already
    strips those characters for its own settings; the sanitized value must be written
    back so the SDK observes it too.
    """
    from app.config import sanitize_langfuse_url_input

    normalized = sanitize_langfuse_url_input(dashboard_url)
    stripped = _describe_invisible_chars(dashboard_url)
    if stripped:
        print(f"Sanitized Langfuse endpoint: removed invisible characters {stripped}")
    if not normalized:
        _fail(f"Langfuse endpoint is empty after removing invisible characters {stripped}")
    parts = urlsplit(normalized)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        # Deliberately does not echo the value: in CI it is a secret.
        _fail(
            "Langfuse endpoint is not an absolute HTTP(S) URL after sanitization "
            f"(removed {stripped}); got scheme={parts.scheme!r} with_host={bool(parts.netloc)}. "
            "Check LANGFUSE_BASE_URL / ARGUS_LANGFUSE_DASHBOARD_URL in .env.cloud for stray "
            "characters such as a UTF-8 BOM."
        )
    os.environ["LANGFUSE_BASE_URL"] = normalized
    os.environ["ARGUS_LANGFUSE_DASHBOARD_URL"] = normalized
    return normalized


def _preflight_langfuse_auth() -> str:
    """Confirm the credentials work before starting the slow verification.

    Without this probe a rejected credential only surfaces minutes later as an opaque
    ``401 Invalid credentials`` in the middle of a launch-creation traceback, which is
    exactly the diagnostic dead end that made the invisible-character regression hard
    to attribute. The Langfuse SDK base64-encodes ``<public>:<secret>`` verbatim, so the
    message points at the realistic causes instead of guessing.
    """
    import re

    from langfuse import get_client

    try:
        projects = get_client().api.projects.get(
            request_options={"timeout_in_seconds": 5, "max_retries": 0}
        ).data
    except Exception as exc:  # noqa: BLE001 - re-reported as verification evidence
        status = re.search(r"status_code[:=]\s*(\d{3})", str(exc))
        suffix = f", HTTP {status.group(1)}" if status else ""
        _fail(
            f"Langfuse rejected the configured credentials{suffix} before the verification "
            "started. The SDK reads LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY verbatim from "
            "the environment: check .env.cloud and the langfuse-e2e environment for stray "
            "invisible characters (U+FEFF BOM, zero-width) or surrounding whitespace, and "
            "confirm the key belongs to the configured host."
        )

    if len(projects) != 1:
        _fail(f"expected exactly one project for the configured key, got {len(projects)}")
    return str(projects[0].id)


def _register_agent_and_version(client: Any, agent_endpoint: str) -> tuple[str, str]:
    agent_id = f"async-link-agent-{uuid.uuid4().hex[:8]}"
    version = "1.0.0"
    created = client.post("/api/v1/agents", json={"id": agent_id, "name": "Async Link Agent"})
    if created.status_code != 201:
        _fail(f"agent registration failed: {created.status_code} {created.text}")
    spec = client.post(
        "/api/v1/agent-versions",
        json={
            "agent_id": agent_id,
            "version": version,
            "endpoint": agent_endpoint,
            "protocol": "HTTP_JSON",
            "method": "POST",
            # 必须与 config/agents.yaml 的 banking-agent 形状一致：
            # map_request 会把 Dataset item 包成 {"input": item_input} 再解析点路径。
            "request_mapping": {
                "messages": "input.messages",
                "customer_id": "input.customer_id",
            },
            "timeout_seconds": 30,
            "max_retries": 1,
            "max_concurrency": 4,
            "rate_limit_per_minute": 600,
            "is_idempotent": False,
        },
    )
    if spec.status_code != 201:
        _fail(f"agent version registration failed: {spec.status_code} {spec.text}")
    return agent_id, version


def _drive_until_link(
    app_modules: dict[str, Any],
    launch_id: str,
    deadline: float,
) -> dict[str, Any]:
    """Explicitly drives worker, outbox syncer, run score syncer and compensation."""
    worker = app_modules["worker"]
    outbox_syncer = app_modules["outbox_syncer"]
    run_score_syncer = app_modules["run_score_syncer"]
    reconciler = app_modules["reconciler"]
    link_service = app_modules["launch_link_service"]
    db_manager = app_modules["db_manager"]

    last_state: dict[str, Any] = {}
    last_reported_reason: str | None = None
    last_link_status: str | None = None
    last_link_reason: str | None = None
    while time.monotonic() < deadline:
        messages = worker.poll_queue(count=10, block_ms=200)
        for message_id, item_id, generation in messages:
            asyncio.run(worker.execute_item_message(message_id, item_id, generation))
        if messages:
            outbox_syncer.process_batch(batch_size=10)
            run_score_syncer.process_batch(batch_size=10)
        reconciler.run_reconcile_cycle()
        # Explicit compensation pass: a SYNCED launch must end up with a valid link.
        reconciler.reconcile_langfuse_links(link_service)

        # Drive the link service directly as well. The reconciler only sees candidates
        # that pass its own query and backs off between retries, so a permanent reason
        # (unconfigured dashboard, conflicting dataset identity, ...) would otherwise
        # stay invisible and surface only as this loop's timeout.
        try:
            link_result = link_service.ensure_launch_link(launch_id)
            last_link_status, last_link_reason = link_result.status, link_result.reason
        except Exception as exc:  # noqa: BLE001 - reported as verification evidence
            last_link_status, last_link_reason = "EXCEPTION", type(exc).__name__
        if last_link_reason and last_link_reason != last_reported_reason:
            last_reported_reason = last_link_reason
            print(f"link backfill: status={last_link_status} reason={last_link_reason}")

        with db_manager.get_session() as session:
            from app.db_models import ExperimentLaunchRecord

            launch = session.get(ExperimentLaunchRecord, launch_id)
            if launch is None:
                _fail(f"launch {launch_id} disappeared during execution")
            last_state = {
                "status": launch.status,
                "langfuse_sync_status": launch.langfuse_sync_status,
                "langfuse_experiment_id": launch.langfuse_experiment_id,
                "langfuse_experiment_url": launch.langfuse_experiment_url,
                "langfuse_sync_error": launch.langfuse_sync_error,
                "link_backfill_status": last_link_status,
                "link_backfill_reason": last_link_reason,
            }
        if (
            last_state["status"] in ("COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED")
            and last_state["langfuse_sync_status"] == "SYNCED"
            and last_state["langfuse_experiment_id"]
            and last_state["langfuse_experiment_url"]
        ):
            return last_state
        time.sleep(0.5)

    _fail(f"launch {launch_id} did not reach SYNCED with a link in time: {last_state}")
    return last_state  # pragma: no cover - _fail raises


def _verify_remote_identity(
    db_manager: Any,
    launch_id: str,
    persisted_run_id: str,
    dashboard_url: str,
) -> dict[str, Any]:
    from app.db_models import ExperimentLaunchRecord, LangfuseSyncTaskRecord
    from langfuse import get_client

    with db_manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        dataset_name = launch.dataset_name
        run_names = [
            row.dataset_run_name
            for row in session.query(LangfuseSyncTaskRecord)
            .filter(LangfuseSyncTaskRecord.launch_id == launch_id)
            .all()
            if row.dataset_run_name
        ]
        url = launch.langfuse_experiment_url

    client = get_client()
    projects = client.api.projects.get(
        request_options={"timeout_in_seconds": 5, "max_retries": 1}
    ).data
    if len(projects) != 1:
        _fail(f"expected exactly one project for the configured key, got {len(projects)}")
    project_id = str(projects[0].id)

    # Langfuse Cloud withdrew the v3 dataset-run read path for organizations created
    # after 2026-09-16: GET /api/public/datasets/{name}/runs/{run} now answers
    # 410 LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION. A Dataset Run is an Experiment
    # in Langfuse v4, so identity is verified through /api/public/experiments.
    remote_run = None
    for run_name in dict.fromkeys(run_names):
        try:
            try:
                response = client.api.experiments.list(
                    name=run_name,
                    from_start_time=EXPERIMENT_LOOKUP_START,
                    limit=100,
                    request_options={"timeout_in_seconds": 5, "max_retries": 1},
                )
            except TypeError:
                # Guard against SDK signature drift: fall back to an unfiltered page
                # and match the run name locally.
                response = client.api.experiments.list(
                    from_start_time=EXPERIMENT_LOOKUP_START,
                    limit=100,
                    request_options={"timeout_in_seconds": 5, "max_retries": 1},
                )
        except Exception as exc:  # noqa: BLE001 - reported as verification evidence
            print(f"  experiment lookup failed for {run_name}: {exc}", file=sys.stderr)
            continue
        for candidate in getattr(response, "data", None) or []:
            if str(getattr(candidate, "id", "")) == persisted_run_id:
                remote_run = candidate
                break
        if remote_run is not None:
            break
    if remote_run is None:
        _fail(
            "no remote Experiment matched the persisted run id "
            f"{persisted_run_id} (dataset {dataset_name}, run names "
            f"{list(dict.fromkeys(run_names))})"
        )

    remote_dataset_id = str(getattr(remote_run, "dataset_id", ""))
    parts = urlsplit(url)
    path_parts = [segment for segment in parts.path.split("/") if segment]
    # {prefix}/project/{project_id}/datasets/{dataset_id}/runs/{run_id}
    try:
        datasets_index = path_parts.index("datasets")
        runs_index = path_parts.index("runs")
        url_project_id = path_parts[datasets_index - 1]
        url_dataset_id = path_parts[datasets_index + 1]
        url_run_id = path_parts[runs_index + 1]
    except (ValueError, IndexError):
        _fail(f"persisted link does not look like a Dataset Run page URL: {url}")
        raise

    if url_run_id != persisted_run_id:
        _fail(f"link run id {url_run_id} != persisted run id {persisted_run_id}")
    if url_run_id != str(remote_run.id):
        _fail(f"link run id {url_run_id} != remote run id {remote_run.id}")
    if url_dataset_id != remote_dataset_id:
        _fail(f"link dataset id {url_dataset_id} != remote dataset id {remote_dataset_id}")
    if url_project_id != project_id:
        _fail(f"link project id {url_project_id} != key project id {project_id}")
    if not parts.scheme.startswith("http"):
        _fail(f"link is not an absolute http(s) URL: {url}")
    if dashboard_url and not url.startswith(dashboard_url.rstrip("/")):
        _fail(f"link {url} does not use the configured dashboard base {dashboard_url}")

    return {
        "persisted_run_id": persisted_run_id,
        "remote_run_id": str(remote_run.id),
        "remote_dataset_id": remote_dataset_id,
        "key_project_id": project_id,
        "url": url,
        "url_project_id": url_project_id,
        "url_dataset_id": url_dataset_id,
        "url_run_id": url_run_id,
        "dataset_run_names": list(dict.fromkeys(run_names)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-endpoint", default=os.getenv("ARGUS_VERIFY_AGENT_ENDPOINT", "http://127.0.0.1:18081/invoke"))
    parser.add_argument("--dataset-name", default=os.getenv("ARGUS_VERIFY_DATASET", "banking-agent-regression"))
    parser.add_argument("--artifact-dir", default=os.getenv("ARTIFACT_DIR", str(ROOT / "artifacts" / "e2e")))
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    args = parser.parse_args()

    env_file_values = _read_env_file(Path(os.getenv("ARGUS_CLOUD_ENV_FILE", ".env.cloud")))
    # NOTE: nothing below may import ``app.config`` before ``_configure_environment``
    # has run. ``app/config.py`` builds ``settings = Settings()`` at import time and
    # ``main.py`` binds that object, so a premature import permanently freezes
    # ARGUS_LANGFUSE_DASHBOARD_URL as None and every link backfill degrades to
    # DASHBOARD_UNCONFIGURED.
    base_url_raw = _require_env("LANGFUSE_BASE_URL", env_file_values)
    # Cloud E2E connects directly to the managed API host, which is also the UI origin.
    dashboard_url = (
        env_file_values.get("ARGUS_LANGFUSE_DASHBOARD_URL", "").strip()
        or os.getenv("ARGUS_LANGFUSE_DASHBOARD_URL", "").strip()
        or base_url_raw
    )

    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="argus-async-link-") as tmpdir:
        db_path = Path(tmpdir) / "async_link_verify.db"
        _configure_environment(db_path, dashboard_url)

        # Environment first, app.config import second (see the note in main()).
        _apply_langfuse_credentials(env_file_values)
        # Importing app.config sanitizes the endpoint for its own settings; mirror the
        # result into the environment the Langfuse SDK reads.
        _normalize_langfuse_endpoint(dashboard_url)
        _preflight_langfuse_auth()

        import app.main as main_module
        from app.db import MigrationRunner
        from fastapi.testclient import TestClient

        MigrationRunner(main_module.db_manager.engine, ROOT / "migrations").apply_all()

        components = {
            "db_manager": main_module.db_manager,
            "worker": main_module.worker,
            "outbox_syncer": main_module.outbox_syncer,
            "run_score_syncer": main_module.run_score_syncer,
            "reconciler": main_module.reconciler,
            "launch_link_service": main_module.launch_link_service,
        }

        with TestClient(main_module.app) as client:
            agent_id, version = _register_agent_and_version(client, args.agent_endpoint)
            try:
                created = client.post(
                    "/api/v1/experiment-launches",
                    json={
                        "agent_id": agent_id,
                        "agent_version": version,
                        "dataset_name": args.dataset_name,
                        "name": f"async-link-verify-{uuid.uuid4().hex[:8]}",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - reported as verification evidence
                _fail(f"launch creation raised (Langfuse identity/SDK problem?): {exc}")
                raise
            if created.status_code != 201:
                _fail(f"launch creation failed: {created.status_code} {created.text}")
            launch_id = created.json()["id"]

            accepted = client.post(f"/api/v1/experiment-launches/{launch_id}/run")
            if accepted.status_code != 202:
                _fail(f"async run must return 202, got {accepted.status_code} {accepted.text}")

            state = _drive_until_link(components, launch_id, time.monotonic() + args.timeout)

            detail = client.get(f"/api/v1/experiment-launches/{launch_id}").json()
            if detail["langfuse_experiment_url"] != detail["links"]["langfuse_experiment"]:
                _fail("API top-level URL and links.langfuse_experiment disagree")

        evidence = _verify_remote_identity(
            components["db_manager"],
            launch_id,
            str(state["langfuse_experiment_id"]),
            dashboard_url,
        )

    report = {
        "launch_id": launch_id,
        "agent_id": agent_id,
        "dashboard_url": dashboard_url,
        "api_state": state,
        "api_detail_url": detail["langfuse_experiment_url"],
        "remote_identity": evidence,
    }
    evidence_path = artifact_dir / "async-langfuse-link.json"
    evidence_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print("Async Langfuse link verification: PASS")
    print(f"launch: {launch_id}")
    print(f"run id: {evidence['remote_run_id']}")
    print(f"dataset id: {evidence['remote_dataset_id']}")
    print(f"project id: {evidence['key_project_id']}")
    print(f"link: {evidence['url']}")
    print(f"evidence: {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
