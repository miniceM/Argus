-- 发布策略显式归属 Agent；无 Gate 引用的策略随 Agent 清理。
-- 013 已发布，不修改原迁移；回填既有 JSON 里的 Agent 关系。
ALTER TABLE release_policies ADD COLUMN agent_id VARCHAR(128) REFERENCES agents(id) ON DELETE CASCADE;
DELETE FROM release_policies WHERE (definition ->> 'agent_id') NOT IN (SELECT id FROM agents);
UPDATE release_policies SET agent_id = definition ->> 'agent_id';
