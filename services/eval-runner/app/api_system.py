from __future__ import annotations

from fastapi import APIRouter

from .config import settings
from .models import SystemInfoResponse

router = APIRouter(prefix="/api/v1", tags=["System"])


@router.get(
    "/system/info",
    response_model=SystemInfoResponse,
    summary="Get Argus Control Plane runtime build and environment information",
)
def get_system_info() -> SystemInfoResponse:
    return SystemInfoResponse(
        service="argus-control-plane",
        version=settings.runner_version,
        build_id=settings.build_id,
        environment=settings.environment,
    )
