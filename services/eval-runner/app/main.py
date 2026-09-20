from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from langfuse import get_client
from opentelemetry.propagate import inject

from .api_launches import router as launches_router
from .api_registry import router as registry_router
from .config import find_path, settings
from .db import DatabaseManager, MigrationRunner
from .db_models import ExperimentLaunchRecord
from .evaluators import ITEM_EVALUATORS, RUN_EVALUATORS
from .executor import RemoteAgentExecutor
from .manifest import LaunchService, acquire_launch_execution
from .models import BootstrapResult, ExperimentRequest, ExperimentResult
from .registry import AgentRegistry, map_request

app = FastAPI(
    title="Enterprise Remote Agent Eval Runner",
    version=settings.runner_version,
    description="Enterprise Agent Evaluation Control Plane with Persistent Registry, Version Snapshots, and Zero-SDK Agents.",
)

# 1. Initialize Database Manager & Migrations
db_manager = DatabaseManager.from_env()

migrations_dir = find_path(settings.migrations_path, "migrations")
if migrations_dir.exists():
    migration_runner = MigrationRunner(db_manager.engine, migrations_dir)
    migration_runner.apply_all()

# 2. Initialize AgentRegistry & Optional YAML Import
registry = AgentRegistry(db_manager)
if settings.argus_auto_import_yaml:
    yaml_path = find_path(settings.agent_registry_path, "config", "agents.yaml")
    if yaml_path.exists():
        registry.import_yaml(yaml_path)


# 3. Initialize Launch Service
launch_service = LaunchService(db_manager, registry, runner_version=settings.runner_version)

# 4. Mount Enterprise REST Routers (Zero URL Path Variables)
app.include_router(registry_router)
app.include_router(launches_router)


def _client():
    # Current Langfuse Python SDK v4 reads LANGFUSE_PUBLIC_KEY,
    # LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL from the environment.
    return get_client()


def _wait_for_langfuse() -> None:
    deadline = time.monotonic() + settings.ready_timeout_seconds
    ready_url = settings.langfuse_base_url.rstrip("/") + "/api/public/ready"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(ready_url, timeout=3)
            if response.status_code == 200:
                return
        except Exception as exc:  # readiness loop deliberately tolerates startup races
            last_error = exc
        time.sleep(2)
    raise RuntimeError(f"Langfuse not ready at {ready_url}: {last_error}")


def _safe_summary(result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ["run_name", "dataset_run_id", "dataset_run_url", "experiment_id"]:
        value = getattr(result, key, None)
        if value is not None:
            summary[key] = str(value)

    item_rows: list[dict[str, Any]] = []
    for item_result in getattr(result, "item_results", []) or []:
        item = getattr(item_result, "item", None)
        row = {
            "dataset_item_id": str(getattr(item, "id", "")) if item is not None else None,
            "input": getattr(item, "input", None) if item is not None else None,
            "output": getattr(item_result, "output", None),
            "error": str(getattr(item_result, "error", "")) if getattr(item_result, "error", None) else None,
            "scores": {
                str(getattr(e, "name", "score")): getattr(e, "value", None)
                for e in (getattr(item_result, "evaluations", []) or [])
            },
        }
        item_rows.append(row)
    if item_rows:
        summary["items"] = item_rows

    run_scores = {
        str(getattr(e, "name", "score")): getattr(e, "value", None)
        for e in (getattr(result, "run_evaluations", []) or [])
    }
    if run_scores:
        summary["run_scores"] = run_scores

    if not summary:
        summary["repr"] = repr(result)
    return summary


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "runner_version": settings.runner_version}


@app.get("/agents")
def agents() -> dict[str, Any]:
    return registry.list()


@app.post("/admin/bootstrap", response_model=BootstrapResult)
def bootstrap() -> BootstrapResult:
    """Create/upsert the demonstration dataset in Langfuse.

    Deterministic dataset-item UUIDs make the operation idempotent.
    """
    try:
        _wait_for_langfuse()
        lf = _client()
        seed_path = find_path(settings.dataset_seed_path, "data", "dataset.json")
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        dataset_name = seed.get("name") or seed["dataset_name"]

        try:
            dataset = lf.get_dataset(dataset_name)
        except Exception:
            lf.create_dataset(name=dataset_name, description=seed.get("description"))
            dataset = lf.get_dataset(dataset_name)

        for row in seed["items"]:
            lf.create_dataset_item(
                id=row["id"],
                dataset_name=dataset_name,
                input=row["input"],
                expected_output=row.get("expected_output"),
                metadata=row.get("metadata"),
            )
        lf.flush()
        return BootstrapResult(
            dataset_name=dataset_name,
            dataset_id=str(getattr(dataset, "id", "")) or None,
            items_upserted=len(seed["items"]),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"bootstrap failed: {exc}") from exc


