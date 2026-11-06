-- HTTP 配置属于不可变版本；普通字段只保存引用，不存 Secret。
ALTER TABLE credentials ADD COLUMN type VARCHAR(32) NOT NULL DEFAULT 'bearer_token';
ALTER TABLE credentials ADD COLUMN allowed_origins JSON NOT NULL DEFAULT '[]';
ALTER TABLE agent_versions ADD COLUMN http_auth JSON;
ALTER TABLE agent_versions ADD COLUMN http_headers JSON NOT NULL DEFAULT '[]';
ALTER TABLE agent_versions ADD COLUMN response_mapping JSON;
CREATE TABLE agent_version_credentials (
 agent_version_id VARCHAR(64) NOT NULL REFERENCES agent_versions(id) ON DELETE CASCADE,
 credential_id VARCHAR(64) NOT NULL REFERENCES credentials(id) ON DELETE RESTRICT,
 PRIMARY KEY(agent_version_id, credential_id)
);
CREATE INDEX ix_agent_version_credentials_credential ON agent_version_credentials(credential_id);
ALTER TABLE execution_attempts ADD COLUMN credential_usage JSON;
