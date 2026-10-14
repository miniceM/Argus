from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path

_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTHORIZATION = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s,;]+")
_TOKEN = re.compile(r"(?i)\b(?:pk|sk)[_-][A-Za-z0-9._~-]+\b")
_NAMED_KEY = re.compile(
    r"(?i)\b(?:LANGFUSE_)?(?:PUBLIC|SECRET|API)[_-]?KEY\s*[:=]\s*[^\s,;]+"
)


def summarize_http_error(
    status: int, response_body: str, *, secret_values: Sequence[str]
) -> str:
    """Summarize a JSON error response without logging credentials or URLs."""
    prefix = f"Cloud bootstrap returned HTTP {status}"
    if not response_body.strip():
        return f"{prefix} with an empty response body"

    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError:
        return f"{prefix} with a non-JSON response body"

    if not isinstance(payload, dict):
        return f"{prefix} with a JSON response but no safe error detail"

    detail = payload.get("detail") or payload.get("message") or payload.get("error")
    if not isinstance(detail, str) or not detail.strip():
        return f"{prefix} with a JSON response but no safe error detail"

    sanitized = _URL.sub("[redacted URL]", detail.strip())
    for secret in secret_values:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    sanitized = _AUTHORIZATION.sub(r"\1 [redacted]", sanitized)
    sanitized = _TOKEN.sub("[redacted key]", sanitized)
    sanitized = _NAMED_KEY.sub("[redacted key]", sanitized)
    sanitized = re.sub(r"[\r\n\t]+", " ", sanitized)
    sanitized = re.sub(r"\s{2,}", " ", sanitized).strip()
    if not sanitized:
        return f"{prefix} with no safe error detail"

    if len(sanitized) > 400:
        sanitized = f"{sanitized[:397]}..."
    return f"{prefix}: {sanitized}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=int, required=True)
    parser.add_argument("response_file", type=Path)
    args = parser.parse_args()

    response_body = args.response_file.read_text(encoding="utf-8", errors="replace")
    secret_values = tuple(
        os.environ.get(name, "")
        for name in (
            "LANGFUSE_BASE_URL",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
        )
    )
    print(
        summarize_http_error(
            args.status,
            response_body,
            secret_values=secret_values,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
