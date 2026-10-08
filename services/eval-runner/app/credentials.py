"""逻辑凭据与加密 Provider；主密钥独立于数据库，密文绑定资源和修订。"""
from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from .db_models import AgentVersionRecord, CredentialAuditRecord, CredentialRecord, CredentialSecretRecord
from .secret_providers import CredentialUnavailable, VaultKubernetesProvider, _token


@dataclass(frozen=True)
class ResolvedCredential:
    token: str = field(repr=False)
    credential_id: str | None = None
    version: int | None = None
    provider: str | None = None


def protected_file(variable: str) -> str:
    try:
        path = Path(os.environ[variable])
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise CredentialUnavailable()
        return path.read_text(encoding="utf-8")
    except (OSError, KeyError, ValueError):
        raise CredentialUnavailable() from None


class ManagedSecretProvider:
    """AES-256-GCM；Keyring 轮换顺序：保留旧密钥、重包裹、移除旧密钥。"""
    def __init__(self):
        try:
            ring = json.loads(protected_file("ARGUS_MANAGED_SECRET_KEY_FILE"))
            self.active_key_id = ring["active_key_id"]
            self.keys = {key_id: base64.b64decode(value, validate=True) for key_id, value in ring["keys"].items()}
            if self.active_key_id not in self.keys or any(len(key) != 32 for key in self.keys.values()):
                raise CredentialUnavailable()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CredentialUnavailable() from None

    def encrypt(self, credential_id: str, version: int, token: str) -> dict:
        nonce = os.urandom(12)
        aad = f"argus-credential-v1:{credential_id}:{version}:{self.active_key_id}".encode()
        ciphertext = AESGCM(self.keys[self.active_key_id]).encrypt(nonce, _token(token).encode(), aad)
        return {"algorithm": "AES-256-GCM", "key_id": self.active_key_id,
                "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode()}

    def decrypt(self, credential_id: str, version: int, envelope: dict) -> str:
        try:
            if envelope["algorithm"] != "AES-256-GCM":
                raise CredentialUnavailable()
            key_id = envelope["key_id"]
            aad = f"argus-credential-v1:{credential_id}:{version}:{key_id}".encode()
            token = AESGCM(self.keys[key_id]).decrypt(base64.b64decode(envelope["nonce"], validate=True),
                    base64.b64decode(envelope["ciphertext"], validate=True), aad).decode()
            return _token(token)
        except Exception:
            raise CredentialUnavailable() from None


class CredentialService:
    def __init__(self, manager):
        self.manager = manager

    @staticmethod
    def metadata(record):
        return {"id": record.id, "name": record.name, "environment": record.environment,
                "type": "bearer_token", "provider": record.provider, "enabled": record.enabled, "version": record.active_version}

    @staticmethod
    def audit(session, record, action, actor, *, version=None):
        session.add(CredentialAuditRecord(id=str(uuid.uuid4()), credential_id=record.id,
                                         action=action, actor=actor, version=record.active_version if version is None else version))

    def usage(self, credential_id):
        with self.manager.get_session() as session:
            versions = session.scalars(select(AgentVersionRecord).where(AgentVersionRecord.credential_id == credential_id)).all()
            return {"credential_id": credential_id, "versions": [
                {"agent_id": version.agent_id, "version": version.version, "is_active": version.is_active}
                for version in versions]}

    def create(self, name, environment, provider, secret, provider_ref, actor):
        credential_id = str(uuid.uuid4())
        if provider == "managed":
            envelope = ManagedSecretProvider().encrypt(credential_id, 1, secret)
            provider_ref = None
        else:
            if os.getenv("ARGUS_VAULT_ENABLED") != "true":
                raise CredentialUnavailable()
            VaultKubernetesProvider().validate(provider_ref)
            envelope = None
        with self.manager.get_session() as session:
            record = CredentialRecord(id=credential_id, name=name, environment=environment, provider=provider, active_version=1)
            session.add(record)
            session.flush()
            session.add(CredentialSecretRecord(credential_id=credential_id, version=1, envelope=envelope, provider_ref=provider_ref))
            self.audit(session, record, "CREATE", actor)
            session.commit()
            return self.metadata(record)

    def rotate(self, credential_id, secret, actor):
        provider = ManagedSecretProvider()
        with self.manager.get_session() as session:
            record = session.scalar(select(CredentialRecord).where(CredentialRecord.id == credential_id).with_for_update())
            if record is None or record.provider != "managed" or not record.enabled:
                raise ValueError("CREDENTIAL_NOT_ROTATABLE")
            record.active_version += 1
            session.add(CredentialSecretRecord(credential_id=record.id, version=record.active_version,
                                              envelope=provider.encrypt(record.id, record.active_version, secret)))
            self.audit(session, record, "ROTATE", actor)
            session.commit()
            return self.metadata(record)

    def rewrap(self, actor):
        provider = ManagedSecretProvider()
        count = 0
        with self.manager.get_session() as session:
            # 任一旧密文不可读都回滚，不产生部分迁移。
            rows = session.scalars(select(CredentialSecretRecord).with_for_update()).all()
            for row in rows:
                if row.envelope is None:
                    continue
                token = provider.decrypt(row.credential_id, row.version, row.envelope)
                if row.envelope["key_id"] == provider.active_key_id:
                    continue
                row.envelope = provider.encrypt(row.credential_id, row.version, token)
                self.audit(session, session.get(CredentialRecord, row.credential_id), "REWRAP", actor, version=row.version)
                count += 1
            session.commit()
        return {"rewrapped": count}

    def resolve(self, credential_id) -> ResolvedCredential:
        try:
            with self.manager.get_session() as session:
                record = session.get(CredentialRecord, credential_id)
                if record is None or not record.enabled:
                    raise CredentialUnavailable()
                revision = session.get(CredentialSecretRecord, (record.id, record.active_version))
                if revision is None:
                    raise CredentialUnavailable()
                provider, version, envelope, reference = record.provider, record.active_version, revision.envelope, revision.provider_ref
            if provider == "managed":
                token = ManagedSecretProvider().decrypt(credential_id, version, envelope)
            elif provider == "vault" and os.getenv("ARGUS_VAULT_ENABLED") == "true":
                token, version = VaultKubernetesProvider().resolve_with_metadata(reference)
                if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                    raise CredentialUnavailable()
            else:
                raise CredentialUnavailable()
            return ResolvedCredential(token, credential_id, version, provider)
        except Exception:
            raise CredentialUnavailable() from None
