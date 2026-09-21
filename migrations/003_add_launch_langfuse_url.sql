-- migrations/003_add_launch_langfuse_url.sql
-- Add langfuse_experiment_url to experiment_launches table

ALTER TABLE experiment_launches
    ADD COLUMN langfuse_experiment_url VARCHAR(1024);
