-- Issue #82: typed EvaluationResult is the source of truth for every selected
-- frozen Binding. `scores` on experiment_item_executions remains only as a
-- restricted numeric projection for backwards compatibility.
CREATE TABLE evaluation_results (
    id VARCHAR(64) PRIMARY KEY,
    item_execution_id VARCHAR(64) NOT NULL REFERENCES experiment_item_executions(id) ON DELETE CASCADE,
    launch_id VARCHAR(64) NOT NULL REFERENCES experiment_launches(id) ON DELETE CASCADE,
    evaluator_id VARCHAR(128) NOT NULL,
    evaluator_version VARCHAR(64),
    result_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    value JSON,
    normalized_value DOUBLE PRECISION,
    comment TEXT,
    evidence JSON,
    duration_ms DOUBLE PRECISION,
    error_code VARCHAR(64),
    error_message TEXT,
    binding_id VARCHAR(128),
    definition_digest VARCHAR(128),
    executor_type VARCHAR(64),
    manifest_schema_version VARCHAR(16),
    contract_status VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_evaluation_results_item_evaluator UNIQUE (item_execution_id, evaluator_id)
);
CREATE INDEX idx_evaluation_results_launch ON evaluation_results(launch_id, evaluator_id);
