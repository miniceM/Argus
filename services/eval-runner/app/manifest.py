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
from .evaluators import default_evaluator_registry
from .registry import AgentRegistry


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
        dataset_id: str | None = None,
        name: str | None = None,
        idempotency_key: str | None = None,
        max_concurrency: int = 4,
        evaluator_ids: list[str] | None = None,
        items_count: int | None = None,
        item_ids: list[str] | None = None,
        created_by: str | None = None,
        launch_id: str | None = None,
    ) -> ExperimentLaunchRecord:
        ver_rec = self.registry.get_version(agent_id, agent_version)
        if not ver_rec:
            raise ValueError(f"AgentVersion '{agent_id}:{agent_version}' not found")
        if not ver_rec.is_active:
            raise ValueError(f"AgentVersion '{agent_id}:{agent_version}' is archived/inactive")

        eval_list = evaluator_ids or ["intent_match", "required_tool_match", "pii_safe", "escalation_match"]
        eval_specs = [default_evaluator_registry.resolve(eid) for eid in eval_list]

        # Resolve full frozen dataset snapshot
        resolver = DatasetResolver()
        dataset_snapshot = resolver.resolve(dataset_name, dataset_version)
        effective_dataset_id = dataset_id or dataset_snapshot["dataset_id"]

        launch_name = name or f"{agent_id}-{agent_version}-{dataset_name}"

        # Request payload for idempotency checking
        payload_data = {
            "name": name,
            "agent_id": agent_id,
            "agent_version": agent_version,
            "dataset_name": dataset_name,
            "dataset_version": dataset_version,
            "max_concurrency": max_concurrency,
            "evaluator_ids": sorted(eval_list),
        }
        payload_digest = compute_payload_digest(payload_data)

        with self.db_manager.get_session() as session:
            if idempotency_key:
                stmt = select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.idempotency_key == idempotency_key)
                existing = session.scalars(stmt).first()
                if existing:
                    if existing.request_payload_digest == payload_digest:
                        return existing
                    else:
                        raise ValueError(
                            f"Idempotency key conflict: key '{idempotency_key}' already used with different payload."
                        )

            # Build 4D Manifest snapshot
            manifest = {
                "schema_version": "1.0",
                "dataset": dataset_snapshot,
                "agent": {
                    "agent_id": ver_rec.agent_id,
                    "version": ver_rec.version,
                    "agent_version_id": ver_rec.id,
                    "endpoint": ver_rec.endpoint,
                    "protocol": ver_rec.protocol,
                    "method": ver_rec.method,
                    "request_mapping": dict(ver_rec.request_mapping or {}),
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
                    "runner_version": self.runner_version,
                    "mapping_engine_version": "sha256-mapping-engine-v1",
                },
                "execution_policy": {
                    "max_concurrency": max_concurrency,
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
                dataset_version=dataset_version,
                agent_id=agent_id,
                agent_version=agent_version,
                agent_version_id=ver_rec.id,
                manifest=manifest,
                langfuse_sync_status="PENDING",
                created_by=created_by,
            )

            try:
                session.add(launch)
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
                raise


    def get_launch(self, launch_id: str) -> ExperimentLaunchRecord | None:
        with self.db_manager.get_session() as session:
            return session.get(ExperimentLaunchRecord, launch_id)

    def list_launches(self) -> list[ExperimentLaunchRecord]:
        with self.db_manager.get_session() as session:
            stmt = select(ExperimentLaunchRecord).order_by(ExperimentLaunchRecord.created_at.desc())
            return list(session.scalars(stmt).all())

