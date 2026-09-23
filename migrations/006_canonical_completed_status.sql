-- migrations/006_canonical_completed_status.sql
-- Canonicalize legacy 'SUCCEEDED' status to 'COMPLETED' for experiment launches

UPDATE experiment_launches SET status = 'COMPLETED' WHERE status = 'SUCCEEDED';
