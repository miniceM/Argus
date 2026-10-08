"""Shared fixtures for Langfuse link backfill tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db_models import ExperimentItemExecutionRecord as Item
from app.db_models import ExperimentLaunchRecord as Launch
from app.db_models import LangfuseSyncTaskRecord as Task
from app.langfuse_links import LangfuseLaunchLinkService, LangfuseLinkResolver


def seed_terminal_launch(
    env: tuple,
    *,
    dataset_name: str = "banking-agent-regression",
    dataset_id: str | None = "ds-real",
    dataset_source: str = "langfuse",
    launch_status: str = "COMPLETED",
    sync_status: str = "SYNCED",
    store_run_id: str | None = "r1",
    store_url: str | None = None,
    task_status: str = "SYNCED",
    task_run_id: str | None = "r1",
    task_run_name: str = "review",
    item_generation: int = 1,
    item_count: int = 1,
    task_generation: int | None = None,
    task_source: str | None = None,
    extra_tasks: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """Creates a terminal launch with one item and (optionally) one outbox task."""
    db_mgr, queue, limiter, orch, worker, rec = env
    manifest = {
        "agent": {
            "agent_id": "test-agent",
            "version": "v1",
            "agent_version_id": "test-agent-v1",
            "endpoint": "http://localhost/invoke",
            "method": "POST",
            "request_mapping": {},
            "is_idempotent": False,
        },
        "execution_policy": {
            "timeout_seconds": 60,
            "max_retries": 2,
            "max_concurrency": 1,
            "rate_limit_per_minute": 60,
        },
        "dataset": {
            "source": dataset_source,
            "items": [{"id": str(i), "input": {}} for i in range(item_count)],
        },
        "evaluators": [],
    }
    if dataset_id is not None:
        manifest["dataset"]["dataset_id"] = dataset_id

    launch = orch.create_launch("test-agent", "v1", dataset_name, "v1", "review", manifest)
    orch.start_launch(launch.id)
    msgs = queue.read_group("review", count=item_count)
    item_id = msgs[0][1]
    item_ids = [m[1] for m in msgs]

    with db_mgr.get_session() as session:
        launch_rec = session.get(Launch, launch.id)
        launch_rec.status = launch_status
        launch_rec.langfuse_sync_status = sync_status
        launch_rec.langfuse_experiment_id = store_run_id
        launch_rec.langfuse_experiment_url = store_url
        item = session.get(Item, item_id)
        item.execution_status = "succeeded"
        item.eval_status = "succeeded"
        item.trace_id = "a" * 32
        item.dispatch_generation = item_generation

        if task_status is not None:
            payload: dict[str, Any] = {"quality": 1}
            if task_source is not None:
                payload["_dataset_source"] = task_source
            if task_run_id is not None:
                payload["_dataset_run_id"] = task_run_id
            session.add(
                Task(
                    id=f"task-{uuid.uuid4().hex[:8]}",
                    launch_id=launch.id,
                    item_id=item_id,
                    dataset_item_id="0",
                    dispatch_generation=task_generation if task_generation is not None else item_generation,
                    task_type="FULL_EVAL_SYNC",
                    trace_id="a" * 32,
                    dataset_run_name=task_run_name,
                    dataset_version="2026-01-01T00:00:00Z",
                    scores_payload=payload,
                    status=task_status,
                    next_retry_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
        for spec in extra_tasks or []:
            index = spec.get("item_index", 0)
            session.add(
                Task(
                    id=f"task-{uuid.uuid4().hex[:8]}",
                    launch_id=launch.id,
                    item_id=item_ids[index],
                    dataset_item_id=str(index),
                    dispatch_generation=spec.get("generation", item_generation),
                    task_type="FULL_EVAL_SYNC",
                    trace_id="a" * 32,
                    dataset_run_name=spec.get("run_name", task_run_name),
                    dataset_version="2026-01-01T00:00:00Z",
                    scores_payload=spec.get("payload", {}),
                    status=spec.get("status", "SYNCED"),
                    next_retry_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )
    return launch.id, item_id


def make_service(db_mgr, lf_client, dashboard_base: str | None) -> LangfuseLaunchLinkService:
    return LangfuseLaunchLinkService(db_mgr, LangfuseLinkResolver(lf_client, dashboard_base))
