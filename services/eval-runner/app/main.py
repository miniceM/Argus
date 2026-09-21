from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from langfuse import get_client
from opentelemetry.propagate import inject

from .api_evaluators import router as evaluators_router
from .api_launches import router as launches_router
from .api_registry import router as registry_router
from .api_system import router as system_router
from .config import find_path, settings
from .db import DatabaseManager, MigrationRunner
from .db_models import ExperimentLaunchRecord
from .evaluators import default_evaluator_registry
from .executor import RemoteAgentExecutor
from .limiter import DistributedAgentLimiter, MemoryAgentLimiter, RedisDistributedLimiter
from .manifest import LaunchService, acquire_launch_execution
from .metrics import metrics_router
from .models import BootstrapResult, ExperimentRequest, ExperimentResult
from .orchestrator import LaunchOrchestrator
from .queue import MemoryQueueAdapter, QueueAdapter, RedisStreamQueueAdapter
from .reconciler import ExecutionReconciler
from .registry import AgentRegistry, map_request
from .worker import ExecutionWorker

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

# 3. Initialize Queue & Limiter
if settings.argus_redis_url:
    import redis
    redis_client = redis.Redis.from_url(settings.argus_redis_url)
    queue_adapter: QueueAdapter = RedisStreamQueueAdapter(redis_client)
    limiter: DistributedAgentLimiter = RedisDistributedLimiter(redis_client)
else:
    # Fail-closed in production if no redis url; fallback to Memory only in test mode
    if settings.argus_db_mode == "test":
        queue_adapter = MemoryQueueAdapter()
        limiter = MemoryAgentLimiter()
    else:
        raise RuntimeError("ARGUS_REDIS_URL must be configured in production mode")

# 4. Initialize Orchestrator, Worker, Reconciler
orchestrator = LaunchOrchestrator(db_manager, queue_adapter, limiter)
worker = ExecutionWorker(db_manager, queue_adapter, limiter)
reconciler = ExecutionReconciler(db_manager, queue_adapter, limiter)

# 5. Initialize Launch Service
launch_service = LaunchService(db_manager, registry, runner_version=settings.runner_version)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = None
    reconciler_task = None
    stop_event = asyncio.Event()

    async def _worker_loop():
        while not stop_event.is_set():
            try:
                # Offload blocking queue read off the event loop thread
                msgs = await asyncio.to_thread(worker.poll_queue, count=5, block_ms=1000)
                if not msgs:
                    # Yield event loop when queue is idle
                    await asyncio.sleep(0.05)
                    continue
                for msg_id, item_id, gen in msgs:
                    await worker.execute_item_message(msg_id, item_id, gen)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

    async def _reconciler_loop():
        while not stop_event.is_set():
            try:
                # Offload DB-intensive reconciliation cycle including backlog recovery off the event loop
                await asyncio.to_thread(reconciler.run_reconcile_cycle)
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(1)

    if settings.argus_worker_enabled and settings.argus_db_mode != "test":
        worker_task = asyncio.create_task(_worker_loop())
    if settings.argus_reconciler_enabled and settings.argus_db_mode != "test":
        reconciler_task = asyncio.create_task(_reconciler_loop())

    yield

    stop_event.set()
    if worker_task:
        worker_task.cancel()
    if reconciler_task:
        reconciler_task.cancel()


app = FastAPI(
    title="Enterprise Remote Agent Eval Runner",
    version=settings.runner_version,
    description="Enterprise Agent Evaluation Control Plane with Persistent Registry, Version Snapshots, and Zero-SDK Agents.",
    lifespan=lifespan,
)

# Mount Routers
app.include_router(registry_router)
app.include_router(launches_router)
app.include_router(evaluators_router)
app.include_router(system_router)
app.include_router(metrics_router)


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

        # Legacy experiment evaluator set: 5 item evaluators + 1 run evaluator
        legacy_evaluator_ids = [
            "intent_match",
            "required_tool_match",
            "pii_safe",
            "escalation_match",
            "overall_pass",
            "run_pass_rate",
        ]

        # Persist Launch in Argus DB with deterministic ID binding and dataset client reuse
        persisted = launch_service.create_launch(
            agent_id=request.agent_id,
            agent_version=request.agent_version,
            dataset_name=request.dataset_name,
            dataset_id=str(getattr(dataset, "id", "")) or None,
            name=experiment_name,
            max_concurrency=request.max_concurrency,
            evaluator_ids=legacy_evaluator_ids,
            launch_id=launch_id,
            dataset_client=dataset,
            allow_run_scope=True,
        )
        launch_id = persisted.id
        acquire_launch_execution(db_manager, launch_id)

        # Execution core derives exact item and run evaluators from frozen manifest
        frozen_eval_specs = persisted.manifest.get("evaluators", [])
        frozen_item_evaluators = [
            default_evaluator_registry.get_evaluator_fn(ev["id"], ev.get("version"))
            for ev in frozen_eval_specs
            if ev.get("scope", "item") == "item"
        ]
        frozen_run_evaluators = [
            default_evaluator_registry.get_evaluator_fn(ev["id"], ev.get("version"))
            for ev in frozen_eval_specs
            if ev.get("scope") == "run"
        ]
        effective_max_concurrency = persisted.manifest["execution_policy"]["max_concurrency"]

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
            evaluators=frozen_item_evaluators,
            run_evaluators=frozen_run_evaluators,
            max_concurrency=effective_max_concurrency,
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
                        # Extract threshold from frozen run_pass_rate evaluator
                        run_threshold = 1.0
                        for ev in frozen_eval_specs:
                            if ev.get("id") == "run_pass_rate":
                                run_threshold = float(ev.get("threshold", 1.0))
                                break
                        rec.quality_conclusion = "pass" if float(pass_rate) >= run_threshold else "fail"
                    else:
                        rec.quality_conclusion = "unknown"
                    run_id = (
                        getattr(result, "experiment_id", None)
                        or getattr(result, "dataset_run_id", None)
                        or getattr(result, "id", None)
                        or summary.get("experiment_id")
                        or summary.get("dataset_run_id")
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

