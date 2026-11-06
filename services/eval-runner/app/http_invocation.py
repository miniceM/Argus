"""冻结的同步 HTTP 调用契约；这里不持有 Secret 或执行状态。"""
from __future__ import annotations

import base64
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_serializer, model_validator

from .secret_providers import CredentialUnavailable, _token

CredentialType = Literal["bearer_token", "api_key", "basic_auth", "header_value"]
RESERVED_HEADERS = {
    "host", "content-length", "content-type", "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate", "expect",
    "traceparent", "tracestate", "baggage", "idempotency-key", "x-idempotency-key",
    "x-request-id", "cookie", "set-cookie",
}


def header_name(value: str) -> str:
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", value) or value.lower() in RESERVED_HEADERS or value.lower().startswith(("x-argus-", "x-eval-")):
        raise ValueError("Invalid or platform-reserved Header name")
    return value


def header_text(value: str) -> str:
    if not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("Header value must be bounded printable ASCII")
    return value


def sensitive_name(name: str) -> bool:
    return bool(re.search(r"authorization|cookie|token|secret|password|api[-_]?key", name, re.I))


def endpoint_origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError()
        host = parts.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return f"{parts.scheme}://{host}:{port}"
    except (ValueError, UnicodeError):
        raise ValueError("Invalid HTTP target origin") from None


def allowed_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Credential origin cannot contain path, query or fragment")
    return endpoint_origin(url)


class HTTPAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheme: Literal["none", "bearer", "api_key", "basic"]
    header_name: str | None = None
    prefix: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def contract(self):
        if self.scheme == "api_key":
            if self.header_name is None:
                raise ValueError("API Key requires a Header name")
            header_name(self.header_name)
            if self.prefix:
                header_text(self.prefix)
        elif self.header_name is not None or self.prefix:
            raise ValueError("Header name/prefix are API Key settings only")
        return self

    def target_header(self):
        return self.header_name if self.scheme == "api_key" else "Authorization"


class HTTPHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    value: str | None = Field(default=None, max_length=4096)
    credential_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    prefix: str = Field(default="", max_length=64)

    @field_validator("name")
    @classmethod
    def name_contract(cls, value):
        return header_name(value)

    @model_validator(mode="after")
    def value_contract(self):
        if (self.value is None) == (self.credential_id is None):
            raise ValueError("Header requires exactly one of value or credential_id")
        if self.value is not None:
            header_text(self.value)
            if sensitive_name(self.name) or self.prefix:
                raise ValueError("Sensitive Headers require a credential reference; prefix is secret-only")
        if self.prefix:
            header_text(self.prefix)
        return self


DOT_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
TARGET_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _extract_dot_path(body: dict, path: str, label: str = "response path"):
    value = body
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f"Frozen {label} is missing")
    return value


class ResponseMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer_path: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
    fields: dict[str, str] | None = Field(default=None)

    @model_validator(mode="after")
    def validate_mapping_contract(self):
        mapping = self.fields or {}
        if not self.answer_path and not mapping:
            raise ValueError("ResponseMapping requires answer_path or fields")
        if len(mapping) > 32:
            raise ValueError("ResponseMapping supports at most 32 fields")
        for target, source in mapping.items():
            if not TARGET_KEY_RE.fullmatch(target) or target.startswith("_") or target.lower().startswith("argus_"):
                raise ValueError(f"Invalid or reserved response mapping target key: {target}")
            if not source or len(source) > 256 or not DOT_PATH_RE.fullmatch(source):
                raise ValueError(f"Invalid response mapping source dot-path: {source}")
        if self.answer_path and "answer" in mapping and mapping["answer"] != self.answer_path:
            raise ValueError("Conflicting answer_path and fields['answer']")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None and v != {}}

    def apply(self, body: dict) -> dict:
        extracted: dict = {}
        mapping = self.fields or {}
        effective_answer_path = self.answer_path or mapping.get("answer")
        if effective_answer_path:
            answer_val = _extract_dot_path(body, effective_answer_path, "answer path")
            if not isinstance(answer_val, str) or not answer_val.strip():
                raise ValueError("Frozen answer path must contain nonempty text")
            extracted["answer"] = answer_val
        for target, source in mapping.items():
            if target == "answer":
                continue
            val = _extract_dot_path(body, source, f"response path '{source}'")
            if val is None:
                raise ValueError(f"Frozen response path '{source}' resolved to null")
            extracted[target] = val
        # 原始字段仍保留，评分使用明确采纳的提取字段。
        return {**body, **extracted}


class BasicSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: SecretStr
    password: SecretStr


def encode_secret(secret, kind: str) -> str:
    if kind == "basic_auth":
        if not isinstance(secret, dict):
            raise CredentialUnavailable()
        try:
            basic = BasicSecret.model_validate(secret)
            username, password = basic.username.get_secret_value(), basic.password.get_secret_value()
            if not username or not password or ":" in username or len(username) + len(password) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in username + password):
                raise ValueError()
            return base64.b64encode(f"{username}:{password}".encode()).decode()
        except Exception:
            raise CredentialUnavailable() from None
    if not isinstance(secret, str) or len(secret) > 4096:
        raise CredentialUnavailable()
    return _token(secret)


def credential_bindings(spec: dict) -> list[tuple[str, str, str]]:
    auth = HTTPAuth.model_validate(spec.get("http_auth") or {"scheme": "bearer" if spec.get("credential_id") or spec.get("credential_ref") else "none"})
    result = []
    if spec.get("credential_id"):
        kind = {"bearer": "bearer_token", "api_key": "api_key", "basic": "basic_auth"}.get(auth.scheme)
        if not kind:
            raise ValueError("No Auth cannot bind a primary credential")
        result.append((spec["credential_id"], kind, auth.target_header()))
    for entry in spec.get("http_headers") or []:
        if entry.get("credential_id"):
            result.append((entry["credential_id"], "header_value", entry["name"]))
    return result


def check_credential_target(credential, kind: str, endpoint: str, environment: str | None):
    if not credential.enabled or credential.type != kind or credential.environment != (environment or "production"):
        raise ValueError("Credential type, state or environment does not match target")
    if credential.allowed_origins and endpoint_origin(endpoint) not in credential.allowed_origins:
        raise ValueError("Credential is not authorized for target origin")
    if kind != "bearer_token" and not credential.allowed_origins:
        raise ValueError("New credential types require explicit target origins")
