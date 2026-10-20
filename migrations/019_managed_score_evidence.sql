-- 外部评分仍复用 typed results；唯一键防止同一外部 Score 被跨 Item/Launch 重用。
ALTER TABLE evaluation_results ADD COLUMN external_project_id VARCHAR(128);
ALTER TABLE evaluation_results ADD COLUMN external_score_id VARCHAR(128);
CREATE UNIQUE INDEX uq_evaluation_results_external_score ON evaluation_results(external_project_id, external_score_id);

-- 最小只追加诊断审计，不保存另一套质量结果。
CREATE TABLE managed_score_evidence (
    id VARCHAR(64) PRIMARY KEY,
    item_execution_id VARCHAR(64) NOT NULL,
    launch_id VARCHAR(64) NOT NULL,
    dispatch_generation INTEGER NOT NULL,
    score_name VARCHAR(128) NOT NULL,
    score_id VARCHAR(128),
    decision VARCHAR(32) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    evidence JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_execution_id, launch_id) REFERENCES experiment_item_executions(id, launch_id) ON DELETE CASCADE
);
CREATE INDEX ix_managed_score_evidence_item ON managed_score_evidence(item_execution_id, created_at);
