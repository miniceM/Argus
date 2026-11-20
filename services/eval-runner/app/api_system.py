from __future__ import annotations

from fastapi import APIRouter

from .config import settings
from .models import SystemInfoResponse
from .secret_providers import is_dev_admin_bypass, is_dev_keyring_fallback

router = APIRouter(prefix="/api/v1", tags=["System"])


@router.get(
    "/system/info",
    response_model=SystemInfoResponse,
    summary="Get Argus Control Plane runtime build and environment information",
)
def get_system_info() -> SystemInfoResponse:
    admin_bypass = is_dev_admin_bypass()
    keyring_fallback = is_dev_keyring_fallback()
    return SystemInfoResponse(
        service="argus-control-plane",
        version=settings.runner_version,
        build_id=settings.build_id,
        environment=settings.environment,
        langfuse_dashboard_url=settings.argus_langfuse_dashboard_url,
        credential_admin_required=not admin_bypass,
        secret_dev_mode=admin_bypass or keyring_fallback,
    )
