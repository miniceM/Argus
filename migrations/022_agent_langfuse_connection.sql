-- Agent 统一管理当前项目；历史凭据引用保留至 Agent 及其冻结证据被清理。
ALTER TABLE agents ADD COLUMN langfuse_connection JSON;
CREATE TABLE agent_langfuse_credentials (
    agent_id VARCHAR(128) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    credential_id VARCHAR(64) NOT NULL REFERENCES credentials(id) ON DELETE RESTRICT,
    PRIMARY KEY (agent_id, credential_id)
);