@app.post("/experiments/run", response_model=ExperimentResult)
def run_experiment(request: ExperimentRequest) -> ExperimentResult:
    launch_id = str(uuid.uuid4())
    try:
        _wait_for_langfuse()
        lf = _client()
        spec = registry.get(request.agent_id, request.agent_version)
        dataset = lf.get_dataset(request.dataset_name)
        executor = RemoteAgentExecutor(spec)
        experiment_name = request.experiment_name or f"{request.agent_id}-{request.agent_version}"

        # Persist Launch in Argus DB with deterministic ID binding
        try:
            persisted = launch_service.create_launch(
                agent_id=request.agent_id,
                agent_version=request.agent_version,
                dataset_name=request.dataset_name,
                dataset_id=str(getattr(dataset, "id", "")) or None,
                name=experiment_name,
                max_concurrency=request.max_concurrency,
                launch_id=launch_id,
            )
            launch_id = persisted.id
            acquire_launch_execution(db_manager, launch_id)
        except Exception:
            # Fallback tolerating launch creation if already running
            pass

        async def remote_task(*, item: Any, **_: Any) -> dict[str, Any]:
            payload = map_request(item.input, spec.request_mapping)
            headers = {
                "Content-Type": "application/json",
                "X-Eval-Launch-Id": launch_id,
                "X-Eval-Dataset-Item-Id": str(item.id),
                "X-Eval-Agent-Version": spec.version,
            }

            with lf.start_as_current_observation(
                as_type="tool",
                name="remote-agent-http",
                input={"agent": f"{spec.agent_id}:{spec.version}", "request": payload},
                metadata={"endpoint": spec.endpoint, "execution_mode": "SYNC_HTTP"},
            ) as call_observation:
                inject(headers)
                result = await executor.invoke(payload, headers)
                call_observation.update(
                    output=result.body,
                    metadata={
                        "endpoint": spec.endpoint,
                        "http_status": result.status_code,
                        "attempts": result.attempts,
                        "duration_ms": result.duration_ms,
                        "trace_context_received": result.trace_context_received,
                    },
                )
                return result.body

        result = dataset.run_experiment(
            name=experiment_name,
            description=(
                "Remote Agent Evaluation PoC: Langfuse is the system of record; "
                "the runner calls an existing agent endpoint without adding evaluation code to the agent."
            ),
            task=remote_task,
            evaluators=ITEM_EVALUATORS,
            run_evaluators=RUN_EVALUATORS,
            max_concurrency=request.max_concurrency,
            metadata={
                "launch_id": launch_id,
                "agent_id": spec.agent_id,
                "agent_version": spec.version,
                "runner_version": settings.runner_version,
                "execution_mode": "SYNC_HTTP",
            },
        )
        lf.flush()
        summary = _safe_summary(result)

        # Mark launch completed and synced to Langfuse in Argus DB
        try:
            with db_manager.get_session() as session:
                rec = session.get(ExperimentLaunchRecord, launch_id)
                if rec:
                    rec.status = "SUCCEEDED"
                    run_scores = summary.get("run_scores", {})
                    pass_rate = run_scores.get("overall_pass_rate")
                    if pass_rate is not None:
                        rec.quality_conclusion = "pass" if float(pass_rate) == 1.0 else "fail"
                    else:
                        rec.quality_conclusion = "unknown"
                    run_id = (
                        getattr(result, "dataset_run_id", None)
                        or getattr(result, "id", None)
                        or summary.get("dataset_run_id")
                        or summary.get("experiment_id")
                    )
                    rec.langfuse_experiment_id = str(run_id) if run_id else None
                    rec.langfuse_sync_status = "SYNCED"
                    rec.langfuse_sync_error = None
                    rec.completed_at = datetime.utcnow()
                    session.commit()
        except Exception:
            pass

        return ExperimentResult(
            launch_id=launch_id,
            agent_id=spec.agent_id,
            agent_version=spec.version,
            experiment_name=experiment_name,
            dataset_run_url=summary.get("dataset_run_url"),
            result=summary,
        )
    except KeyError as exc:
        try:
            with db_manager.get_session() as session:
                rec = session.get(ExperimentLaunchRecord, launch_id)
                if rec and rec.status in ("PENDING", "RUNNING"):
                    rec.status = "FAILED"
                    rec.quality_conclusion = "fail"
                    rec.langfuse_sync_status = "FAILED"
                    rec.langfuse_sync_error = str(exc)
                    rec.completed_at = datetime.utcnow()
                    session.commit()
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        try:
            with db_manager.get_session() as session:
                rec = session.get(ExperimentLaunchRecord, launch_id)
                if rec and rec.status in ("PENDING", "RUNNING"):
                    rec.status = "FAILED"
                    rec.quality_conclusion = "fail"
                    rec.langfuse_sync_status = "FAILED"
                    rec.langfuse_sync_error = str(exc)
                    rec.completed_at = datetime.utcnow()
                    session.commit()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"experiment failed: {exc}") from exc

