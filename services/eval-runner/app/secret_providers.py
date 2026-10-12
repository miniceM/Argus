"""凭据失败脱敏、Token 校验与显式开发环境引用。"""

from __future__ import annotations

import os


class CredentialUnavailable(ValueError):
    def __init__(self):
        # 不把远端响应、JWT、Token 或底层异常写入 Attempt / Trace。
        super().__init__("CREDENTIAL_UNAVAILABLE: configured credential could not be resolved")


def _token(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CredentialUnavailable()
    if not value.isascii() or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
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


def provider_for(reference: str, allowed_envs: set[str]) -> EnvironmentSecretProvider:
    if reference.startswith("env://"):
        return EnvironmentSecretProvider(allowed_envs)
    if reference.startswith(("vault://", "k8s-secret://")):
        scheme = reference.split(":", 1)[0]
        raise ValueError(f"Credential provider '{scheme}://' is not supported for resolution in current version")
    raise ValueError("Unsupported credential reference scheme. Expected env://")
