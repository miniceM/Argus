-- 数据回填 v1：comparison.environment > 冻结 agent.environment > production。
-- MigrationRunner 在同一事务中回填；不改 Manifest、Snapshot、digest 或 Binding。
ALTER TABLE experiment_launches ADD COLUMN environment VARCHAR(64) NOT NULL DEFAULT 'production';
CREATE INDEX idx_experiment_launches_agent_environment ON experiment_launches(agent_id, environment);
