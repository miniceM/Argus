from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def find_path(configured_path: str | Path, *subpaths: str) -> Path:
    """Safely resolve a path either from configured path, or by searching parent directories."""
    p = Path(configured_path)
    if p.exists():
        return p

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent.joinpath(*subpaths)
        if candidate.exists():
            return candidate

    return p


@dataclass(frozen=True)
class Settings:
    database_url: str | None = os.getenv("DATABASE_URL")
    argus_db_mode: str = os.getenv("ARGUS_DB_MODE", "prod")
    argus_auto_import_yaml: bool = os.getenv("ARGUS_AUTO_IMPORT_YAML", "true").lower() in ("true", "1", "yes")
    langfuse_base_url: str = os.getenv("LANGFUSE_BASE_URL", "http://langfuse-web:3000")
    agent_registry_path: str = os.getenv("AGENT_REGISTRY_PATH", "/app/config/agents.yaml")
    dataset_seed_path: str = os.getenv("DATASET_SEED_PATH", "/app/data/dataset.json")
    migrations_path: str = os.getenv("ARGUS_MIGRATIONS_PATH", "/app/migrations")
    runner_version: str = os.getenv("RUNNER_VERSION", "0.2.0")
    ready_timeout_seconds: int = int(os.getenv("LANGFUSE_READY_TIMEOUT_SECONDS", "120"))
    argus_redis_url: str | None = os.getenv("ARGUS_REDIS_URL")
    argus_worker_enabled: bool = os.getenv("ARGUS_WORKER_ENABLED", "true").lower() in ("true", "1", "yes")
    argus_reconciler_enabled: bool = os.getenv("ARGUS_RECONCILER_ENABLED", "true").lower() in ("true", "1", "yes")
    worker_concurrency: int = int(os.getenv("ARGUS_WORKER_CONCURRENCY", "10"))

    @property
    def environment(self) -> str:
        return os.getenv("ARGUS_ENVIRONMENT", "local")

    @property
    def build_id(self) -> str:
        return os.getenv("ARGUS_BUILD_ID", "dev")


settings = Settings()

