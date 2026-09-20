from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlsplit

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

    if ref.startswith("env://"):
        env_name = ref[len("env://") :].strip()
        allowed = get_allowed_credential_envs()
        if env_name not in allowed:
            raise ValueError(
                f"Environment variable '{env_name}' is not in allowed credential whitelist: {sorted(allowed)}"
            )
    elif ref.startswith("vault://") or ref.startswith("k8s-secret://"):
        scheme = ref.split("://")[0]
        raise ValueError(f"Credential provider '{scheme}://' is not supported for resolution in current version.")
    else:
        raise ValueError(f"Unsupported credential reference scheme: '{ref}'. Expected env://")


def resolve_credential(ref: str | None) -> str | None:
    if not ref:
        return None

    if ref.startswith("env://"):
        env_name = ref[len("env://") :].strip()
        allowed = get_allowed_credential_envs()
        if env_name in allowed:
            return os.getenv(env_name)
    return None


def sanitize_headers_for_trace(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    sensitive_keys = {"authorization", "cookie", "x-api-key", "x-demo-token"}
    for k, v in headers.items():
        if k.lower() in sensitive_keys:
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized
