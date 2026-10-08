from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Invisible characters that are never legitimate in a URL or an API key but survive
# str.strip(): a UTF-8 BOM (U+FEFF) and zero-width / word-joiner marks. They typically
# arrive by copy-pasting a value out of an editor, and they break strict parsing in ways
# that are extremely hard to diagnose (a URL just reports a missing scheme; a credential
# surfaces as an opaque 401 "Invalid credentials").
INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"

# Kept as an alias: the name predates the credential sanitizer and reads naturally at
# URL-specific call sites.
INVISIBLE_URL_CHARS = INVISIBLE_CHARS


def sanitize_langfuse_input(value: str | None) -> str | None:
    """Strip invisible characters and surrounding whitespace; ``None`` when nothing is left.

    This runs at the configuration boundary only. :func:`validate_langfuse_dashboard_url`
    stays strict so that stored links are never silently rewritten.
    """
    if value is None:
        return None
    cleaned = "".join(char for char in value.strip() if char not in INVISIBLE_CHARS)
    return cleaned.strip() or None


def sanitize_langfuse_url_input(value: str | None) -> str | None:
    """Strip characters that can never appear in a legitimate Langfuse URL."""
    return sanitize_langfuse_input(value)


def sanitize_langfuse_credential_input(value: str | None) -> str | None:
    """Strip characters that can never appear in a legitimate Langfuse API key.

    The SDK reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` straight from the
    environment and base64-encodes them into the ``Authorization`` header without any
    normalization, so a stray BOM or a trailing newline turns every API call into a
    401. Secrets pasted into GitHub Environment variables commonly carry exactly those.
    """
    return sanitize_langfuse_input(value)


def validate_langfuse_dashboard_url(value: str | None) -> str | None:
    """Return a browser-safe absolute Langfuse UI URL, preserving its path."""
    if value is None:
        return None

    candidate = value.strip()
    if not candidate or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F or char == "\\" for char in candidate):
        return None
    if "?" in candidate or "#" in candidate:
        return None

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        # Accessing .port validates numeric syntax and the 0..65535 range.
        _port = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        return None

    return candidate


def _load_langfuse_dashboard_url() -> str | None:
    configured = os.getenv("ARGUS_LANGFUSE_DASHBOARD_URL")
    sanitized = sanitize_langfuse_url_input(configured)
    if configured and sanitized != configured.strip():
        logger.warning(
            "Stripped invisible characters (BOM/zero-width) from ARGUS_LANGFUSE_DASHBOARD_URL."
        )
    validated = validate_langfuse_dashboard_url(sanitized)
    if sanitized and validated is None:
        logger.warning(
            "Ignoring invalid ARGUS_LANGFUSE_DASHBOARD_URL; expected an absolute HTTP(S) URL without credentials, query, or fragment."
        )
    return validated


def _load_langfuse_base_url() -> str:
    """Langfuse API base URL used by the SDK and tracing exporters.

    The SDK performs no normalization of this value, so invisible characters here
    surface as opaque transport errors instead of configuration errors.
    """
    default = "http://langfuse-web:3000"
    configured = os.getenv("LANGFUSE_BASE_URL")
    sanitized = sanitize_langfuse_url_input(configured)
    # A value made only of whitespace/BOM/zero-width sanitizes to None; the effective
    # base URL is then the default, and that is what has to reach the environment.
    resolved = sanitized or default
    if configured and sanitized != configured.strip():
        logger.warning(
            "Stripped invisible characters (BOM/zero-width) from LANGFUSE_BASE_URL; "
            "otherwise every Langfuse SDK call fails with a missing-scheme error."
        )
    if configured and resolved != configured:
        # The SDK reads LANGFUSE_BASE_URL straight from the environment through
        # `get_client()` and normalizes nothing, so cleaning only the settings value
        # would leave every request using the original one: the log would claim the
        # URL was sanitized while the SDK still failed with a missing-scheme error.
        # Write the resolved value back before the first client is created. It is
        # never None, so this cannot raise `str expected, not NoneType` at import.
        os.environ["LANGFUSE_BASE_URL"] = resolved
    return resolved


def find_path(configured_path: str | Path, *subpaths: str) -> Path:
    """Safely resolve a path either from configured path, or by searching parent directories."""
    p = Path(configured_path)
    if p.exists():
        return p

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent.joinpath(*subpaths)
        if candidate.exists():
            return candidate

    return p


@dataclass(frozen=True)
class Settings:
    database_url: str | None = os.getenv("DATABASE_URL")
    argus_db_mode: str = os.getenv("ARGUS_DB_MODE", "prod")
    argus_auto_import_yaml: bool = os.getenv("ARGUS_AUTO_IMPORT_YAML", "true").lower() in ("true", "1", "yes")
    langfuse_base_url: str = field(default_factory=_load_langfuse_base_url)
    argus_langfuse_dashboard_url: str | None = _load_langfuse_dashboard_url()
    agent_registry_path: str = os.getenv("AGENT_REGISTRY_PATH", "/app/config/agents.yaml")
    dataset_seed_path: str = os.getenv("DATASET_SEED_PATH", "/app/data/dataset.json")
    migrations_path: str = os.getenv("ARGUS_MIGRATIONS_PATH", "/app/migrations")
    runner_version: str = os.getenv("RUNNER_VERSION", "0.1.0")
    ready_timeout_seconds: int = int(os.getenv("LANGFUSE_READY_TIMEOUT_SECONDS", "120"))
    argus_redis_url: str | None = os.getenv("ARGUS_REDIS_URL")
    argus_worker_enabled: bool = os.getenv("ARGUS_WORKER_ENABLED", "true").lower() in ("true", "1", "yes")
    argus_reconciler_enabled: bool = os.getenv("ARGUS_RECONCILER_ENABLED", "true").lower() in ("true", "1", "yes")
    worker_concurrency: int = int(os.getenv("ARGUS_WORKER_CONCURRENCY", "10"))
    # Issue #84: how long a recoverable Agent output stays available for an
    # evaluation-only retry. The checkpoint holds the business response only
    # (never a second copy of the full Langfuse trace), so retention is short
    # and explicit. 0 disables evaluation-only recovery entirely.
    execution_checkpoint_ttl_seconds: int = int(
        os.getenv("ARGUS_EXECUTION_CHECKPOINT_TTL_SECONDS", "604800")
    )

    @property
    def environment(self) -> str:
        return os.getenv("ARGUS_ENVIRONMENT", "local")

    @property
    def build_id(self) -> str:
        return os.getenv("ARGUS_BUILD_ID", "dev")


settings = Settings()
