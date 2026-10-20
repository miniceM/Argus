"""Run-item write strategy: legacy REST vs Langfuse v4 OTel projection.

Argus writes the same logical run-item link through two strategies:

- ``legacy``  — existing ``lf.api.dataset_run_items.create``; unchanged behavior
  for self-hosted Langfuse or a v3 organization where that endpoint materializes.
- ``otel``    — self-contained OTel projection POST (P1/P2 contract) for v4
  cloud organizations where the legacy endpoint returns 200 but does not
  project into ``GET /api/public/experiments``.

The strategy is read per sync attempt (no cached decision) so a test or
deployment can switch it with an env var without a runner restart.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

from .langfuse_otel_projection import replay_experiment_item

logger = logging.getLogger("argus.langfuse_run_items")

STRATEGY_ENV = "ARGUS_LANGFUSE_RUN_ITEM_STRATEGY"
LEGACY_STRATEGY = "legacy"
OTEL_STRATEGY = "otel"
VALID_STRATEGIES = (LEGACY_STRATEGY, OTEL_STRATEGY)

_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"


def resolve_run_item_strategy() -> str:
    """Read the strategy from the environment each time it is needed."""
    configured = os.getenv(STRATEGY_ENV)
    raw = "legacy" if configured is None else configured.strip().lower()
    if raw not in VALID_STRATEGIES:
        raise ValueError(f"Invalid {STRATEGY_ENV}; expected legacy or otel")
    return raw


def self_assigned_run_id(*, launch_id: str, run_name: str) -> str:
    """Stable experiment id used by the v4 projection path.

    Langfuse v4 requires a UUID-shaped experiment id; determinism makes a re-run
    idempotent and lets the link service derive the same URL across workers.
    """
    digest_source = f"argus-run:{launch_id}:{run_name}".encode()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, digest_source.decode("utf-8")))


def link_dataset_run_item(
    *,
    strategy: str,
    lf: Any,
    launch_id: str,
    run_name: str,
    dataset_id: str | None = None,
    dataset_item_id: str,
    dataset_version: str,
    trace_id: str,
    observation_id: str | None,
    # OTel-only credentials. Passing none keeps the legacy path intact; passing
    # none and selecting `otel` is a configuration error that surfaces at the
    # call site instead of silently reverting to legacy.
    langfuse_base_url: str | None = None,
    langfuse_public_key: str | None = None,
    langfuse_secret_key: str | None = None,
    timeout_seconds: float = 30.0,
    invoke_legacy: Callable[..., Any] | None = None,
    observation_projection: dict[str, Any] | None = None,
) -> str | None:
    """One-shot run-item dispatch.

    Returns the run id to propagate into the outbox `_dataset_run_id` write-back.
    Returns ``None`` only on the legacy path when the remote response does not
    carry one (e.g. a local test fake) — never silently for `otel`.
    """
    if strategy == LEGACY_STRATEGY:
        invoke = invoke_legacy or lf.api.dataset_run_items.create
        res = invoke(
            run_name=run_name,
            dataset_item_id=dataset_item_id,
            trace_id=trace_id,
            observation_id=observation_id,
            dataset_version=dataset_version,
        )
        return getattr(res, "dataset_run_id", None)

    if strategy == OTEL_STRATEGY:
        if not dataset_id:
            raise ValueError("OTel run-item projection requires a Langfuse dataset_id")
        if not observation_id:
            raise ValueError("OTel run-item projection requires an observation_id span id")
        if not langfuse_base_url or not langfuse_public_key or not langfuse_secret_key:
            raise ValueError(
                "OTel run-item projection requires Langfuse base URL, public key, and secret key"
            )
        run_id = self_assigned_run_id(launch_id=launch_id, run_name=run_name)
        replay_experiment_item(
            trace_id=trace_id,
            span_id=str(observation_id),
            dataset_id=str(dataset_id),
            dataset_item_id=str(dataset_item_id),
            run_id=run_id,
            run_name=run_name,
            dataset_version=dataset_version,
            credentials=OtelProjectionCredentials(
                base_url=langfuse_base_url,
                public_key=langfuse_public_key,
                secret_key=langfuse_secret_key,
            ),
            timeout_seconds=timeout_seconds,
            **(observation_projection or {}),
        )
        return run_id

    raise ValueError(f"Unknown run-item strategy {strategy!r}")


class OtelProjectionCredentials:
    """Typed holder for basic-auth credentials passed to the projection layer."""

    def __init__(self, *, base_url: str, public_key: str, secret_key: str):
        self.base_url = base_url
        self.public_key = public_key
        self.secret_key = secret_key


def read_managed_observation_projection(db_mgr: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """从成功 Checkpoint 为现有 Outbox 恢复可评分 TOOL，不依赖 SDK 的内存缓冲。

    同一 trace/span、固定时间和输入输出保证重放身份稳定；Builtin/失败执行
    仍使用原始关联标记。Checkpoint 损坏/失配必须走现有 Outbox 失败恢复，
    不能伪造空输入输出，更不能重调 Agent。
    """
    import json
    from datetime import UTC

    from .db_models import ExperimentItemExecutionRecord, ExperimentLaunchRecord
    from .execution_checkpoint import CheckpointUnavailableError, load_recoverable_checkpoint
    from .managed_evaluation import evaluation_source

    with db_mgr.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, payload["launch_id"])
        if launch is None or evaluation_source(launch.manifest) != "langfuse_managed":
            return None
        item = session.get(ExperimentItemExecutionRecord, payload["item_id"])
        if item is None or item.execution_status != "succeeded":
            return None
        checkpoint = load_recoverable_checkpoint(
            session, item_execution_id=item.id, dispatch_generation=payload["dispatch_generation"],
        )
        if (item.dispatch_generation != payload["dispatch_generation"]
                or checkpoint.trace_id != payload["trace_id"]
                or checkpoint.observation_id != payload.get("observation_id")):
            raise CheckpointUnavailableError("CHECKPOINT_MISMATCH")
        contract = (launch.manifest or {}).get("managed_evaluation") or {}
        created = checkpoint.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        attributes = [
            ("langfuse.observation.type", "tool"),
            ("langfuse.environment", contract["environment"]),
            ("langfuse.observation.input", json.dumps(
                {"agent": f"{launch.agent_id}:{launch.agent_version}", "request": checkpoint.input_payload},
                ensure_ascii=False, allow_nan=False,
            )),
            ("langfuse.observation.output", json.dumps(checkpoint.agent_output, ensure_ascii=False, allow_nan=False)),
            ("langfuse.observation.metadata.launch_id", launch.id),
            ("langfuse.observation.metadata.item_id", item.id),
            ("langfuse.observation.metadata.generation", str(payload["dispatch_generation"])),
            ("langfuse.observation.metadata.argus_checkpoint_digest", checkpoint.output_digest),
        ]
        return {
            "span_name": contract["target"]["name"],
            "start_unix_nano": int(created.timestamp() * 1_000_000_000),
            "attributes": attributes,
        }
