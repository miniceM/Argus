from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str | None = os.getenv("DATABASE_URL")
    argus_db_mode: str = os.getenv("ARGUS_DB_MODE", "prod")
    argus_auto_import_yaml: bool = os.getenv("ARGUS_AUTO_IMPORT_YAML", "true").lower() in ("true", "1", "yes")
    langfuse_base_url: str = os.getenv("LANGFUSE_BASE_URL", "http://langfuse-web:3000")
    agent_registry_path: str = os.getenv("AGENT_REGISTRY_PATH", "/app/config/agents.yaml")
    dataset_seed_path: str = os.getenv("DATASET_SEED_PATH", "/app/data/dataset.json")
    migrations_path: str = os.getenv("ARGUS_MIGRATIONS_PATH", "/app/migrations")
    runner_version: str = os.getenv("RUNNER_VERSION", "0.1.0")
    ready_timeout_seconds: int = int(os.getenv("LANGFUSE_READY_TIMEOUT_SECONDS", "120"))


settings = Settings()
