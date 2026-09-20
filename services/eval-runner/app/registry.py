from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from .config import find_path
from .db import DatabaseManager
from .db_models import AgentRecord, AgentVersionRecord


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

    def list_agents(self) -> list[AgentRecord]:
        with self.db_manager.get_session() as session:
            stmt = select(AgentRecord).order_by(AgentRecord.created_at)
            return list(session.scalars(stmt).all())

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
