"""运行时凭据 Provider：Registry 只保存引用，Worker 按身份读取。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx


class SecretProvider(Protocol):
    def validate(self, reference: str) -> None: ...
    def resolve(self, reference: str) -> str: ...


class CredentialUnavailable(ValueError):
    def __init__(self):
        # 不把远端响应、JWT、Token 或底层异常写入 Attempt / Trace。
        super().__init__("CREDENTIAL_UNAVAILABLE: configured credential could not be resolved")


def _token(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CredentialUnavailable()
    if not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CredentialUnavailable()
    return value


class EnvironmentSecretProvider:
    def __init__(self, allowed: set[str]):
        self.allowed = allowed

    def validate(self, reference: str) -> None:
        default = "development" if os.getenv("ARGUS_DB_MODE") == "test" else "production"
        if os.getenv("ARGUS_SECRET_MODE", default) != "development":
            raise ValueError("env:// credentials require explicit development mode (ARGUS_SECRET_MODE=development)")
        name = reference.removeprefix("env://")
        if name not in self.allowed:
            raise ValueError(f"Environment variable '{name}' is not in allowed credential whitelist")

    def resolve(self, reference: str) -> str:
        self.validate(reference)
        return _token(os.getenv(reference.removeprefix("env://")))


class VaultKubernetesProvider:
    """Vault KV v2，通过 Kubernetes Workload Identity 获取短期读取凭据。"""

    _transport: httpx.BaseTransport | None = None

    def __init__(self):
        self.address = os.getenv("ARGUS_VAULT_ADDR", "").rstrip("/")
        self.role = os.getenv("ARGUS_VAULT_ROLE", "")
        self.mount = os.getenv("ARGUS_VAULT_KV_MOUNT", "secret")
        self.auth_mount = os.getenv("ARGUS_VAULT_AUTH_MOUNT", "kubernetes")
        self.jwt_path = os.getenv(
            "ARGUS_VAULT_JWT_PATH", "/var/run/secrets/kubernetes.io/serviceaccount/token"
        )
        self.namespace = os.getenv("ARGUS_VAULT_NAMESPACE")

    def validate(self, reference: str) -> None:
        if not self.address or not self.role:
            raise ValueError("Credential provider 'vault://' is not supported without Vault workload configuration")
        parts = urlsplit(reference)
        if (
            parts.scheme != "vault" or parts.netloc != self.mount
            or parts.query or not parts.fragment or not parts.path.startswith("/")
            or any(not re.fullmatch(r"[A-Za-z0-9_-]+", segment) for segment in parts.path[1:].split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_-]+", parts.fragment)
        ):
            raise ValueError("Invalid vault:// reference; expected configured-mount/path#field")
        address = urlsplit(self.address)
        if (
            address.scheme != "https" or not address.hostname or address.username or address.password
            or address.query or address.fragment or address.path not in ("", "/")
            or any(char.isspace() or char == "\\" for char in self.address)
        ):
            raise ValueError("ARGUS_VAULT_ADDR must be an HTTPS origin without credentials, query or path")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.mount) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.auth_mount):
            raise ValueError("Invalid Vault mount configuration")

    def resolve(self, reference: str) -> str:
        self.validate(reference)
        parts = urlsplit(reference)
        try:
            jwt = _token(Path(self.jwt_path).read_text().strip())
            headers = {"X-Vault-Namespace": self.namespace} if self.namespace else {}
            # TLS 验证保持开启，不跟随重定向，不自动重试，不缓存 Agent Secret。
            with httpx.Client(timeout=5, follow_redirects=False, transport=self._transport) as client:
                login = client.post(
                    f"{self.address}/v1/auth/{self.auth_mount}/login",
                    json={"role": self.role, "jwt": jwt}, headers=headers,
                )
                login.raise_for_status()
                vault_token = _token(login.json()["auth"]["client_token"])
                response = client.get(
                    f"{self.address}/v1/{self.mount}/data{parts.path}",
                    headers={**headers, "X-Vault-Token": vault_token},
                )
                response.raise_for_status()
                return _token(response.json()["data"]["data"][parts.fragment])
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
            raise CredentialUnavailable() from None


def provider_for(reference: str, allowed_envs: set[str]) -> SecretProvider:
    if reference.startswith("env://"):
        return EnvironmentSecretProvider(allowed_envs)
    if reference.startswith("vault://"):
        return VaultKubernetesProvider()
    if reference.startswith("k8s-secret://"):
        raise ValueError("Credential provider 'k8s-secret://' is not supported for resolution in current version")
    raise ValueError("Unsupported credential reference scheme. Expected env:// or vault://")
