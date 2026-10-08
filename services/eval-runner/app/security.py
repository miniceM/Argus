from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlsplit

from .credentials import ResolvedCredential
from .secret_providers import CredentialUnavailable, VaultKubernetesProvider, provider_for

FORBIDDEN_QUERY_KEYS = {"token", "secret", "key", "password", "auth", "api_key", "apikey"}
DEFAULT_ALLOWED_ENVS = {"DEMO_AUTH_TOKEN", "ARGUS_DEMO_TOKEN"}


def validate_endpoint_url(url: str) -> None:
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(f"Endpoint URL must use http or https scheme: {url}")

    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ValueError("URL-embedded credentials (user:pass@host) are forbidden.")

    query_params = parse_qsl(parts.query, keep_blank_values=True)
    for k, _ in query_params:
        if k.lower() in FORBIDDEN_QUERY_KEYS:
            raise ValueError(f"Endpoint URL contains forbidden sensitive query parameters: '{k}'")


def get_allowed_credential_envs() -> set[str]:
    raw = os.getenv("ARGUS_ALLOWED_CREDENTIAL_ENVS")
    if not raw:
        return DEFAULT_ALLOWED_ENVS
    return {item.strip() for item in raw.split(",") if item.strip()}


def validate_credential_ref(ref: str | None) -> None:
    if not ref:
        return
    provider_for(ref, get_allowed_credential_envs()).validate(ref)


def resolve_credential(ref: str | None) -> str | None:
    if not ref:
        return None

    return provider_for(ref, get_allowed_credential_envs()).resolve(ref)


def resolve_execution_credential(ref: str) -> ResolvedCredential:
    provider = provider_for(ref, get_allowed_credential_envs())
    if isinstance(provider, VaultKubernetesProvider):
        token, version = provider.resolve_with_metadata(ref)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise CredentialUnavailable()
        return ResolvedCredential(token, version=version, provider="vault")
    return ResolvedCredential(provider.resolve(ref), provider=ref.split(":", 1)[0])


def sanitize_headers_for_trace(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    sensitive_keys = {"authorization", "cookie", "x-api-key", "x-demo-token"}
    for k, v in headers.items():
        if k.lower() in sensitive_keys:
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized
