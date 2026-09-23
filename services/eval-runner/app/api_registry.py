from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .models import (
    AgentCreateRequest,
    AgentDeleteResponse,
    AgentResponse,
    AgentSummaryResponse,
    AgentVersionArchiveRequest,
    AgentVersionCreateRequest,
    AgentVersionResponse,
)
from .registry import AgentRegistry

router = APIRouter(prefix="/api/v1", tags=["Agent Registry"])


def get_registry() -> AgentRegistry:
    from .main import registry  # Shared registry singleton initialized in main
    return registry


@router.post(
    "/agents",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new Agent Definition",
)
def create_agent(
    payload: AgentCreateRequest,
    reg: AgentRegistry = Depends(get_registry),
) -> AgentResponse:
    try:
        agent = reg.create_agent(
            agent_id=payload.id,
            name=payload.name,
            description=payload.description,
            owner=payload.owner,
        )
        return AgentResponse.model_validate(agent)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/agents",
    response_model=AgentResponse | list[AgentSummaryResponse],
    summary="Query an Agent by ID (?id=...) or list all agents",
)
def get_or_list_agents(
    id: str | None = Query(default=None, description="Optional Agent ID. If omitted, returns all agents."),
    reg: AgentRegistry = Depends(get_registry),
) -> Any:
    if id:
        agent = reg.get_agent_summary(id)
        if not agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent '{id}' not found")
        return AgentResponse.model_validate(agent)
    else:
        agents = reg.list_agents_summary()
        return [AgentSummaryResponse.model_validate(a) for a in agents]


@router.delete(
    "/agents",
    response_model=AgentDeleteResponse,
    summary="Delete an Agent by ID (?id=...) with optional force cleanup of local evaluation records",
)
def delete_agent(
    id: str = Query(..., description="Agent ID to delete (required)"),
    force: bool = Query(default=False, description="Whether to cascade delete local evaluation records. Langfuse records remain untouched."),
    reg: AgentRegistry = Depends(get_registry),
) -> AgentDeleteResponse:
    try:
        res = reg.delete_agent(agent_id=id, force=force)
        return AgentDeleteResponse.model_validate(res)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/agents/{agent_id}",
    response_model=AgentDeleteResponse,
    include_in_schema=False,
)
def delete_agent_by_path(
    agent_id: str,
    force: bool = Query(default=False),
    reg: AgentRegistry = Depends(get_registry),
) -> AgentDeleteResponse:
    return delete_agent(id=agent_id, force=force, reg=reg)



@router.post(
    "/agent-versions",
    response_model=AgentVersionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new immutable Agent Version specification",
)
def create_agent_version(
    payload: AgentVersionCreateRequest,
    reg: AgentRegistry = Depends(get_registry),
) -> AgentVersionResponse:
    try:
        version = reg.create_version(
            agent_id=payload.agent_id,
            version=payload.version,
            endpoint=payload.endpoint,
            protocol=payload.protocol,
            method=payload.method,
            request_mapping=payload.request_mapping,
            request_schema=payload.request_schema,
            response_schema=payload.response_schema,
            credential_ref=payload.credential_ref,
            timeout_seconds=payload.timeout_seconds,
            max_retries=payload.max_retries,
            rate_limit_per_minute=payload.rate_limit_per_minute,
            max_concurrency=payload.max_concurrency,
            is_idempotent=payload.is_idempotent,
            artifact_ref=payload.artifact_ref,
            environment=payload.environment,
            metadata=payload.metadata,
            trace_propagation=payload.trace_propagation,
        )
        return AgentVersionResponse.model_validate(version)

    except ValueError as exc:
        msg = str(exc)
        if "already exists" in msg:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg) from exc
        elif "does not exist" in msg:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg) from exc
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc


@router.get(
    "/agent-versions",
    response_model=AgentVersionResponse | list[AgentVersionResponse],
    summary="Query Agent Versions by agent_id and optional version (?agent_id=...&version=...)",
)
def get_or_list_agent_versions(
    agent_id: str = Query(..., description="Agent ID (required)"),
    version: str | None = Query(default=None, description="Optional version. If omitted, returns all versions of the agent."),
    reg: AgentRegistry = Depends(get_registry),
) -> Any:
    if version:
        ver = reg.get_version(agent_id, version)
        if not ver:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AgentVersion '{agent_id}:{version}' not found",
            )
        return AgentVersionResponse.model_validate(ver)
    else:
        versions = reg.list_versions(agent_id)
        return [AgentVersionResponse.model_validate(v) for v in versions]


@router.post(
    "/agent-versions/archive",
    response_model=AgentVersionResponse,
    summary="Archive/deactivate an Agent Version (Body: {agent_id, version})",
)
def archive_agent_version(
    payload: AgentVersionArchiveRequest,
    reg: AgentRegistry = Depends(get_registry),
) -> AgentVersionResponse:
    try:
        ver = reg.archive_version(payload.agent_id, payload.version)
        return AgentVersionResponse.model_validate(ver)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
