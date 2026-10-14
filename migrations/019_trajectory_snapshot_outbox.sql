-- 旧任务保持 NULL 与 generation 唯一性；新轨迹任务固定不可变 Snapshot。
ALTER TABLE langfuse_sync_tasks ADD COLUMN result_snapshot_id VARCHAR(64)
    REFERENCES run_result_snapshots(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX uq_sync_legacy_item_gen_type
    ON langfuse_sync_tasks(item_id, dispatch_generation, task_type)
    WHERE result_snapshot_id IS NULL;
CREATE UNIQUE INDEX uq_sync_item_snapshot_type
    ON langfuse_sync_tasks(item_id, result_snapshot_id, task_type)
    WHERE result_snapshot_id IS NOT NULL;
CREATE INDEX idx_sync_result_snapshot ON langfuse_sync_tasks(result_snapshot_id);

-- 已发布 SQL 使用唯一索引；ORM 建表使用同名约束，两种旧入口均保留数据。
ALTER TABLE langfuse_sync_tasks DROP CONSTRAINT IF EXISTS uq_sync_item_gen_type;
DROP INDEX IF EXISTS uq_sync_item_gen_type;
