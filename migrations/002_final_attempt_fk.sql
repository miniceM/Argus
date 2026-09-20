-- migrations/002_final_attempt_fk.sql
-- Add foreign key constraint from experiment_item_executions.final_attempt_id to execution_attempts.id

ALTER TABLE experiment_item_executions
    ADD CONSTRAINT fk_item_executions_final_attempt
    FOREIGN KEY (final_attempt_id)
    REFERENCES execution_attempts(id)
    ON DELETE SET NULL;
