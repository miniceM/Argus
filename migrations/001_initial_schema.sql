-- migrations/001_initial_schema.sql
-- Initial schema for Argus Agent Registry, Experiment Launch and Execution Plane

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(64) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checksum VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id VARCHAR(128) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    owner VARCHAR(128),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_versions (
    id VARCHAR(64) PRIMARY KEY,
    agent_id VARCHAR(128) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    version VARCHAR(64) NOT NULL,
    spec_digest VARCHAR(64) NOT NULL,
    artifact_ref VARCHAR(255),
    endpoint VARCHAR(1024) NOT NULL,
    protocol VARCHAR(32) NOT NULL DEFAULT 'HTTP_JSON',
    method VARCHAR(16) NOT NULL DEFAULT 'POST',
    request_mapping JSON NOT NULL DEFAULT '{}',
    request_schema JSON,
    response_schema JSON,
    credential_ref VARCHAR(255),
    timeout_seconds FLOAT NOT NULL DEFAULT 30.0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 600,
    max_concurrency INTEGER NOT NULL DEFAULT 4,
    trace_propagation VARCHAR(32) NOT NULL DEFAULT 'W3C',
    is_idempotent BOOLEAN NOT NULL DEFAULT FALSE,
    environment VARCHAR(64),
    metadata JSON,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_versions_agent_id_version UNIQUE (agent_id, version)
);

CREATE INDEX IF NOT EXISTS idx_agent_versions_agent_id ON agent_versions(agent_id);

CREATE TABLE IF NOT EXISTS experiment_launches (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    quality_conclusion VARCHAR(32) NOT NULL DEFAULT 'unknown',
    idempotency_key VARCHAR(128) UNIQUE,
    request_payload_digest VARCHAR(64),
    dataset_id VARCHAR(128),
    dataset_name VARCHAR(128) NOT NULL,
    dataset_version VARCHAR(128),
    agent_id VARCHAR(128) NOT NULL,
    agent_version VARCHAR(64) NOT NULL,
    agent_version_id VARCHAR(64) NOT NULL REFERENCES agent_versions(id) ON DELETE RESTRICT,
    manifest JSON NOT NULL,
    langfuse_experiment_id VARCHAR(128),
    langfuse_sync_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    langfuse_sync_error TEXT,
    created_by VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_experiment_launches_agent ON experiment_launches(agent_id, agent_version);
CREATE INDEX IF NOT EXISTS idx_experiment_launches_status ON experiment_launches(status);

CREATE TABLE IF NOT EXISTS experiment_item_executions (
    id VARCHAR(64) PRIMARY KEY,
    launch_id VARCHAR(64) NOT NULL REFERENCES experiment_launches(id) ON DELETE CASCADE,
    dataset_item_id VARCHAR(128) NOT NULL,
    execution_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    eval_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    quality_conclusion VARCHAR(32) NOT NULL DEFAULT 'unknown',
    execution_error TEXT,
    eval_error TEXT,
    trace_id VARCHAR(128),
    observation_id VARCHAR(128),
    final_attempt_id VARCHAR(64),
    scores JSON,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_item_executions_launch_item UNIQUE (launch_id, dataset_item_id)
);

CREATE INDEX IF NOT EXISTS idx_item_executions_launch ON experiment_item_executions(launch_id);

CREATE TABLE IF NOT EXISTS execution_attempts (
    id VARCHAR(64) PRIMARY KEY,
    item_execution_id VARCHAR(64) NOT NULL REFERENCES experiment_item_executions(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'RUNNING',
    http_status INTEGER,
    error_type VARCHAR(64),
    error_message TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    request_ref VARCHAR(255),
    response_ref VARCHAR(255),
    trace_context_received BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_execution_attempts_item_attempt UNIQUE (item_execution_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_execution_attempts_item ON execution_attempts(item_execution_id);
