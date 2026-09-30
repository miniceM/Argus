from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .dataset import DatasetResolver
from .db import DatabaseManager
from .db_models import ExperimentLaunchRecord
from .evaluator_binding import MANIFEST_BINDING_SCHEMA_VERSION, freeze_binding
from .evaluators import EvaluatorSelectionError, default_evaluator_registry
from .registry import AgentRegistry
from .runner_identity import current_runner_identity, validate_runner_identity


def compute_payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def acquire_launch_execution(db_manager: DatabaseManager, launch_id: str) -> bool:
    """Atomically acquire the execution lock for a launch via conditional update."""
    with db_manager.get_session() as session:
        stmt = (
            update(ExperimentLaunchRecord)
            .where(ExperimentLaunchRecord.id == launch_id, ExperimentLaunchRecord.status == "PENDING")
            .values(status="RUNNING", started_at=datetime.utcnow())
        )
        res = session.execute(stmt)
        session.commit()
        return bool(res.rowcount > 0)


class LaunchService:
    def __init__(self, db_manager: DatabaseManager, registry: AgentRegistry, runner_version: str = "0.1.0"):
        self.db_manager = db_manager
        self.registry = registry
        self.runner_version = runner_version

    def create_launch(
        self,
        agent_id: str,
        agent_version: str,
        dataset_name: str,
        dataset_version: str | None = None,
        environment: str = "production",
        baseline_snapshot_id: str | None = None,
        dataset_id: str | None = None,
        name: str | None = None,
        idempotency_key: str | None = None,
        max_concurrency: int | None = None,
        evaluator_ids: list[str] | None = None,
        evaluator_selections: list[dict[str, Any]] | None = None,
        items_count: int | None = None,
        item_ids: list[str] | None = None,
        created_by: str | None = None,
        launch_id: str | None = None,
        dataset_snapshot: dict[str, Any] | None = None,
        dataset_client: Any | None = None,
        allow_run_scope: bool = False,
    ) -> ExperimentLaunchRecord:
        from .baselines import normalize_environment

        normalized_environment = normalize_environment(environment)
        runner_identity = current_runner_identity(runner_version=self.runner_version)
        identity_error = validate_runner_identity(runner_identity.model_dump(), runner_identity)
        if identity_error:
            raise ValueError(f"{identity_error}: runner build identity is unavailable")
        # ---- Issue #80: normalize the request into explicit (id, version) selections ----
        if evaluator_selections is not None and evaluator_ids is not None:
            raise EvaluatorSelectionError(
                "Provide either evaluator_selections or evaluator_ids, not both.",
                code="EVALUATOR_SELECTION_AMBIGUOUS",
            )

        if evaluator_selections is not None:
            if len(evaluator_selections) == 0:
                raise EvaluatorSelectionError(
                    "evaluator_selections must not be empty. A launch must have at least one evaluator.",
                    code="EVALUATOR_SELECTION_EMPTY",
                )
            requested = [
                (str(item["id"]), str(item["version"])) for item in evaluator_selections
            ]
        elif evaluator_ids is not None:
            if len(evaluator_ids) == 0:
                raise EvaluatorSelectionError(
                    "evaluator_ids must not be empty. A launch must have at least one evaluator.",
                    code="EVALUATOR_SELECTION_EMPTY",
                )
            requested = [(evaluator_id, None) for evaluator_id in evaluator_ids]
        else:
            requested = [
                (evaluator_id, None)
                for evaluator_id in default_evaluator_registry.default_item_ids()
            ]

        seen: set[str] = set()
        for evaluator_id, _ver in requested:
            if evaluator_id in seen:
                raise EvaluatorSelectionError(
                    f"Duplicate evaluator selection: '{evaluator_id}'.",
                    code="EVALUATOR_SELECTION_DUPLICATE",
                    evaluator_id=evaluator_id,
                )
            seen.add(evaluator_id)

        # Issue #80: every selection goes through the single server-side
        # release-eligibility gate, so a hand-crafted request cannot bypass the UI.
        eval_specs = [
            default_evaluator_registry.resolve_for_release(
                evaluator_id,
                version,
                required_scope=None if allow_run_scope else "item",
            )
            for evaluator_id, version in requested
        ]
        eval_specs.sort(key=lambda spec: spec["id"])

        # ---- Issue #81: freeze the *execution identity*, not only id/version ----
        # A Launch is only reproducible when the implementation artifact and the
        # Runner identity that produced it are recorded and re-verified later.
        eval_specs = [
            freeze_binding(
                default_evaluator_registry.definition(spec["id"]),
                default_evaluator_registry.version(spec["id"], spec["version"]),
                runner_identity=runner_identity.model_dump(),
                composed_of=default_evaluator_registry.definition(spec["id"]).composed_of,
            ).to_payload()
            for spec in eval_specs
        ]


        # Request payload for idempotency checking (calculated upfront)
        payload_data = {
            "agent_id": agent_id,
            "agent_version": agent_version,
            "dataset_name": dataset_name,
            "dataset_version": dataset_version,
            "environment": normalized_environment,
            "baseline_snapshot_id": baseline_snapshot_id,
            "evaluator_selections": [
                {"id": spec["id"], "version": spec["version"]} for spec in eval_specs
            ],
            "max_concurrency": max_concurrency,
            "name": name,
        }
        payload_digest = compute_payload_digest(payload_data)

        # Upfront idempotency check: never call external services if key already exists
        if idempotency_key:
            with self.db_manager.get_session() as session:
                stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.idempotency_key == idempotency_key)
                existing = session.scalars(stmt).first()
                if existing:
                    if existing.request_payload_digest == payload_digest:
                        return existing
                    raise ValueError(
                        f"Idempotency key conflict: key '{idempotency_key}' already used with different payload."
                    )

        ver_rec = self.registry.get_version(agent_id, agent_version)
        if not ver_rec:
            raise ValueError(f"AgentVersion '{agent_id}:{agent_version}' not found")
        if not ver_rec.is_active:
            raise ValueError(f"AgentVersion '{agent_id}:{agent_version}' is archived/inactive")

        agent_rec = self.registry.get_agent(agent_id)
        if not agent_rec or agent_rec.status != "active":
            raise ValueError(
                f"Agent '{agent_id}' 处于不可用状态 '{getattr(agent_rec, 'status', 'not_found')}'，不可创建新的评测任务"
            )

        # Concurrency policy: inherit from AgentVersion if None, enforce limit if specified
        if max_concurrency is not None:
            if max_concurrency > ver_rec.max_concurrency:
                raise ValueError(
                    f"Requested max_concurrency ({max_concurrency}) exceeds AgentVersion limit ({ver_rec.max_concurrency})"
                )
            effective_concurrency = max_concurrency
        else:
            effective_concurrency = ver_rec.max_concurrency

        # Resolve full frozen dataset snapshot
        if dataset_snapshot is not None:
            resolved_snapshot = dataset_snapshot
        else:
            resolver = DatasetResolver()
            resolved_snapshot = resolver.resolve(
                dataset_name, dataset_version, dataset_client=dataset_client
            )
        effective_dataset_id = dataset_id or resolved_snapshot["dataset_id"]


        launch_name = name or f"{agent_id}-{agent_version}-{dataset_name}"

        # Build 4D Manifest snapshot
        manifest = {
            "schema_version": MANIFEST_BINDING_SCHEMA_VERSION,
            "dataset": resolved_snapshot,
            "comparison": {
                "environment": normalized_environment,
                "baseline_snapshot_id": None,
                "baseline_binding_revision": None,
                "baseline_resolution": "none",
                "comparison_policy_version": "comparison-v1",
            },
            "agent": {
                "agent_id": ver_rec.agent_id,
                "version": ver_rec.version,
                "agent_version_id": ver_rec.id,
                "endpoint": ver_rec.endpoint,
                "protocol": ver_rec.protocol,
                "method": ver_rec.method,
                "request_mapping": dict(ver_rec.request_mapping or {}),
                "usage_cost_mapping": dict(ver_rec.usage_cost_mapping or {}) or None,
                "credential_ref": ver_rec.credential_ref,
                "spec_digest": ver_rec.spec_digest,
                "artifact_ref": ver_rec.artifact_ref,
                "is_idempotent": ver_rec.is_idempotent,
            },
            "evaluators": eval_specs,
            "quality_policy": {
                "mode": "all_selected_must_pass",
                "threshold_rule": "score >= threshold",
            },
            "runner": {
                **runner_identity.model_dump(),
            },
            "execution_policy": {
                "max_concurrency": effective_concurrency,
                "timeout_seconds": ver_rec.timeout_seconds,
                "max_retries": ver_rec.max_retries,
                "rate_limit_per_minute": ver_rec.rate_limit_per_minute,
            },
        }

        effective_launch_id = launch_id or str(uuid.uuid4())
        launch = ExperimentLaunchRecord(
            id=effective_launch_id,
            name=launch_name,
            status="PENDING",
            quality_conclusion="unknown",
            idempotency_key=idempotency_key,
            request_payload_digest=payload_digest,
            dataset_id=effective_dataset_id,
            dataset_name=dataset_name,
            dataset_version=resolved_snapshot["dataset_version"],
            agent_id=agent_id,
            agent_version=agent_version,
            agent_version_id=ver_rec.id,
            manifest=manifest,
            langfuse_sync_status="PENDING",
            created_by=created_by,
        )

        with self.db_manager.get_session() as session:
            try:
                # Lock agent row to prevent concurrent deletion and ensure active status within transaction
                from .db_models import AgentRecord
                agent_rec = session.scalar(
                    select(AgentRecord).where(AgentRecord.id == agent_id).with_for_update()
                )
                if not agent_rec or agent_rec.status != "active":
                    raise ValueError(
                        f"Agent '{agent_id}' 处于不可用状态 '{getattr(agent_rec, 'status', 'not_found')}'，不可创建新的评测任务"
                    )

                from .baselines import validate_baseline_snapshot
                from .db_models import BaselineBindingRecord, RunResultSnapshotRecord

                comparison = dict(manifest["comparison"])
                comparison["environment"] = normalized_environment
                if baseline_snapshot_id:
                    selected_snapshot = session.get(RunResultSnapshotRecord, baseline_snapshot_id)
                    if not selected_snapshot:
                        raise ValueError("Requested baseline result snapshot was not found for this Agent")
                    selected_launch = session.get(ExperimentLaunchRecord, selected_snapshot.launch_id)
                    if not selected_launch:
                        raise ValueError("Requested baseline Launch was not found")
                    validate_baseline_snapshot(selected_snapshot, selected_launch, agent_id)
                    comparison.update(
                        baseline_snapshot_id=selected_snapshot.id,
                        baseline_binding_revision=None,
                        baseline_resolution="explicit",
                    )
                else:
                    binding_stmt = select(BaselineBindingRecord).where(
                        BaselineBindingRecord.agent_id == agent_id,
                        BaselineBindingRecord.environment == normalized_environment,
                    )
                    if self.db_manager.engine.dialect.name == "postgresql":
                        binding_stmt = binding_stmt.with_for_update()
                    binding = session.scalars(binding_stmt).first()
                    if binding and binding.result_snapshot_id:
                        comparison.update(
                            baseline_snapshot_id=binding.result_snapshot_id,
                            baseline_binding_revision=binding.revision,
                            baseline_resolution="automatic",
                        )
                manifest["comparison"] = comparison
                launch.manifest = manifest
                session.add(launch)
                session.flush()

                # Materialize all dataset items as PENDING
                from .db_models import ExperimentItemExecutionRecord
                items_seed = manifest.get("dataset", {}).get("items", [])
                for item in items_seed:
                    item_id = str(item.get("id", uuid.uuid4()))
                    item_rec = ExperimentItemExecutionRecord(
                        id=str(uuid.uuid4()),
                        launch_id=launch.id,
                        dataset_item_id=item_id,
                        execution_status="pending",
                        eval_status="pending",
                        quality_conclusion="unknown",
                        dispatch_generation=1,
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    session.add(item_rec)

                session.commit()
                session.refresh(launch)
                return launch
            except IntegrityError as exc:
                session.rollback()
                if idempotency_key and ("idempotency_key" in str(exc).lower() or "uq_experiment_launches_idempotency_key" in str(exc).lower()):
                    existing = session.scalars(
                        select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.idempotency_key == idempotency_key)
                    ).first()
                    if existing:
                        if existing.request_payload_digest == payload_digest:
                            return existing
                        raise ValueError(
                            f"Idempotency key conflict: key '{idempotency_key}' already used with different payload."
                        ) from exc
                orig_err = str(getattr(exc, "orig", exc)).lower()
                pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
                if "foreign key" in orig_err or pgcode == "23503":
                    raise ValueError(
                        f"Agent '{agent_id}' 或其版本规格已被并发清理或不可用，无法创建评测任务"
                    ) from exc
                raise



    def get_launch(self, launch_id: str) -> ExperimentLaunchRecord | None:
        with self.db_manager.get_session() as session:
            return session.get(ExperimentLaunchRecord, launch_id)

    def list_launches(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        quality_conclusion: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ExperimentLaunchRecord]:
        with self.db_manager.get_session() as session:
            stmt = select(ExperimentLaunchRecord)
            if agent_id:
                stmt = stmt.where(ExperimentLaunchRecord.agent_id == agent_id)
            if status:
                normalized_status = status.upper()
                if normalized_status in ("SUCCEEDED", "COMPLETED"):
                    stmt = stmt.where(ExperimentLaunchRecord.status.in_(["COMPLETED", "SUCCEEDED"]))
                else:
                    stmt = stmt.where(ExperimentLaunchRecord.status == normalized_status)
            if quality_conclusion:
                stmt = stmt.where(ExperimentLaunchRecord.quality_conclusion == quality_conclusion.lower())
            stmt = stmt.order_by(ExperimentLaunchRecord.created_at.desc())
            if offset > 0:
                stmt = stmt.offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            return list(session.scalars(stmt).all())
