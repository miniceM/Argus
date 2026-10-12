-- 补齐 014 的归属约束，不改写既有迁移或校验和。
UPDATE release_policies SET agent_id = definition ->> 'agent_id'
WHERE agent_id IS NULL AND (definition ->> 'agent_id') IN (SELECT id FROM agents);
DELETE FROM release_policies WHERE agent_id IS NULL;
ALTER TABLE release_policies ALTER COLUMN agent_id SET NOT NULL;
