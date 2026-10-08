"""凭据管理：响应只含元数据，写入需独立管理授权。"""
from __future__ import annotations

import hashlib
import hmac
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .baselines import normalize_environment
from .credentials import CredentialService, protected_file
from .db_models import CredentialRecord
from .secret_providers import CredentialUnavailable, _token


class SecretSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "Invalid credential request") from None
        return safe


router = APIRouter(prefix="/api/v1/credentials", tags=["Credentials"], route_class=SecretSafeRoute)


def service():
    from .main import db_manager
    return CredentialService(db_manager)


def authorize(authorization: str | None = Header(default=None)) -> str:
    try:
        expected = _token(protected_file("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE").strip())
    except CredentialUnavailable:
        raise HTTPException(503, "Credential management authorization is not configured") from None
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Credential management authorization required")
    if not hmac.compare_digest(authorization[7:].encode(), expected.encode()):
        raise HTTPException(403, "Credential management authorization denied")
    return "credential-admin:" + hashlib.sha256(expected.encode()).hexdigest()[:12]


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    type: Literal["bearer_token"] = "bearer_token"
    environment: str = "production"
    provider: Literal["managed", "vault"] = "managed"
    secret: SecretStr | None = None
    provider_ref: str | None = Field(default=None, max_length=255, description="Vault 管理员登记的内部映射，普通 API 不返回")

    @field_validator("environment")
    @classmethod
    def environment_scope(cls, value):
        return normalize_environment(value)

    @model_validator(mode="after")
    def check_provider(self):
        if self.provider == "managed" and (self.secret is None or self.provider_ref is not None):
            raise ValueError("Managed secret required")
        if self.provider == "vault" and (not self.provider_ref or self.secret is not None):
            raise ValueError("Vault reference required")
        return self


class CredentialRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: SecretStr


class CredentialDisable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_name: str
    force: bool = Field(default=False, strict=True)


class CredentialResponse(BaseModel):
    id: str
    name: str
    type: Literal["bearer_token"] = "bearer_token"
    environment: str
    provider: Literal["managed", "vault"]
    enabled: bool
    version: int


class CredentialUsageVersion(BaseModel):
    agent_id: str
    version: str
    is_active: bool


class CredentialUsage(BaseModel):
    credential_id: str
    versions: list[CredentialUsageVersion]


@router.get("", response_model=list[CredentialResponse])
def list_credentials(svc=Depends(service)):
    with svc.manager.get_session() as session:
        return [svc.metadata(record) for record in session.scalars(select(CredentialRecord).order_by(CredentialRecord.created_at)).all()]


@router.post("", response_model=CredentialResponse, status_code=201)
def create_credential(payload: CredentialCreate, actor=Depends(authorize), svc=Depends(service)):
    secret = payload.secret.get_secret_value() if payload.secret is not None else None
    if secret is not None:
        try:
            _token(secret)
        except CredentialUnavailable:
            raise HTTPException(422, "Invalid credential secret") from None
    try:
        return svc.create(payload.name, payload.environment, payload.provider, secret, payload.provider_ref, actor)
    except CredentialUnavailable:
        raise HTTPException(503, "Credential provider is unavailable or not configured") from None
    except ValueError:
        raise HTTPException(422, "Invalid credential provider configuration") from None


@router.post("/rewrap")
def rewrap_credentials(actor=Depends(authorize), svc=Depends(service)):
    try:
        return svc.rewrap(actor)
    except CredentialUnavailable:
        raise HTTPException(503, "Credential keyring is unavailable") from None


@router.get("/{credential_id}/usage", response_model=CredentialUsage)
def credential_usage(credential_id: str, svc=Depends(service)):
    return svc.usage(credential_id)


@router.post("/{credential_id}/rotate", response_model=CredentialResponse)
def rotate_credential(credential_id: str, payload: CredentialRotate, actor=Depends(authorize), svc=Depends(service)):
    try:
        _token(payload.secret.get_secret_value())
    except CredentialUnavailable:
        raise HTTPException(422, "Invalid credential secret") from None
    try:
        return svc.rotate(credential_id, payload.secret.get_secret_value(), actor)
    except CredentialUnavailable:
        raise HTTPException(503, "Credential keyring is unavailable") from None
    except (ValueError, IntegrityError):
        raise HTTPException(409, "Credential cannot be rotated or was changed concurrently") from None


@router.post("/{credential_id}/disable", response_model=CredentialResponse)
def disable_credential(credential_id: str, payload: CredentialDisable, actor=Depends(authorize), svc=Depends(service)):
    with svc.manager.get_session() as session:
        record = session.scalar(select(CredentialRecord).where(CredentialRecord.id == credential_id).with_for_update())
        if record is None:
            raise HTTPException(404, "Credential not found")
        if record.name != payload.confirm_name:
            raise HTTPException(400, "Credential name confirmation mismatch")
        if svc.usage(record.id)["versions"] and not payload.force:
            raise HTTPException(409, "Credential is referenced; confirm forced disable")
        record.enabled = False
        svc.audit(session, record, "DISABLE", actor)
        session.commit()
        return svc.metadata(record)


@router.delete("/{credential_id}")
def delete_credential(credential_id: str, confirm_name: str = Query(...), actor=Depends(authorize), svc=Depends(service)):
    try:
        with svc.manager.get_session() as session:
            record = session.scalar(select(CredentialRecord).where(CredentialRecord.id == credential_id).with_for_update())
            if record is None:
                raise HTTPException(404, "Credential not found")
            if record.name != confirm_name:
                raise HTTPException(400, "Credential name confirmation mismatch")
            if svc.usage(record.id)["versions"]:
                raise HTTPException(409, "Credential is referenced by immutable AgentVersions")
            svc.audit(session, record, "DELETE", actor)
            session.delete(record)
            session.commit()
            return {"id": credential_id, "deleted": True}
    except IntegrityError:
        raise HTTPException(409, "Credential is referenced or changed concurrently") from None
