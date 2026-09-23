from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from langfuse import get_client

from .api_evaluators import router as evaluators_router
from .api_launches import router as launches_router
from .api_registry import router as registry_router
from .api_system import router as system_router
from .config import find_path, settings
from .db import DatabaseManager, MigrationRunner
from .execution import LaunchExecutionService
from .langfuse_sync import LangfuseOutboxSyncer
from .limiter import DistributedAgentLimiter, MemoryAgentLimiter, RedisDistributedLimiter
from .manifest import LaunchService
from .metrics import metrics_router
from .models import BootstrapResult, ExperimentRequest, ExperimentResult
from .orchestrator import LaunchOrchestrator
from .queue import MemoryQueueAdapter, QueueAdapter, RedisStreamQueueAdapter
from .reconciler import ExecutionReconciler
from .registry import AgentRegistry
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

def init_queue_and_limiter(
    redis_url: str | None,
    db_mode: str,
    db_url: str,
) -> tuple[QueueAdapter, DistributedAgentLimiter]:
    """Initialize queue adapter and limiter.

    - If redis_url is provided, uses Redis Streams queue adapter and distributed limiter.
    - If in test mode or running on SQLite, falls back to in-memory queue and limiter.
    - Otherwise (production PostgreSQL without Redis), fails closed to prevent uncoordinated multi-instance execution.
    """
    if redis_url:
        import redis
        redis_client = redis.Redis.from_url(redis_url)
        return RedisStreamQueueAdapter(redis_client), RedisDistributedLimiter(redis_client)

    if db_mode == "test" or db_url.startswith("sqlite"):
        return MemoryQueueAdapter(), MemoryAgentLimiter()

    raise RuntimeError("ARGUS_REDIS_URL must be configured in production mode")


# 3. Initialize Queue & Limiter
queue_adapter, limiter = init_queue_and_limiter(
    redis_url=settings.argus_redis_url,
    db_mode=settings.argus_db_mode,
    db_url=db_manager.db_url,
)

def is_langfuse_configured() -> bool:
    import os

    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def _client():
    """Production provider: returns Langfuse client if configured with public/secret keys, else None."""
    if not is_langfuse_configured():
        return None
    return get_client()


# 4. Initialize Orchestrator, Worker, Reconciler, OutboxSyncer
orchestrator = LaunchOrchestrator(db_manager, queue_adapter, limiter)
worker = ExecutionWorker(db_manager, queue_adapter, limiter)
reconciler = ExecutionReconciler(db_manager, queue_adapter, limiter)
outbox_syncer = LangfuseOutboxSyncer(db_manager, langfuse_client=_client)

# 5. Initialize Launch Service
launch_service = LaunchService(db_manager, registry, runner_version=settings.runner_version)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = None
    reconciler_task = None
    syncer_task = None
    stop_event = asyncio.Event()

    async def _worker_loop():
        semaphore = asyncio.Semaphore(settings.worker_concurrency)
        running_tasks = set()

        async def _process_item(m_id: str, i_id: str, g: int):
            async with semaphore:
                try:
                    await worker.execute_item_message(m_id, i_id, g)
                except Exception:
                    pass

        while not stop_event.is_set():
            try:
                available_slots = settings.worker_concurrency - len(running_tasks)
                if available_slots <= 0:
                    await asyncio.sleep(0.05)
                    continue

                fetch_count = min(available_slots, 10)
                msgs = await asyncio.to_thread(worker.poll_queue, count=fetch_count, block_ms=1000)
                if not msgs:
                    await asyncio.sleep(0.05)
                    continue

                for msg_id, item_id, gen in msgs:
                    task = asyncio.create_task(_process_item(msg_id, item_id, gen))
                    running_tasks.add(task)
                    task.add_done_callback(running_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(1)

        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

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

    async def _syncer_loop():
        while not stop_event.is_set():
            try:
                processed = await asyncio.to_thread(outbox_syncer.process_batch, batch_size=1)
                if processed == 0:
                    await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2.0)

    if settings.argus_worker_enabled and settings.argus_db_mode != "test":
        worker_task = asyncio.create_task(_worker_loop())
    if settings.argus_reconciler_enabled and settings.argus_db_mode != "test":
        reconciler_task = asyncio.create_task(_reconciler_loop())
    if settings.argus_db_mode != "test":
        syncer_task = asyncio.create_task(_syncer_loop())

    yield

    stop_event.set()
    if worker_task:
        worker_task.cancel()
    if reconciler_task:
        reconciler_task.cancel()
    if syncer_task:
        syncer_task.cancel()


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

# Remote Experiment and W3C trace propagation contract: inject(headers) handled in LaunchExecutionService




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
async def run_experiment(request: ExperimentRequest) -> ExperimentResult:
    launch_id = str(uuid.uuid4())
    try:
        await asyncio.to_thread(_wait_for_langfuse)
        lf = _client()
        dataset = lf.get_dataset(request.dataset_name)
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

        # Delegate execution to the unified LaunchExecutionService
        svc = LaunchExecutionService(db_manager, registry)
        outcome = await svc.execute_launch(persisted.id, dataset_client=dataset)

        summary = outcome.result_summary or {}
        if not summary.get("dataset_run_url") and outcome.dataset_run_url:
            summary["dataset_run_url"] = outcome.dataset_run_url
        if not summary.get("experiment_id") and outcome.langfuse_experiment_id:
            summary["experiment_id"] = outcome.langfuse_experiment_id

        return ExperimentResult(
            launch_id=persisted.id,
            agent_id=persisted.agent_id,
            agent_version=persisted.agent_version,
            experiment_name=persisted.name,
            dataset_run_url=outcome.dataset_run_url or summary.get("dataset_run_url"),
            result=summary,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"experiment failed: {exc}") from exc


