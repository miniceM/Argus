-- migrations/005_langfuse_outbox.sql
-- Transactional Outbox for Langfuse synchronization and active attempt tracking

ALTER TABLE experiment_item_executions ADD COLUMN active_attempt_id VARCHAR(64);
ALTER TABLE execution_attempts ADD COLUMN dispatch_generation INT NOT NULL DEFAULT 1;
ALTER TABLE execution_attempts ADD COLUMN lease_token VARCHAR(128);

CREATE TABLE langfuse_sync_tasks (
    id VARCHAR(64) PRIMARY KEY,
    launch_id VARCHAR(64) NOT NULL REFERENCES experiment_launches(id) ON DELETE CASCADE,
    item_id VARCHAR(64) NOT NULL REFERENCES experiment_item_executions(id) ON DELETE CASCADE,
    dataset_item_id VARCHAR(128) NOT NULL,
    dataset_version VARCHAR(64),
    dispatch_generation INT NOT NULL,
    task_type VARCHAR(32) NOT NULL DEFAULT 'FULL_EVAL_SYNC',
    trace_id VARCHAR(128) NOT NULL,
    observation_id VARCHAR(128),
    dataset_run_name VARCHAR(255) NOT NULL,
    scores_payload JSON NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    owner_id VARCHAR(128),
    claim_token VARCHAR(64),
    lease_expires_at TIMESTAMP,
    attempts INT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_sync_item_gen_type ON langfuse_sync_tasks(item_id, dispatch_generation, task_type);
CREATE INDEX idx_sync_tasks_status_retry ON langfuse_sync_tasks(status, next_retry_at, lease_expires_at);
