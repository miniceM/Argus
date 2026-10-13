-- S4.1: 最小轨迹证据存储。旧行为空，运行态接入由后续切片负责。
ALTER TABLE agent_versions ADD COLUMN trajectory_source_profile_id VARCHAR(64);
ALTER TABLE execution_checkpoints ADD COLUMN trajectory_acquisition_json JSON;

-- 复合外键同时约束归属，不能把其它 Launch/Item 的有效 ID 拼成证据。
CREATE UNIQUE INDEX uq_item_executions_id_launch ON experiment_item_executions(id, launch_id);
CREATE UNIQUE INDEX uq_execution_attempts_id_item ON execution_attempts(id, item_execution_id);

CREATE TABLE trajectory_evidence_artifacts (
    id VARCHAR(64) PRIMARY KEY,
    launch_id VARCHAR(64) NOT NULL REFERENCES experiment_launches(id) ON DELETE RESTRICT,
    item_execution_id VARCHAR(64) NOT NULL,
    attempt_id VARCHAR(64) NOT NULL,
    trajectory_contract_digest VARCHAR(128) NOT NULL,
    trajectory_digest VARCHAR(128) NOT NULL,
    schema_version VARCHAR(16) NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_trajectory_artifact_item_launch
        FOREIGN KEY (item_execution_id, launch_id)
        REFERENCES experiment_item_executions(id, launch_id) ON DELETE RESTRICT,
    CONSTRAINT fk_trajectory_artifact_attempt_item
        FOREIGN KEY (attempt_id, item_execution_id)
        REFERENCES execution_attempts(id, item_execution_id) ON DELETE RESTRICT,
    CONSTRAINT uq_trajectory_artifact_identity
        UNIQUE (item_execution_id, attempt_id, trajectory_contract_digest, trajectory_digest),
    CONSTRAINT ck_trajectory_artifact_payload_bytes
        CHECK (octet_length(payload_json) BETWEEN 1 AND 2097152)
);

CREATE INDEX idx_trajectory_artifact_launch ON trajectory_evidence_artifacts(launch_id);
CREATE INDEX idx_trajectory_artifact_attempt_item ON trajectory_evidence_artifacts(attempt_id, item_execution_id);
