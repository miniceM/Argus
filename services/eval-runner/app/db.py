from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()


class MigrationRunner:
    def __init__(self, engine: Engine, migrations_dir: Path | str):
        self.engine = engine
        self.migrations_dir = Path(migrations_dir)

    def _ensure_schema_migrations(self, conn: Any) -> None:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version VARCHAR(64) PRIMARY KEY, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "checksum VARCHAR(64) NOT NULL"
                ")"
            )
        )

    def get_applied_versions(self) -> dict[str, str]:
        with self.engine.begin() as conn:
            self._ensure_schema_migrations(conn)
            rows = conn.execute(text("SELECT version, checksum FROM schema_migrations")).fetchall()
            return {row[0]: row[1] for row in rows}

    def apply_all(self) -> list[str]:
        if not self.migrations_dir.exists():
            return []

        applied_versions = self.get_applied_versions()
        migration_files = sorted(self.migrations_dir.glob("*.sql"))
        applied_now: list[str] = []

        with self.engine.begin() as conn:
            self._ensure_schema_migrations(conn)
            for file_path in migration_files:
                version = file_path.name
                content = file_path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()

                if version in applied_versions:
                    continue

                # Remove SQL comments
                clean_content = re.sub(r"--[^\n]*", "", content)
                statements = [s.strip() for s in clean_content.split(";") if s.strip()]
                for statement in statements:
                    conn.execute(text(statement))

                conn.execute(
                    text("INSERT INTO schema_migrations (version, checksum) VALUES (:version, :checksum)"),
                    {"version": version, "checksum": checksum},
                )
                applied_now.append(version)

        return applied_now

    def check_schema_compatibility(self) -> None:
        if not self.migrations_dir.exists():
            return
        migration_files = sorted(self.migrations_dir.glob("*.sql"))
        applied = self.get_applied_versions()
        missing = [f.name for f in migration_files if f.name not in applied]
        if missing:
            raise RuntimeError(
                f"Database schema is not up to date. Missing migrations: {missing}. "
                "Please run migration scripts before starting the service."
            )


class DatabaseManager:
    def __init__(self, db_url: str):
        self.db_url = db_url
        connect_args: dict[str, Any] = {}
        engine_kwargs: dict[str, Any] = {}

        if db_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            if ":memory:" in db_url:
                from sqlalchemy.pool import StaticPool
                engine_kwargs["poolclass"] = StaticPool

        self.engine = create_engine(db_url, connect_args=connect_args, **engine_kwargs)
        if db_url.startswith("sqlite"):
            with self.engine.connect() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON;"))

        self.session_factory = sessionmaker(
            autocommit=False, autoflush=False, expire_on_commit=False, bind=self.engine
        )

    @classmethod
    def from_env(cls) -> DatabaseManager:
        db_url = os.getenv("DATABASE_URL")
        mode = os.getenv("ARGUS_DB_MODE", "").lower()

        if not db_url:
            if mode == "test":
                db_url = "sqlite:///:memory:"
            else:
                raise RuntimeError(
                    "DATABASE_URL is not configured. Argus requires an explicit PostgreSQL connection string in production."
                )

        return cls(db_url)

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        session: Session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
