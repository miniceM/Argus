"""冻结 Langfuse 项目路由；仅引用现有 Credential，绝不回落到其他项目。"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import settings, validate_langfuse_dashboard_url
from .langfuse_scores_client import ScoresReadError, ScoresV3Client
from .secret_providers import CredentialUnavailable, _token


class LangfuseConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(max_length=1024)
    project_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    credential_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("base_url")
    @classmethod
    def origin_only(cls, value):
        validated = validate_langfuse_dashboard_url(value)
        if not validated or value != value.strip() or urlsplit(validated).path not in ("", "/"):
            raise ValueError("Langfuse base URL must be an HTTP(S) origin without credentials")
        return validated.rstrip("/")


def is_bound(manifest):
    connection = (manifest or {}).get("langfuse_connection")
    # 字段存在但损坏时同样走严格解析，不能当成未配置而回落全局。
    return connection is not None and (not isinstance(connection, dict) or connection.get("source") != "GLOBAL_ENV")


def manifest_for_launch(manager, launch_id):
    from .db_models import ExperimentLaunchRecord

    with manager.get_session() as session:
        launch = session.get(ExperimentLaunchRecord, launch_id)
        if launch is None:
            raise ScoresReadError("ITEM_NOT_FOUND")
        return launch.manifest


@dataclass(frozen=True)
class ResolvedLangfuseConnection:
    base_url: str
    project_id: str
    credential_id: str
    credential_version: int
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def projection(self):
        return {"source": "BOUND", "base_url": self.base_url, "project_id": self.project_id,
                "credential_id": self.credential_id, "credential_version": self.credential_version}

    def evidence(self):
        return {"credential_id": self.credential_id, "version": self.credential_version,
                "provider": "managed", "header_name": "Langfuse-Authorization"}


def resolve_connection(manager, binding, environment, *, operation="DATASET", actor="system"):
    from .credentials import CredentialService
    from .db_models import CredentialRecord

    try:
        connection = LangfuseConnection.model_validate({key: binding[key] for key in LangfuseConnection.model_fields})
        resolved = CredentialService(manager).resolve(connection.credential_id, "basic_auth", connection.base_url, environment)
        public, secret = base64.b64decode(resolved.token, validate=True).decode().split(":", 1)
        public, secret = _token(public), _token(secret)
        # 记录本次解析修订；后续轮换不改写 Manifest 或历史 Attempt。
        with manager.get_session() as session:
            record = session.get(CredentialRecord, connection.credential_id)
            CredentialService.audit(session, record, "RESOLVE_LANGFUSE_" + operation, actor, version=resolved.version)
        return ResolvedLangfuseConnection(connection.base_url, connection.project_id, connection.credential_id,
                                         resolved.version, public, secret)
    except Exception:
        raise CredentialUnavailable() from None


def resolve_manifest_connection(manager, manifest, *, operation="PREFLIGHT", actor="system"):
    if not is_bound(manifest):
        return None
    return resolve_connection(manager, manifest["langfuse_connection"],
                              (manifest.get("comparison") or {}).get("environment")
                              or (manifest.get("agent") or {}).get("environment") or "production",
                              operation=operation, actor=actor)


def global_projection():
    # 历史 Manifest 不回填；新全局 Launch 明确记录来源而不持久化环境密钥。
    return {"source": "GLOBAL_ENV", "base_url": settings.langfuse_base_url,
            "project_id": os.getenv("LANGFUSE_PROJECT_ID"), "credential_id": None, "credential_version": None}


def scores_client_for_manifest(manager, manifest):
    try:
        connection = resolve_manifest_connection(manager, manifest, operation="SCORES")
    except CredentialUnavailable:
        raise ScoresReadError("CREDENTIAL_UNAVAILABLE") from None
    if connection is None:
        return ScoresV3Client.from_env()
    verify_connection(connection)
    return ScoresV3Client(connection.base_url, connection.public_key, connection.secret_key)


class BoundLangfuseClient:
    """仅用独立 REST 客户端，不注册 SDK/TracerProvider 全局状态。

    每次操作关闭连接，无项目缓存、后台线程或 exporter。OTLP 复用有界
    Outbox（已有 16 个远程调用槽位），不按绑定数量累积进程资源。
    """
    def __init__(self, connection):
        from langfuse.api.client import LangfuseAPI

        self.connection = connection
        self.http = httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False)
        self.api = LangfuseAPI(base_url=connection.base_url, username=connection.public_key,
                               password=connection.secret_key, httpx_client=self.http, follow_redirects=False)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def verify_project(self):
        try:
            projects = self.api.projects.get(request_options={"max_retries": 0})
            if [project.id for project in projects.data] != [self.connection.project_id]:
                raise ScoresReadError("MANAGED_PROJECT_MISMATCH")
        except ScoresReadError:
            raise
        except Exception as exc:
            code = {401: "AUTHENTICATION_FAILED", 403: "AUTHORIZATION_FAILED"}.get(
                getattr(exc, "status_code", None), "UPSTREAM_UNAVAILABLE")
            raise ScoresReadError(code) from None

    def get_dataset(self, name, *, version):
        try:
            options = {"max_retries": 0}
            dataset = self.api.datasets.get(dataset_name=quote(name, safe=""), request_options=options)
            items = []
            for page in range(1, 1001):
                result = self.api.dataset_items.list(dataset_name=name, page=page, limit=100,
                                                     version=version, request_options=options)
                items.extend(result.data)
                if result.meta.total_pages <= page:
                    return SimpleNamespace(id=dataset.id, items=items)
            raise ScoresReadError("PAGINATION_LIMIT")
        except ScoresReadError:
            raise
        except Exception as exc:
            code = {401: "AUTHENTICATION_FAILED", 403: "AUTHORIZATION_FAILED"}.get(
                getattr(exc, "status_code", None), "DATASET_UNAVAILABLE")
            raise ScoresReadError(code) from None


def verify_connection(connection):
    with BoundLangfuseClient(connection) as client:
        client.verify_project()
