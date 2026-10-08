-- Issue #84: evaluation-only recovery.
--
-- An Agent that already answered successfully must be recoverable for
-- re-evaluation without ever calling it again. Two structures make that
-- possible and auditable:
--
-- 1. execution_checkpoints — the recoverable Agent output, written after a
--    successful HTTP 200 and BEFORE evaluation begins. It holds the original
--    output plus a content digest, the dataset input/expected output and the
--    frozen Binding provenance, so a later re-evaluation is locally
--    verifiable and parsable without depending on a Langfuse Trace that may
--    never have synced. It is bounded runtime-recovery data (explicit
--    retention, only the business response — never a second copy of the full
--    Langfuse trace), not a Trace analysis store.
--
-- 2. An independent evaluation lifecycle on experiment_item_executions
--    (evaluation_generation / status / lease) plus evaluation_attempts. The
--    evaluation lifecycle is fully independent of the execution attempt: a
--    re-evaluation never creates an ExecutionAttemptRecord and never touches
--    dispatch_generation, so the Agent invocation count and the non-idempotent
--    execution safety boundary are preserved.

CREATE TABLE execution_checkpoints (
    id VARCHAR(64) PRIMARY KEY,
    item_execution_id VARCHAR(64) NOT NULL REFERENCES experiment_item_executions(id) ON DELETE CASCADE,
    launch_id VARCHAR(64) NOT NULL REFERENCES experiment_launches(id) ON DELETE CASCADE,
    dataset_item_id VARCHAR(128) NOT NULL,
    dispatch_generation INT NOT NULL,
    output_digest VARCHAR(128) NOT NULL,
    agent_output JSON,
    input_payload JSON NOT NULL,
    expected_output JSON,
    binding_provenance JSON NOT NULL,
    manifest_digest VARCHAR(128) NOT NULL,
    final_attempt_id VARCHAR(64),
    trace_id VARCHAR(128),
    observation_id VARCHAR(128),
    langfuse_trace_url VARCHAR(2048),
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_execution_checkpoints_item_generation UNIQUE (item_execution_id, dispatch_generation)
);
CREATE INDEX idx_execution_checkpoints_expiry ON execution_checkpoints(expires_at);

ALTER TABLE experiment_item_executions ADD COLUMN evaluation_generation INT NOT NULL DEFAULT 0;
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_status VARCHAR(32) NOT NULL DEFAULT 'none';
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_lease_owner VARCHAR(128);
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_lease_token VARCHAR(128);
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_lease_expires_at TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_error TEXT;
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_started_at TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_completed_at TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN evaluation_reused_output_digest VARCHAR(128);

CREATE TABLE evaluation_attempts (
    id VARCHAR(64) PRIMARY KEY,
    item_execution_id VARCHAR(64) NOT NULL REFERENCES experiment_item_executions(id) ON DELETE CASCADE,
    launch_id VARCHAR(64) NOT NULL REFERENCES experiment_launches(id) ON DELETE CASCADE,
    evaluation_generation INT NOT NULL,
    status VARCHAR(32) NOT NULL,
    target_bindings JSON NOT NULL,
    reused_output_digest VARCHAR(128) NOT NULL,
    worker_id VARCHAR(128),
    lease_token VARCHAR(128),
    error_type VARCHAR(64),
    error_message TEXT,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    CONSTRAINT uq_evaluation_attempts_item_generation UNIQUE (item_execution_id, evaluation_generation)
);
CREATE INDEX idx_evaluation_attempts_item ON evaluation_attempts(item_execution_id);
