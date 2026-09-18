from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExperimentRequest(BaseModel):
    agent_id: str = "banking-agent"
    agent_version: str
    dataset_name: str = "banking-agent-regression"
    experiment_name: str | None = None
    max_concurrency: int = Field(default=4, ge=1, le=50)


class BootstrapResult(BaseModel):
    dataset_name: str
    dataset_id: str | None = None
    items_upserted: int


class ExperimentResult(BaseModel):
    launch_id: str
    agent_id: str
    agent_version: str
    experiment_name: str
    dataset_run_url: str | None = None
    result: dict[str, Any]
