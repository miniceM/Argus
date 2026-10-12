-- 不可变发布策略与门禁结果。门禁引用的 Snapshot 必须保留，不能被级联清理。
CREATE TABLE IF NOT EXISTS release_policies (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    version VARCHAR(64) NOT NULL,
    definition JSON NOT NULL,
    policy_digest VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_release_policy_version UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS release_gates (
    id VARCHAR(64) PRIMARY KEY,
    policy_id VARCHAR(64) NOT NULL REFERENCES release_policies(id) ON DELETE RESTRICT,
    candidate_snapshot_id VARCHAR(64) NOT NULL REFERENCES run_result_snapshots(id) ON DELETE RESTRICT,
    baseline_snapshot_id VARCHAR(64) REFERENCES run_result_snapshots(id) ON DELETE RESTRICT,
    request_digest VARCHAR(128) NOT NULL UNIQUE,
    result JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
