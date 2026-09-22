-- migrations/004_s2_reliable_runtime.sql
-- S2 Reliable Runtime schema additions for experiment launches, item executions, and execution attempts

ALTER TABLE experiment_launches ADD COLUMN cancel_requested_at TIMESTAMP;
ALTER TABLE experiment_launches ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE experiment_launches ADD COLUMN status_reason TEXT;

ALTER TABLE experiment_item_executions ADD COLUMN queued_at TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN available_at TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN lease_owner VARCHAR(128);
ALTER TABLE experiment_item_executions ADD COLUMN lease_token VARCHAR(128);
ALTER TABLE experiment_item_executions ADD COLUMN lease_expires_at TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN dispatch_generation INT NOT NULL DEFAULT 1;
ALTER TABLE experiment_item_executions ADD COLUMN created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE experiment_item_executions ADD COLUMN updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE execution_attempts ADD COLUMN worker_id VARCHAR(128);
ALTER TABLE execution_attempts ADD COLUMN request_phase VARCHAR(64) NOT NULL DEFAULT 'PREPARED';

CREATE INDEX idx_item_launch_status ON experiment_item_executions(launch_id, execution_status);
CREATE INDEX idx_item_status_available ON experiment_item_executions(execution_status, available_at);
CREATE INDEX idx_item_lease_expires ON experiment_item_executions(lease_expires_at);
