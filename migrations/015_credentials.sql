-- Credential 为稳定资源；Secret 历史修订只保存密文。
CREATE TABLE IF NOT EXISTS credentials (
 id VARCHAR(64) PRIMARY KEY, name VARCHAR(128) NOT NULL, environment VARCHAR(64) NOT NULL,
 provider VARCHAR(32) NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
 active_version INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS credential_secrets (
 credential_id VARCHAR(64) NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
 version INTEGER NOT NULL, envelope JSON, provider_ref VARCHAR(255),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(credential_id, version)
);
CREATE TABLE IF NOT EXISTS credential_audit (
 id VARCHAR(64) PRIMARY KEY, credential_id VARCHAR(64) NOT NULL, action VARCHAR(32) NOT NULL,
 actor VARCHAR(128) NOT NULL, version INTEGER, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE agent_versions ADD COLUMN credential_id VARCHAR(64) REFERENCES credentials(id) ON DELETE RESTRICT;
ALTER TABLE execution_attempts ADD COLUMN credential_id VARCHAR(64);
ALTER TABLE execution_attempts ADD COLUMN credential_version INTEGER;
ALTER TABLE execution_attempts ADD COLUMN credential_provider VARCHAR(32);
