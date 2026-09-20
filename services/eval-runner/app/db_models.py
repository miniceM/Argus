from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SchemaMigrationRecord(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)


class AgentRecord(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    versions: Mapped[list[AgentVersionRecord]] = relationship(
        "AgentVersionRecord", back_populates="agent", cascade="all, delete-orphan"
    )


class AgentVersionRecord(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_id_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(1024), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), default="HTTP_JSON", nullable=False)
    method: Mapped[str] = mapped_column(String(16), default="POST", nullable=False)
    request_mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    request_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    trace_propagation: Mapped[str] = mapped_column(String(32), default="W3C", nullable=False)
    is_idempotent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    agent: Mapped[AgentRecord] = relationship("AgentRecord", back_populates="versions")
    launches: Mapped[list[ExperimentLaunchRecord]] = relationship(
        "ExperimentLaunchRecord", back_populates="agent_version_rel"
    )


class ExperimentLaunchRecord(Base):
    __tablename__ = "experiment_launches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    quality_conclusion: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    request_payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    langfuse_experiment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    langfuse_sync_status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    langfuse_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent_version_rel: Mapped[AgentVersionRecord] = relationship("AgentVersionRecord", back_populates="launches")
    item_executions: Mapped[list[ExperimentItemExecutionRecord]] = relationship(
        "ExperimentItemExecutionRecord", back_populates="launch", cascade="all, delete-orphan"
    )


class ExperimentItemExecutionRecord(Base):
    __tablename__ = "experiment_item_executions"
    __table_args__ = (UniqueConstraint("launch_id", "dataset_item_id", name="uq_item_executions_launch_item"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    launch_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("experiment_launches.id", ondelete="CASCADE"), nullable=False
    )
    dataset_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    eval_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    quality_conclusion: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    eval_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    observation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    final_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    launch: Mapped[ExperimentLaunchRecord] = relationship("ExperimentLaunchRecord", back_populates="item_executions")
    attempts: Mapped[list[ExecutionAttemptRecord]] = relationship(
        "ExecutionAttemptRecord", back_populates="item_execution", cascade="all, delete-orphan"
    )


class ExecutionAttemptRecord(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (UniqueConstraint("item_execution_id", "attempt_no", name="uq_execution_attempts_item_attempt"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_execution_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("experiment_item_executions.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    response_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trace_context_received: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    item_execution: Mapped[ExperimentItemExecutionRecord] = relationship(
        "ExperimentItemExecutionRecord", back_populates="attempts"
    )
