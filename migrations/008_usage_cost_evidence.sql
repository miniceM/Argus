-- Explicit per-version response mapping and per-attempt frozen usage/cost evidence.
ALTER TABLE agent_versions ADD COLUMN usage_cost_mapping JSON;
ALTER TABLE execution_attempts ADD COLUMN usage_cost JSON;
