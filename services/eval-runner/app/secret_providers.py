"""凭据失败脱敏、Token 校验与显式开发环境引用。"""

from __future__ import annotations

import os

CREDENTIAL_FAILURE_REASONS = frozenset(
    {
        "CREDENTIAL_NOT_FOUND",
        "CREDENTIAL_DISABLED",
        "CREDENTIAL_TYPE_MISMATCH",
        "ENVIRONMENT_MISMATCH",
        "ORIGIN_NOT_ALLOWED",
        "ALLOWED_ORIGINS_REQUIRED",
        "SECRET_REVISION_MISSING",
        "UNSUPPORTED_PROVIDER",
        "KEYRING_MISSING",
        "KEYRING_INSECURE_PERMISSIONS",
        "KEYRING_INVALID",
        "DECRYPT_FAILED",
        "MALFORMED_SECRET_FORMAT",
        "INVALID_CONNECTION_BINDING",
        "ADMIN_TOKEN_MISSING",
        "ADMIN_TOKEN_INSECURE_PERMISSIONS",
    }
)


def is_explicit_secret_dev_mode() -> bool:
    """仅当显式设置 ARGUS_SECRET_MODE=development 时启用开发便捷模式，不随 ARGUS_DB_MODE=test 自动开启。"""
    return os.getenv("ARGUS_SECRET_MODE", "").strip().lower() == "development"


def is_dev_keyring_fallback() -> bool:
    return is_explicit_secret_dev_mode() and not os.getenv("ARGUS_MANAGED_SECRET_KEY_FILE", "").strip()


def is_dev_admin_bypass() -> bool:
    return is_explicit_secret_dev_mode() and not os.getenv("ARGUS_CREDENTIAL_ADMIN_TOKEN_FILE", "").strip()


class CredentialUnavailable(ValueError):
    code = "CREDENTIAL_UNAVAILABLE"

    def __init__(self, reason: str | None = None):
        # 仅保留白名单内的非敏感原因码，绝不把远端响应、JWT、Token 或底层异常写入 Attempt / Trace。
        self.reason = reason if reason in CREDENTIAL_FAILURE_REASONS else None
        if self.reason:
            msg = f"CREDENTIAL_UNAVAILABLE ({self.reason}): configured credential could not be resolved"
        else:
            msg = "CREDENTIAL_UNAVAILABLE: configured credential could not be resolved"
        super().__init__(msg)


def _token(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CredentialUnavailable(reason="MALFORMED_SECRET_FORMAT")
    if not value.isascii() or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise CredentialUnavailable(reason="MALFORMED_SECRET_FORMAT")
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
