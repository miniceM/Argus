from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .config import find_path
from .db import DatabaseManager
from .db_models import (
    AgentRecord,
    AgentVersionRecord,
    ExperimentItemExecutionRecord,
    ExperimentLaunchRecord,
    LangfuseSyncTaskRecord,
)
from .models import (
    AgentConcurrencyError,
    AgentHasActiveLaunchesError,
    AgentHasLaunchesError,
    AgentNameMismatchError,
    AgentNotFoundError,
)
from .state_machine import TERMINAL_LAUNCH_STATUSES


def compute_spec_digest(spec_dict: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 digest of normalized execution specification."""
    keys_to_include = [
        "artifact_ref",
        "credential_ref",
        "endpoint",
        "environment",
        "is_idempotent",
        "max_concurrency",
        "max_retries",
        "method",
        "protocol",
        "rate_limit_per_minute",
        "request_mapping",
        "request_schema",
        "response_schema",
        "timeout_seconds",
        "trace_propagation",
    ]
    normalized: dict[str, Any] = {}
    for k in sorted(keys_to_include):
        val = spec_dict.get(k)
        if val is not None:
            normalized[k] = val

    canonical_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def normalize_and_validate_spec(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize AgentVersion specification fields using shared Pydantic validator."""
    from .models import AgentVersionSpecValidator

    try:
        model = AgentVersionSpecValidator.model_validate(data)
    except Exception as exc:
        msg = str(exc)
        if hasattr(exc, "errors"):
            errs = exc.errors()
            if errs and "msg" in errs[0]:
                custom_msg = errs[0]["msg"]
                if "Value error," in custom_msg:
                    msg = custom_msg.split("Value error,", 1)[1].strip()
        raise ValueError(msg) from exc

    normalized = model.model_dump()
    digest = compute_spec_digest(normalized)
    normalized["spec_digest"] = digest
    return normalized



@dataclass(frozen=True)
class AgentVersionSpec:
    agent_id: str
    version: str
    endpoint: str
    method: str
    timeout_seconds: float
    max_retries: int
    rate_limit_per_minute: int
    request_mapping: dict[str, str]
    max_concurrency: int = 4
    credential_ref: str | None = None
    is_idempotent: bool = False
    spec_digest: str = ""
    artifact_ref: str | None = None
    id: str = ""


class AgentRegistry:
    """Persistent Agent and AgentVersion registry."""

    def __init__(self, target: DatabaseManager | str | Path):
        if isinstance(target, (str, Path)):
            path = Path(target)
            self.db_manager = DatabaseManager("sqlite:///:memory:")
            migrations_dir = find_path("/app/migrations", "migrations")
            from .db import MigrationRunner

            MigrationRunner(self.db_manager.engine, migrations_dir).apply_all()
            self.import_yaml(path)
        else:
            self.db_manager = target


    def create_agent(
        self,
        agent_id: str,
        name: str,
        description: str | None = None,
        owner: str | None = None,
    ) -> AgentRecord:
        with self.db_manager.get_session() as session:
            existing = session.get(AgentRecord, agent_id)
            if existing:
                raise ValueError(f"Agent '{agent_id}' already exists")

            agent = AgentRecord(
                id=agent_id,
                name=name,
                description=description,
                owner=owner,
                status="active",
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)
            return agent

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self.db_manager.get_session() as session:
            return session.get(AgentRecord, agent_id)

    def get_agent_summary(self, agent_id: str) -> dict[str, Any] | None:
        with self.db_manager.get_session() as session:
            agent = session.get(AgentRecord, agent_id)
            if not agent:
                return None
            v_count = (
                session.scalar(
                    select(func.count(AgentVersionRecord.id)).where(AgentVersionRecord.agent_id == agent_id)
                )
                or 0
            )
            latest_active = session.scalar(
                select(AgentVersionRecord.version)
                .where(AgentVersionRecord.agent_id == agent_id, AgentVersionRecord.is_active.is_(True))
                .order_by(AgentVersionRecord.created_at.desc())
                .limit(1)
            )
            l_count = (
                session.scalar(
                    select(func.count(ExperimentLaunchRecord.id)).where(ExperimentLaunchRecord.agent_id == agent_id)
                )
                or 0
            )
            active_l_count = (
                session.scalar(
                    select(func.count(ExperimentLaunchRecord.id)).where(
                        ExperimentLaunchRecord.agent_id == agent_id,
                        ExperimentLaunchRecord.status.not_in(TERMINAL_LAUNCH_STATUSES),
                    )
                )
                or 0
            )
            return {
                "id": agent.id,
                "name": agent.name,
                "description": agent.description,
                "owner": agent.owner,
                "status": agent.status,
                "version_count": v_count,
                "latest_version": latest_active,
                "launch_count": l_count,
                "active_launch_count": active_l_count,
                "created_at": agent.created_at,
                "updated_at": agent.updated_at,
            }

    def list_agents(self) -> list[AgentRecord]:
        with self.db_manager.get_session() as session:
            stmt = select(AgentRecord).order_by(AgentRecord.created_at)
            return list(session.scalars(stmt).all())

    def list_agents_summary(self) -> list[dict[str, Any]]:
        with self.db_manager.get_session() as session:
            # 1. Total version count per agent
            v_counts = dict(
                session.execute(
                    select(
                        AgentVersionRecord.agent_id,
                        func.count(AgentVersionRecord.id),
                    ).group_by(AgentVersionRecord.agent_id)
                ).all()
            )

            # 2. Latest active version by created_at per agent
            active_rows = session.execute(
                select(AgentVersionRecord.agent_id, AgentVersionRecord.version)
                .where(AgentVersionRecord.is_active.is_(True))
                .order_by(AgentVersionRecord.agent_id, AgentVersionRecord.created_at.desc())
            ).all()
            latest_active: dict[str, str] = {}
            for aid, ver in active_rows:
                if aid not in latest_active:
                    latest_active[aid] = ver

            # 3. Total launch count per agent
            l_counts = dict(
                session.execute(
                    select(
                        ExperimentLaunchRecord.agent_id,
                        func.count(ExperimentLaunchRecord.id),
                    ).group_by(ExperimentLaunchRecord.agent_id)
                ).all()
            )

            # 4. Active launch count per agent
            active_l_counts = dict(
                session.execute(
                    select(
                        ExperimentLaunchRecord.agent_id,
                        func.count(ExperimentLaunchRecord.id),
                    )
                    .where(ExperimentLaunchRecord.status.not_in(TERMINAL_LAUNCH_STATUSES))
                    .group_by(ExperimentLaunchRecord.agent_id)
                ).all()
            )

            # 5. Get all agents
            agents = session.scalars(select(AgentRecord).order_by(AgentRecord.created_at)).all()
            result = []
            for agent in agents:
                result.append(
                    {
                        "id": agent.id,
                        "name": agent.name,
                        "description": agent.description,
                        "owner": agent.owner,
                        "status": agent.status,
                        "version_count": v_counts.get(agent.id, 0),
                        "latest_version": latest_active.get(agent.id),
                        "launch_count": l_counts.get(agent.id, 0),
                        "active_launch_count": active_l_counts.get(agent.id, 0),
                        "created_at": agent.created_at,
                        "updated_at": agent.updated_at,
                    }
                )
            return result

    def delete_agent(
        self, agent_id: str, force: bool = False, confirm_name: str | None = None
    ) -> dict[str, Any]:
        with self.db_manager.get_session() as session:
            # Row-level lock to prevent concurrent modification
            agent = session.scalar(
                select(AgentRecord).where(AgentRecord.id == agent_id).with_for_update()
            )
            if not agent:
                raise AgentNotFoundError(agent_id)

            # Check for existing launches
            launches = session.scalars(
                select(ExperimentLaunchRecord).where(ExperimentLaunchRecord.agent_id == agent_id)
            ).all()
            launch_count = len(launches)

            if launch_count > 0:
                active_launches = [launch for launch in launches if launch.status not in TERMINAL_LAUNCH_STATUSES]
                active_count = len(active_launches)

                if not force:
                    raise AgentHasLaunchesError(
                        agent_id=agent_id,
                        launch_count=launch_count,
                        active_launch_count=active_count,
                    )

                # Server-side strong validation of agent full name
                if confirm_name != agent.name:
                    raise AgentNameMismatchError(
                        agent_id=agent_id,
                        expected_name=agent.name,
                        provided_name=confirm_name or "",
                    )

                # Check for active (non-terminal) launches
                if active_launches:
                    active_summary = ", ".join(f"'{launch.id}' ({launch.status})" for launch in active_launches[:3])
                    if len(active_launches) > 3:
                        active_summary += f" 等共 {len(active_launches)} 个任务"
                    raise AgentHasActiveLaunchesError(
                        agent_id=agent_id,
                        active_summary=active_summary,
                        active_launch_count=len(active_launches),
                    )

                # Set status to "deleting" and flush to block concurrent launch creations
                agent.status = "deleting"
                session.flush()

                # Force delete: clean launches and their dependent records in local DB only.
                # Notice: we DO NOT call Langfuse API/SDK.
                launch_ids = [launch_rec.id for launch_rec in launches]
                if launch_ids:
                    # Clean up sync tasks explicitly for database engines without full ON DELETE CASCADE support (like SQLite in tests)
                    session.execute(
                        delete(LangfuseSyncTaskRecord).where(
                            LangfuseSyncTaskRecord.launch_id.in_(launch_ids)
                        )
                    )
                    # Clear final_attempt_id to avoid circular foreign key dependency during deletion
                    session.execute(
                        update(ExperimentItemExecutionRecord)
                        .where(ExperimentItemExecutionRecord.launch_id.in_(launch_ids))
                        .values(final_attempt_id=None)
                    )
                    for launch in launches:
                        session.delete(launch)
                    session.flush()

            try:
                session.delete(agent)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AgentConcurrencyError(
                    agent_id=agent_id,
                    reason="存在并发创建的评测任务或关联记录冲突，请稍后重试",
                ) from exc

            return {
                "id": agent_id,
                "deleted": True,
                "launches_deleted": launch_count,
                "message": (
                    f"Agent '{agent_id}' 及其本地数据已完全删除"
                    if launch_count == 0
                    else f"Agent '{agent_id}' 及其关联的 {launch_count} 条本地评测记录已完全清理（Langfuse 远程记录完整保留）"
                ),
            }

    def purge_agent(self, agent_id: str, confirm_name: str) -> dict[str, Any]:
        """Irreversible purge: cascade deletes agent and local evaluation history with strict server-side confirmation."""
        return self.delete_agent(agent_id=agent_id, force=True, confirm_name=confirm_name)

    def create_version(
        self,
        agent_id: str,
        version: str,
        endpoint: str,
        method: str = "POST",
        protocol: str = "HTTP_JSON",
        request_mapping: dict[str, str] | None = None,
        request_schema: dict[str, Any] | None = None,
        response_schema: dict[str, Any] | None = None,
        credential_ref: str | None = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        rate_limit_per_minute: int = 600,
        max_concurrency: int = 4,
        is_idempotent: bool = False,
        artifact_ref: str | None = None,
        environment: str | None = None,
        metadata: dict[str, Any] | None = None,
        trace_propagation: str = "W3C",
    ) -> AgentVersionRecord:
        raw_spec = {
            "endpoint": endpoint,
            "protocol": protocol,
            "method": method,
            "request_mapping": request_mapping,
            "request_schema": request_schema,
            "response_schema": response_schema,
            "credential_ref": credential_ref,
            "timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
            "rate_limit_per_minute": rate_limit_per_minute,
            "max_concurrency": max_concurrency,
            "is_idempotent": is_idempotent,
            "artifact_ref": artifact_ref,
            "environment": environment,
            "trace_propagation": trace_propagation,
            "metadata": metadata,
        }
        normalized = normalize_and_validate_spec(raw_spec)

        with self.db_manager.get_session() as session:
            # Check agent existence
            agent = session.get(AgentRecord, agent_id)
            if not agent:
                raise ValueError(f"Agent '{agent_id}' does not exist. Please register the agent first.")

            # Check duplicate version
            stmt = select(AgentVersionRecord).where(
                AgentVersionRecord.agent_id == agent_id, AgentVersionRecord.version == version
            )
            existing = session.scalars(stmt).first()
            if existing:
                raise ValueError(
                    f"AgentVersion '{agent_id}:{version}' already exists and is immutable. In-place updates are rejected."
                )

            version_rec = AgentVersionRecord(
                id=str(uuid.uuid4()),
                agent_id=agent_id,
                version=version,
                spec_digest=normalized["spec_digest"],
                artifact_ref=normalized["artifact_ref"],
                endpoint=normalized["endpoint"],
                protocol=normalized["protocol"],
                method=normalized["method"],
                request_mapping=normalized["request_mapping"],
                request_schema=normalized["request_schema"],
                response_schema=normalized["response_schema"],
                credential_ref=normalized["credential_ref"],
                timeout_seconds=normalized["timeout_seconds"],
                max_retries=normalized["max_retries"],
                rate_limit_per_minute=normalized["rate_limit_per_minute"],
                max_concurrency=normalized["max_concurrency"],
                trace_propagation=normalized["trace_propagation"],
                is_idempotent=normalized["is_idempotent"],
                environment=normalized["environment"],
                metadata_=metadata,
                is_active=True,
            )
            session.add(version_rec)
            session.commit()
            session.refresh(version_rec)
            return version_rec

    def get_version(self, agent_id: str, version: str) -> AgentVersionRecord | None:
        with self.db_manager.get_session() as session:
            stmt = select(AgentVersionRecord).where(
                AgentVersionRecord.agent_id == agent_id, AgentVersionRecord.version == version
            )
            return session.scalars(stmt).first()

    def list_versions(self, agent_id: str) -> list[AgentVersionRecord]:
        with self.db_manager.get_session() as session:
            stmt = (
                select(AgentVersionRecord)
                .where(AgentVersionRecord.agent_id == agent_id)
                .order_by(AgentVersionRecord.created_at)
            )
            return list(session.scalars(stmt).all())

    def archive_version(self, agent_id: str, version: str) -> AgentVersionRecord:
        with self.db_manager.get_session() as session:
            stmt = select(AgentVersionRecord).where(
                AgentVersionRecord.agent_id == agent_id, AgentVersionRecord.version == version
            )
            ver = session.scalars(stmt).first()
            if not ver:
                raise KeyError(f"Unknown agent/version: {agent_id}:{version}")
            ver.is_active = False
            session.commit()
            session.refresh(ver)
            return ver

    def get(self, agent_id: str, version: str) -> AgentVersionSpec:
        """Backward-compatible fetch returning AgentVersionSpec."""
        ver = self.get_version(agent_id, version)
        if not ver or not ver.is_active:
            raise KeyError(f"Unknown or inactive agent/version: {agent_id}:{version}")

        return AgentVersionSpec(
            agent_id=ver.agent_id,
            version=ver.version,
            endpoint=ver.endpoint,
            method=ver.method,
            timeout_seconds=ver.timeout_seconds,
            max_retries=ver.max_retries,
            rate_limit_per_minute=ver.rate_limit_per_minute,
            request_mapping=dict(ver.request_mapping or {}),
            max_concurrency=ver.max_concurrency,
            credential_ref=ver.credential_ref,
            is_idempotent=ver.is_idempotent,
            spec_digest=ver.spec_digest,
            artifact_ref=ver.artifact_ref,
            id=ver.id,
        )

    def list(self) -> dict[str, Any]:
        """Backward-compatible listing for GET /agents."""
        result: dict[str, Any] = {}
        agents = self.list_agents()
        for ag in agents:
            versions = self.list_versions(ag.id)
            ver_dict = {}
            for v in versions:
                if v.is_active:
                    ver_dict[v.version] = {
                        "endpoint": v.endpoint,
                        "method": v.method,
                        "timeout_seconds": v.timeout_seconds,
                        "max_retries": v.max_retries,
                        "rate_limit_per_minute": v.rate_limit_per_minute,
                        "request_mapping": v.request_mapping,
                    }
            result[ag.id] = {
                "name": ag.name,
                "description": ag.description,
                "versions": ver_dict,
            }
        return result

    def import_yaml(self, path: Path | str) -> dict[str, list[str]]:
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        agents_data = raw.get("agents", {})
        created: list[str] = []
        skipped: list[str] = []

        with self.db_manager.get_session() as session:
            for agent_id, agent_info in agents_data.items():
                agent = session.get(AgentRecord, agent_id)
                if not agent:
                    agent = AgentRecord(
                        id=agent_id,
                        name=agent_info.get("name", agent_id),
                        description=agent_info.get("description"),
                        status="active",
                    )
                    session.add(agent)
                    session.flush()

                versions_data = agent_info.get("versions", {})
                for ver_name, ver_info in versions_data.items():
                    normalized = normalize_and_validate_spec(ver_info)
                    digest = normalized["spec_digest"]

                    stmt = select(AgentVersionRecord).where(
                        AgentVersionRecord.agent_id == agent_id, AgentVersionRecord.version == ver_name
                    )
                    existing = session.scalars(stmt).first()
                    if existing:
                        if existing.spec_digest == digest:
                            skipped.append(f"{agent_id}:{ver_name}")
                            continue
                        else:
                            raise ValueError(
                                f"AgentVersion '{agent_id}:{ver_name}' exists with different spec_digest ({existing.spec_digest} != {digest}). Cannot overwrite."
                            )

                    new_ver = AgentVersionRecord(
                        id=str(uuid.uuid4()),
                        agent_id=agent_id,
                        version=ver_name,
                        spec_digest=digest,
                        artifact_ref=normalized["artifact_ref"],
                        endpoint=normalized["endpoint"],
                        protocol=normalized["protocol"],
                        method=normalized["method"],
                        request_mapping=normalized["request_mapping"],
                        request_schema=normalized["request_schema"],
                        response_schema=normalized["response_schema"],
                        credential_ref=normalized["credential_ref"],
                        timeout_seconds=normalized["timeout_seconds"],
                        max_retries=normalized["max_retries"],
                        rate_limit_per_minute=normalized["rate_limit_per_minute"],
                        max_concurrency=normalized["max_concurrency"],
                        trace_propagation="W3C",
                        is_idempotent=normalized["is_idempotent"],
                        environment=normalized["environment"],
                        metadata_=ver_info.get("metadata"),
                        is_active=True,
                    )
                    session.add(new_ver)
                    created.append(f"{agent_id}:{ver_name}")

            session.commit()

        return {"created": created, "skipped": skipped}



def _resolve_dot_path(obj: Any, path: str) -> Any:
    value = obj
    for part in path.split("."):
        if isinstance(value, dict):
            value = value[part]
        else:
            value = getattr(value, part)
    return value


def map_request(dataset_input: Any, mapping: dict[str, str]) -> dict[str, Any]:
    root = {"input": dataset_input}
    return {target: _resolve_dot_path(root, source) for target, source in mapping.items()}
