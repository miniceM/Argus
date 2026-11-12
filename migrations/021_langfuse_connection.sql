-- 项目绑定属于不可变 AgentVersion；凭据引用复用 agent_version_credentials FK。
ALTER TABLE agent_versions ADD COLUMN langfuse_connection JSON;
