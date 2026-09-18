from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    langfuse_base_url: str = os.getenv("LANGFUSE_BASE_URL", "http://langfuse-web:3000")
    agent_registry_path: str = os.getenv("AGENT_REGISTRY_PATH", "/app/config/agents.yaml")
    dataset_seed_path: str = os.getenv("DATASET_SEED_PATH", "/app/data/dataset.json")
    runner_version: str = os.getenv("RUNNER_VERSION", "0.1.0")
    ready_timeout_seconds: int = int(os.getenv("LANGFUSE_READY_TIMEOUT_SECONDS", "120"))


settings = Settings()
